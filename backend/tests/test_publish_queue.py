"""The durable publish queue: rendered clips wait in the database, one per hour.

Pacing alone was not enough. Its clock lived in the worker process, so a restart
made the next upload immediate; the wait held a worker slot hostage for hours; and
a clip whose upload failed (the five of job 91, refused by YouTube's upload limit)
never went back into the queue by itself. These tests pin the three things the
queue exists to do: the spacing is read from the database, exactly one clip goes
out per pass, and a failure leaves the clip queued with a later time rather than
burning it.

Everything here runs against an in-memory SQLite database and a fake uploader. No
request ever reaches YouTube.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _one_hour_gap(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_LIMIT_RETRY_HOURS", 6, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RETRY_MINUTES", 30, raising=False)


@pytest.fixture(autouse=True)
def _rendered_files(tmp_path, monkeypatch):
    """A storage root holding the clip files the queue is asked to upload."""
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    for index in range(3):
        directory = tmp_path / "jobs" / "1" / f"clip_{index}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "final_video.mp4").write_bytes(b"0" * 16)
    return tmp_path


def _clip(db, *, index=0, video_id=None, published_at=None, due=None,
          file_path="jobs/1/clip_0/final_video.mp4", uploaded=False, retries=0,
          error=None, auto_upload=True, status="completed"):
    job = Job(youtube_url="https://youtu.be/x", status=status, auto_upload=auto_upload)
    db.add(job)
    db.flush()
    clip = Clip(job_id=job.id, index=index, start=0.0, end=9.0, title=f"Clip {index}",
                description="d", hashtags=["#Shorts"], file_path=file_path,
                uploaded=uploaded, youtube_video_id=video_id, published_at=published_at,
                upload_retry_count=retries, upload_error=error)
    if due is not None:
        clip.publish_due_at = due
    db.add(clip)
    db.commit()
    return clip


class _Recorder:
    """Stands in for the YouTube upload, and can fail like it does."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, *, video_path, title, description, hashtags, source_title,
                 privacy_status, job_id):
        self.calls.append(title)
        if self.fail_with:
            raise self.fail_with
        return {"video_id": f"vid{len(self.calls)}",
                "shorts_url": f"https://youtube.com/shorts/vid{len(self.calls)}",
                "title": title}


# ---------------------------------------------------------------- the enqueue

def test_a_rendered_clip_is_queued_instead_of_uploaded_now(db):
    clip = _clip(db)
    added = worker.enqueue_clips_for_publish(db, clip.job_id)
    assert added == 1
    db.refresh(clip)
    assert clip.publish_due_at is not None
    assert clip.uploaded is False


def test_a_clip_already_on_youtube_is_never_queued_again(db):
    clip = _clip(db, video_id="already", published_at=datetime.utcnow(), uploaded=True)
    assert worker.enqueue_clips_for_publish(db, clip.job_id) == 0
    db.refresh(clip)
    assert clip.publish_due_at is None


def test_a_clip_already_waiting_keeps_its_place(db):
    clip = _clip(db)
    first = datetime.utcnow() + timedelta(minutes=40)
    db.query(Clip).filter(Clip.id == clip.id).update({"publish_due_at": first})
    db.commit()
    worker.enqueue_clips_for_publish(db, clip.job_id)
    db.refresh(clip)
    assert clip.publish_due_at == first


# ------------------------------------------------------- the gap, read from disk

def test_the_first_clip_of_an_idle_channel_is_due_immediately(db):
    assert worker.next_publish_slot(db, now=datetime(2026, 10, 16, 12, 0)) == \
        datetime(2026, 10, 16, 12, 0)


def test_the_next_clip_waits_an_hour_after_the_last_publish(db):
    last = datetime(2026, 10, 16, 12, 0)
    _clip(db, video_id="one", published_at=last, uploaded=True)
    due = worker.next_publish_slot(db, now=last)
    assert due == last + timedelta(seconds=3600)


def test_the_gap_survives_a_worker_restart(db, monkeypatch):
    """The clock is the database, not the process.

    The old pacing kept its last-upload stamp in a dict, so a restart reset it and
    the very next publish fired immediately. Nothing in this test calls note_upload:
    the only state is the published_at row.
    """
    from utils import upload_pacing
    upload_pacing.reset()
    last = datetime.utcnow() - timedelta(minutes=20)
    _clip(db, video_id="one", published_at=last, uploaded=True)
    due = worker.next_publish_slot(db)
    assert due > datetime.utcnow()
    assert due == last + timedelta(seconds=3600)


def test_the_queue_never_schedules_into_the_past(db):
    long_ago = datetime.utcnow() - timedelta(days=2)
    _clip(db, video_id="one", published_at=long_ago, uploaded=True)
    assert worker.next_publish_slot(db) <= datetime.utcnow()


# ------------------------------------------------------------- one pass, one clip

def test_one_pass_uploads_exactly_one_clip(db):
    now = datetime.utcnow()
    first = _clip(db, index=0, due=now - timedelta(minutes=2))
    second = _clip(db, index=1, due=now - timedelta(minutes=1))
    recorder = _Recorder()
    assert worker.publish_queued_clips(db, now=now, uploader=recorder) == 1
    assert len(recorder.calls) == 1
    db.refresh(first)
    db.refresh(second)
    assert first.youtube_video_id == "vid1"
    assert second.youtube_video_id is None


def test_a_clip_that_is_not_due_yet_is_left_alone(db):
    now = datetime.utcnow()
    clip = _clip(db, due=now + timedelta(minutes=30))
    recorder = _Recorder()
    assert worker.publish_queued_clips(db, now=now, uploader=recorder) == 0
    assert recorder.calls == []
    db.refresh(clip)
    assert clip.publish_due_at is not None


def test_a_clip_without_a_due_time_is_not_touched(db):
    _clip(db)
    recorder = _Recorder()
    assert worker.publish_queued_clips(db, now=datetime.utcnow(), uploader=recorder) == 0
    assert recorder.calls == []


def test_a_clip_of_a_job_that_did_not_ask_for_upload_is_skipped(db):
    clip = _clip(db, auto_upload=False, due=datetime.utcnow() - timedelta(minutes=1))
    recorder = _Recorder()
    assert worker.publish_queued_clips(db, now=datetime.utcnow(), uploader=recorder) == 0
    assert recorder.calls == []
    db.refresh(clip)
    assert clip.uploaded is False


def test_two_workers_cannot_upload_the_same_clip(db):
    """The claim is a compare-and-swap, like the job lease."""
    now = datetime.utcnow()
    clip = _clip(db, due=now - timedelta(seconds=1))
    claimed = worker.claim_clip_for_publish(db, clip.id, expected_due=clip.publish_due_at,
                                            now=now)
    assert claimed is True
    assert worker.claim_clip_for_publish(db, clip.id, expected_due=now - timedelta(seconds=1),
                                         now=now) is False


# ----------------------------------------------------------------- bookkeeping

def test_success_clears_the_queue_row(db):
    now = datetime.utcnow()
    clip = _clip(db, due=now - timedelta(seconds=1))
    worker.publish_queued_clips(db, now=now, uploader=_Recorder())
    db.refresh(clip)
    assert clip.uploaded is True
    assert clip.publish_due_at is None
    assert clip.upload_error is None
    assert abs(clip.published_at - now) < timedelta(seconds=5)


def test_an_ordinary_failure_keeps_the_clip_queued_with_a_short_backoff(db):
    now = datetime.utcnow()
    clip = _clip(db, due=now - timedelta(seconds=1))
    boom = RuntimeError("transient network")
    worker.publish_queued_clips(db, now=now, uploader=_Recorder(fail_with=boom))
    db.refresh(clip)
    assert clip.uploaded is False
    assert clip.youtube_video_id is None
    assert clip.upload_retry_count == 1
    assert "transient network" in clip.upload_error
    assert clip.publish_due_at is not None
    assert clip.publish_due_at > now
    assert clip.publish_due_at <= now + timedelta(minutes=31)


def test_the_upload_limit_error_is_not_charged_as_a_retry(db):
    """YouTube's daily cap is not the clip's fault.

    Counting it as an attempt would retire a perfectly good clip after three
    refusals on a busy day. It waits instead, hours out.
    """
    now = datetime.utcnow()
    clip = _clip(db, due=now - timedelta(seconds=1))
    limit = Exception('<HttpError 400 ... "reason": "uploadLimitExceeded">')
    worker.publish_queued_clips(db, now=now, uploader=_Recorder(fail_with=limit))
    db.refresh(clip)
    assert clip.upload_retry_count == 0
    assert clip.publish_due_at >= now + timedelta(hours=5)
    assert clip.publish_due_at <= now + timedelta(hours=6, minutes=1)


def test_a_clip_gives_up_after_the_declared_retry_budget(db):
    now = datetime.utcnow()
    clip = _clip(db, due=now - timedelta(seconds=1), retries=settings.MAX_UPLOAD_RETRIES - 1)
    worker.publish_queued_clips(db, now=now, uploader=_Recorder(fail_with=RuntimeError("nope")))
    db.refresh(clip)
    assert clip.upload_retry_count == settings.MAX_UPLOAD_RETRIES
    assert clip.publish_due_at is None


def test_a_clip_whose_file_is_gone_is_not_offered_to_the_uploader(db):
    clip = _clip(db, file_path="jobs/1/clip_9/final_video.mp4",
                 due=datetime.utcnow() - timedelta(seconds=1))
    recorder = _Recorder()
    assert worker.publish_queued_clips(db, now=datetime.utcnow(), uploader=recorder) == 0
    assert recorder.calls == []
    db.refresh(clip)
    assert clip.uploaded is False
    assert "missing" in clip.upload_error


# ------------------------------------------------------------------- the wiring

def test_the_completion_sweep_queues_rather_than_uploads(db, monkeypatch):
    """The end-of-job sweep used to upload in place, ignoring the spacing.

    That is how the five clips of job 91 left inside four seconds. It now only
    parks rows, so nothing here may reach an uploader.
    """
    import asyncio

    clip = _clip(db)
    direct = []
    monkeypatch.setattr("services.youtube_upload.upload_clip_to_youtube",
                        lambda *a, **k: direct.append(k), raising=True)
    monkeypatch.setattr("services.tiktok_upload.upload_all_clips_to_tiktok_for_job",
                        lambda *a, **k: {"success": True}, raising=True)
    job = db.query(Job).filter(Job.id == clip.job_id).one()

    asyncio.run(worker.stage_publish_all_clips(job.id, db, job, "SELF_ONLY",
                                               lambda *args: None))

    assert direct == []
    db.refresh(clip)
    assert clip.uploaded is False
    assert clip.publish_due_at is not None


def test_the_shipped_defaults_space_publishes_an_hour_apart():
    from config import Settings

    fields = Settings.model_fields
    assert fields["UPLOAD_MIN_GAP_SECONDS"].default == 3600
    assert fields["UPLOAD_GAP_JITTER_SECONDS"].default == 600
    assert fields["PUBLISH_LIMIT_RETRY_HOURS"].default == 6
