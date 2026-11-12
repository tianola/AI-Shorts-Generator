"""The release clock moves to YouTube, so the PC is no longer part of the schedule.

The durable queue paced publishes by keeping the worker awake: fifteen clips meant
fifteen hours of this machine staying on, and a shutdown mid-evening silently took
the rest of the night's schedule with it. YouTube holds that schedule itself - a
video uploaded with `privacyStatus: private` plus `status.publishAt` becomes public
on its own at that instant, documented at developers.google.com/youtube/v3/docs/videos.

Two clocks follow from that, and they are the thing to keep straight:

* the upload clock, `UPLOAD_MIN_GAP_SECONDS` (minutes) - how fast this machine may
  hand videos to YouTube, and the only reason the local queue survives at all;
* the release clock, `PUBLISH_SCHEDULE_GAP_SECONDS` (hours) - when each video is
  allowed to go public, which YouTube now enforces.

Both anchors are read from the database, never from process memory, so a restart
continues the schedule instead of resetting it.

No request reaches YouTube: the API client is a mock and the queue runs on an
in-memory database with a fake uploader.
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
from tests.youtube_api_fake import fake_service, insert_body, run_upload

NOW = datetime(2026, 11, 12, 1, 0)


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
def _two_clocks(monkeypatch):
    """Pinned here rather than inherited, so a default change cannot pass silently.

    The human-cadence guards (a daily upload budget, a release window, a
    same-source spread) are switched off so that these tests measure the two
    clocks and nothing else; `test_publish_hygiene.py` pins those guards on.
    """
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_LEAD_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_UPLOAD_LEAD_SECONDS", 21600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_DAILY_BUDGET", 0, raising=False)


@pytest.fixture(autouse=True)
def _rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    for index in range(3):
        directory = tmp_path / "jobs" / "1" / f"clip_{index}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "final_video.mp4").write_bytes(b"0" * 16)
    return tmp_path


def _job(db, clip_count=0, *, clips=None):
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=True)
    db.add(job)
    db.flush()
    for index in range(clip_count):
        db.add(Clip(job_id=job.id, index=index, start=0.0, end=9.0,
                    title=f"Clip {index}", description="d", hashtags=["#Shorts"],
                    file_path="jobs/1/clip_0/final_video.mp4"))
    for clip in (clips or []):
        clip.job_id = job.id
        db.add(clip)
    db.commit()
    return job


def _clip(index, title, file_path, **kwargs):
    return Clip(index=index, title=title, file_path=file_path, start=0.0, end=9.0,
                **kwargs)


# ------------------------------------------------- what actually leaves the process

def test_a_scheduled_upload_is_sent_private_with_a_publish_at(tmp_path):
    """publishAt is only accepted on a private video, so privacy is not the caller's.

    An unlisted video with a publish time is a contradiction YouTube rejects; the
    scheduled state is private-until-the-slot, public-after.
    """
    service = fake_service()

    result = run_upload(tmp_path, service, privacy_status="public",
                        publish_at=datetime(2026, 11, 12, 5, 15))

    status = insert_body(service)["status"]
    assert status["privacyStatus"] == "private"
    assert status["publishAt"] == "2026-11-12T05:15:00Z"
    assert result["publish_at"] == "2026-11-12T05:15:00Z"
    assert result["privacy_status"] == "private"


def test_an_unscheduled_upload_keeps_the_privacy_it_was_given(tmp_path):
    """The manual paths still mean "now", in the visibility the operator asked for."""
    service = fake_service()

    result = run_upload(tmp_path, service, privacy_status="unlisted")

    status = insert_body(service)["status"]
    assert status["privacyStatus"] == "unlisted"
    assert "publishAt" not in status
    assert result["publish_at"] is None


# ------------------------------------------------------------- the release clock

def test_parking_a_job_staggers_releases_an_hour_apart(db):
    job = _job(db, clip_count=3)

    assert worker.enqueue_clips_for_publish(db, job.id, now=NOW) == 3
    releases = sorted(clip.scheduled_publish_at
                      for clip in db.query(Clip).filter_by(job_id=job.id))
    assert releases == [NOW + timedelta(minutes=15),
                        NOW + timedelta(hours=1, minutes=15),
                        NOW + timedelta(hours=2, minutes=15)]


def test_parking_a_job_uploads_on_the_way_to_each_release(db):
    """The two clocks must not be confused: a row is dated by its release.

    Uploads used to be spaced by how fast this machine could push bytes — one per
    quarter hour here — so a job of fifteen clips spent the channel's whole
    rolling day of YouTube uploads on the first afternoon and was refused before
    the rest had a turn. Each upload now sits PUBLISH_UPLOAD_LEAD_SECONDS before
    its own release, which leaves the budget roughly where the releases are.
    """
    job = _job(db, clip_count=3)

    worker.enqueue_clips_for_publish(db, job.id, now=NOW)
    rows = db.query(Clip).filter_by(job_id=job.id).order_by(Clip.index.asc()).all()
    assert [clip.scheduled_publish_at for clip in rows] == [
        NOW + timedelta(minutes=15),
        NOW + timedelta(hours=1, minutes=15),
        NOW + timedelta(hours=2, minutes=15),
    ]
    assert [clip.publish_due_at for clip in rows] == [
        NOW - timedelta(hours=5, minutes=45),
        NOW - timedelta(hours=4, minutes=45),
        NOW - timedelta(hours=3, minutes=45),
    ]


def test_the_first_release_is_never_the_moment_the_upload_finishes(db):
    """A clip gets a window to be inspected before the channel's audience sees it."""
    job = _job(db, clip_count=1)

    worker.enqueue_clips_for_publish(db, job.id, now=NOW)
    clip = db.query(Clip).filter_by(job_id=job.id).one()
    assert clip.scheduled_publish_at >= NOW + timedelta(minutes=15)


def test_the_release_clock_survives_a_worker_restart(db):
    """The anchor is the database row, not a variable in the worker.

    One clip was released five minutes ago, so the next slot is an hour after
    that rather than the fifteen-minute lead: the anchor has to bind, otherwise
    the test passes with no clock read at all. Nothing here calls a note_*
    helper; the only state is the row.
    """
    released = _clip(index=1, title="done", file_path="jobs/1/clip_0/final_video.mp4",
                     youtube_video_id="vid0", uploaded=True,
                     published_at=NOW - timedelta(minutes=5),
                     scheduled_publish_at=NOW - timedelta(minutes=5))
    job = _job(db, clip_count=1, clips=[released])

    worker.enqueue_clips_for_publish(db, job.id, now=NOW)
    pending = db.query(Clip).filter(Clip.youtube_video_id.is_(None)).one()
    assert pending.scheduled_publish_at == NOW + timedelta(minutes=55)


def test_a_release_already_parked_is_continued_not_collided(db):
    """A second job cannot put two videos on YouTube at the same minute.

    Clip 0 is uploaded and waiting for its 02:00 slot. A new job parks two clips:
    they must land at 03:00 and 04:00, because 02:00 is taken.
    """
    waiting = _clip(index=0, title="waiting",
                    file_path="jobs/1/clip_0/final_video.mp4",
                    youtube_video_id="vid0", uploaded=True,
                    published_at=NOW, scheduled_publish_at=NOW + timedelta(hours=1))
    _job(db, clips=[waiting])
    second = _job(db, clip_count=2)

    worker.enqueue_clips_for_publish(db, second.id, now=NOW)
    releases = sorted(clip.scheduled_publish_at
                      for clip in db.query(Clip).filter_by(job_id=second.id))
    assert releases == [NOW + timedelta(hours=2), NOW + timedelta(hours=3)]


def test_a_clip_parked_before_the_release_clock_existed_gets_a_slot(db):
    """Rows already in the database are the ones this change has to carry.

    Nine clips are sitting in the real queue with a due time and no release slot.
    Uploaded as they are, they go public the instant the queue reaches them, which
    is the behaviour this change exists to remove.
    """
    legacy = _clip(index=0, title="parked by the old worker",
                   file_path="jobs/1/clip_0/final_video.mp4",
                   publish_due_at=NOW - timedelta(hours=5))
    job = _job(db, clips=[legacy])

    worker.enqueue_clips_for_publish(db, job.id, now=NOW)
    db.refresh(legacy)
    assert legacy.scheduled_publish_at >= NOW + timedelta(minutes=15)


def test_a_clip_that_gave_up_frees_its_slot(db):
    """A retired clip must not hold the channel's clock open forever."""
    doomed = _clip(index=0, title="doomed",
                   file_path="jobs/1/clip_0/final_video.mp4",
                   publish_due_at=NOW - timedelta(seconds=1),
                   scheduled_publish_at=NOW + timedelta(hours=1),
                   upload_retry_count=settings.MAX_UPLOAD_RETRIES - 1)
    _job(db, clips=[doomed])

    worker.publish_queued_clips(db, now=NOW, uploader=_FailingUploader())
    db.refresh(doomed)
    assert doomed.publish_due_at is None
    assert doomed.scheduled_publish_at is None


def test_a_slot_that_already_passed_is_moved_before_the_handover(db):
    """A clip can sit in the queue past its own release time.

    Uploads refused by the daily cap do not stop the clock, so by the moment this
    clip is finally handed over its slot is hours gone. YouTube reads a publishAt
    in the past as "publish now", and the audience would see an unreviewed clip
    at an instant the schedule already claims it left. The row moves, so the next
    clip is placed after it rather than on top of it.
    """
    clip = _clip(index=0, title="t", file_path="jobs/1/clip_0/final_video.mp4",
                 publish_due_at=NOW - timedelta(seconds=1),
                 scheduled_publish_at=NOW - timedelta(hours=3))
    _job(db, clips=[clip])
    recorder = _ScheduleRecorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=recorder) == 1
    db.refresh(clip)
    assert clip.scheduled_publish_at >= NOW + timedelta(minutes=15)
    assert recorder.publish_ats == [clip.scheduled_publish_at]


def test_a_clip_that_reaches_the_queue_without_a_slot_is_scheduled(db):
    """The queue pass, not only the enqueue, is what has to refuse an instant post.

    Nine clips are parked in the real database with a due time and no release
    slot, and nothing will re-run the enqueue for a job that already finished -
    so the enqueue-side backfill never sees them. Handed over with no publishAt
    they go public the second their turn comes, at whichever visibility the job
    asked for. Job 109's is "public".
    """
    clip = _clip(index=0, title="t", file_path="jobs/1/clip_0/final_video.mp4",
                 publish_due_at=NOW - timedelta(hours=5))
    _job(db, clips=[clip])
    recorder = _ScheduleRecorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=recorder) == 1
    db.refresh(clip)
    assert clip.scheduled_publish_at >= NOW + timedelta(minutes=15)
    assert recorder.publish_ats == [clip.scheduled_publish_at]


class _FailingUploader:
    def __call__(self, **kwargs):
        raise RuntimeError("nope")


# ---------------------------------------------------------- the upload hands it over

class _ScheduleRecorder:
    """Stands in for the uploader and remembers the release time it was given."""

    def __init__(self):
        self.publish_ats = []

    def __call__(self, *, video_path, title, description, hashtags, source_title,
                 source_url=None, privacy_status, job_id, thumbnail_path=None,
                 publish_at=None):
        self.publish_ats.append(publish_at)
        return {"video_id": "vid1",
                "shorts_url": "https://youtube.com/shorts/vid1",
                "title": title, "publish_at": "2026-11-12T05:15:00Z"}


def test_the_queued_release_time_is_passed_to_the_uploader(db):
    release = NOW + timedelta(hours=1)
    clip = _clip(index=0, title="t", file_path="jobs/1/clip_0/final_video.mp4",
                 publish_due_at=NOW - timedelta(seconds=1), scheduled_publish_at=release)
    _job(db, clips=[clip])
    recorder = _ScheduleRecorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=recorder) == 1
    assert recorder.publish_ats == [release]


def test_uploading_a_clip_leaves_its_release_slot_in_place(db):
    """The slot is the channel's clock, so it outlives the upload that reserved it."""
    release = NOW + timedelta(hours=1)
    clip = _clip(index=0, title="t", file_path="jobs/1/clip_0/final_video.mp4",
                 publish_due_at=NOW - timedelta(seconds=1), scheduled_publish_at=release)
    _job(db, clips=[clip])

    worker.publish_queued_clips(db, now=NOW, uploader=_ScheduleRecorder())
    db.refresh(clip)
    assert clip.scheduled_publish_at == release
    assert clip.publish_due_at is None
    assert clip.published_at is not None


def test_the_shipped_defaults_are_two_shorts_a_day_within_a_daytime_window():
    """The numbers a person running a channel would recognise, spelled out.

    Two releases a day inside 06:00-22:00 UTC, uploaded six hours ahead of their
    slot, at most eight uploads per rolling day against the roughly ten YouTube
    allows this channel — see docs/YOUTUBE_PUBLISH_BUDGET.md. A quarter-hour
    upload cadence with an hourly release used to be the shipped pair, which is
    how a single job could empty a day of upload budget before releasing six clips.
    """
    from config import Settings

    fields = Settings.model_fields
    assert fields["UPLOAD_MIN_GAP_SECONDS"].default == 3600
    assert fields["UPLOAD_DAILY_BUDGET"].default == 8
    assert fields["PUBLISH_SCHEDULE_GAP_SECONDS"].default == 43200
    assert fields["PUBLISH_SCHEDULE_LEAD_SECONDS"].default == 900
    assert fields["PUBLISH_UPLOAD_LEAD_SECONDS"].default == 21600
    assert fields["PUBLISH_RELEASE_WINDOW_UTC"].default == "6-22"
    assert fields["PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS"].default == 86400
    assert fields["SHORTS_CTA"].default == ""
