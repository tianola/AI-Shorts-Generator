"""The manual publish buttons were a second, undisciplined path to YouTube.

`publish_queued_clips` is the path the queue uses: it checks the channel's gap
(`next_publish_slot`), the channel's rolling upload budget (`next_budget_slot`),
claims the row so a second worker cannot race it (`claim_clip_for_publish`),
hands YouTube the clip's release slot as `publishAt`, and refuses to charge a
strike for `uploadLimitExceeded` because that refusal is the channel's problem.

The two buttons in the Studio UI - "upload this clip" and "upload the whole job" -
called `services.youtube_upload.upload_clip_to_youtube` directly and none of that
applied. The production rows say so:

* clip 461 (job 109, index 8): `uploaded=1`, `youtube_video_id=ICgUjmEUlbg`,
  `published_at` set, `scheduled_publish_at IS NULL` (no release slot was ever
  used, so it went public the instant it was clicked) and `publish_due_at` left
  set - the row reads as published *and* as a publish that is still due, so any
  consumer keying off `publish_due_at` sees a phantom.
* clip 371 (job 91, index 4): `upload_retry_count=2` with both clocks NULL and
  `upload_error` an `uploadLimitExceeded` HttpError - the same refusal the queue
  forgives cost this clip two of its three strikes, because the endpoint's own
  handler increments the counter on any exception.

These tests drive the real endpoints over `TestClient` against an in-memory
database with `get_youtube_auth_status` and `upload_clip_to_youtube` replaced by
fakes. Nothing here can reach YouTube: every publish assertion reads the kwargs
the fake was handed, and the fake raises before any client is built.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app

VIDEO_FILE = "jobs/1/clip_0/final_video.mp4"
LIMIT_ERROR = ('<HttpError 400 when requesting None returned "The user has exceeded '
               'the number of videos they may upload." reason: "uploadLimitExceeded">')


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _gates(monkeypatch):
    """The operator's cadence, pinned so a changed default cannot pass a guard by accident."""
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_DAILY_BUDGET", 8, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 43200, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_LEAD_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_UPLOAD_LEAD_SECONDS", 21600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RETRY_MINUTES", 30, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_LIMIT_RETRY_HOURS", 6, raising=False)
    monkeypatch.setattr(settings, "MAX_UPLOAD_RETRIES", 3, raising=False)


@pytest.fixture(autouse=True)
def _rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    for job_dir in range(1, 11):
        for index in range(10):
            directory = tmp_path / "jobs" / str(job_dir) / f"clip_{index}"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "final_video.mp4").write_bytes(b"0" * 16)
    return tmp_path


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    """An authenticated channel without a token file: the endpoint's first gate."""
    monkeypatch.setattr("services.youtube_upload.get_youtube_auth_status",
                        lambda: {"authenticated": True, "channel_name": "tester"})


class _Recorder:
    """Stands in for `upload_clip_to_youtube` and remembers what it was handed."""

    def __init__(self, fail_with=None):
        self.calls = []
        self.fail_with = fail_with

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_with:
            raise self.fail_with
        n = len(self.calls)
        return {"video_id": f"vid{n}",
                "shorts_url": f"https://youtube.com/shorts/vid{n}",
                "watch_url": f"https://www.youtube.com/watch?v=vid{n}",
                "title": kwargs.get("title") or "Clip",
                "privacy_status": kwargs.get("privacy_status"),
                "publish_at": kwargs.get("publish_at")}


@pytest.fixture
def uploader(monkeypatch):
    """The fake is installed on the module, where every publish path imports it lazily."""
    recorder = _Recorder()
    monkeypatch.setattr("services.youtube_upload.upload_clip_to_youtube", recorder)
    return recorder


def _job(db, *, clips=1, first_index=0, published_back=None):
    """A completed job with `clips` rendered clips, none of them published."""
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=False)
    db.add(job)
    db.flush()
    rows = []
    for index in range(first_index, first_index + clips):
        clip = Clip(job_id=job.id, index=index, start=0.0, end=9.0,
                    title=f"Clip {index}", description="d", hashtags=["#Shorts"],
                    file_path=f"jobs/{job.id}/clip_{index}/final_video.mp4",
                    uploaded=False, upload_retry_count=0)
        if published_back and index == first_index:
            clip.youtube_video_id = f"old{index}"
            clip.uploaded = True
            clip.published_at = datetime.utcnow() - published_back
        db.add(clip)
        rows.append(clip)
    db.commit()
    return job, rows


def _single(db, **fields):
    _, rows = _job(db, clips=1)
    clip = rows[0]
    for key, value in fields.items():
        setattr(clip, key, value)
    db.commit()
    return clip


# ------------------------------------------------------------- the duplicate click

def test_a_second_click_on_a_published_clip_uploads_nothing(db, client, uploader):
    """The endpoint never read `uploaded` / `youtube_video_id`, so a double click
    put the same short on the channel twice. A refusal is the safe answer, and it
    has to name the video that is already out there."""
    clip = _single(db, uploaded=True, youtube_video_id="ICgUjmEUlbg",
                   published_at=datetime.utcnow())

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert uploader.calls == [], "the fake uploader was reachable: a second copy would be live"
    assert response.status_code == 409
    assert "ICgUjmEUlbg" in response.json()["detail"]


# ---------------------------------------------------------------- the channel's gap

def test_the_manual_button_waits_for_the_channels_gap(db, client, uploader):
    """An idle-hour channel may not be jumped by a click.

    The queue's guard is `now >= next_publish_slot(db)`; the click had no
    equivalent, so it uploaded inside the gap the operator set with
    UPLOAD_MIN_GAP_SECONDS.
    """
    _single(db, uploaded=True, youtube_video_id="recent",
            published_at=datetime.utcnow() - timedelta(minutes=5))
    clip = _single(db, index=1)

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert uploader.calls == []
    db.expire_all()
    db.refresh(clip)
    assert clip.publish_due_at is not None, "a held clip stays queued instead of being forgotten"
    assert clip.publish_due_at > datetime.utcnow() + timedelta(minutes=50)
    assert clip.upload_retry_count == 0, "being held back is not the clip's failure"


# --------------------------------------------------------------- the channel's budget

def test_the_manual_button_respects_the_channels_upload_budget(db, client, uploader):
    """UPLOAD_DAILY_BUDGET existed only for the queue, so a click could walk
    straight into the refusal the budget guard exists to avoid."""
    for index in range(8):
        _single(db, index=index, uploaded=True, youtube_video_id=f"b{index}",
                published_at=datetime.utcnow() - timedelta(hours=5))
    clip = _single(db, index=8)

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.json()["success"] is False
    assert uploader.calls == []
    db.expire_all()
    db.refresh(clip)
    assert clip.publish_due_at >= datetime.utcnow() + timedelta(hours=18)
    assert clip.upload_retry_count == 0


# ------------------------------------------------------------------ the release slot

def test_a_manual_publish_is_handed_its_release_slot(db, client, uploader):
    """clip 461 went public on click because `publishAt` was never passed.

    YouTube holds the schedule itself once the clip carries a publish time, which
    is what keeps the cadence alive when this machine is off; a manual publish has
    to keep that promise too, so it gets a slot when it has none.
    """
    clip = _single(db)

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["video_id"] == "vid1"
    assert len(uploader.calls) == 1
    publish_at = uploader.calls[0]["publish_at"]
    assert publish_at is not None, "no publishAt means YouTube releases it the instant it lands"
    assert publish_at > datetime.utcnow()
    db.expire_all()
    db.refresh(clip)
    assert clip.scheduled_publish_at == publish_at


def test_a_manual_publish_clears_the_queue_row_it_just_published(db, client, uploader):
    """The other half of clip 461: `publish_due_at` survived its own publish."""
    clip = _single(db, publish_due_at=datetime.utcnow() - timedelta(minutes=1))

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.status_code == 200, response.text
    db.expire_all()
    db.refresh(clip)
    assert clip.uploaded is True
    assert clip.publish_due_at is None
    assert clip.published_at is not None


# ------------------------------------------------------- strikes for the channel's refusals

def test_a_channel_refusal_costs_the_manual_clip_no_strike(db, client, uploader):
    """clip 371: the queue forgives `uploadLimitExceeded`, the click did not.

    `_record_publish_failure` waits for the budget window to clear instead of
    charging an attempt, because a refusal about the channel's quota says nothing
    about this clip. Two strikes on a perfectly good clip, and a third click
    retires it with both clocks nulled - it leaves the queue invisibly.
    """
    uploader.fail_with = Exception(LIMIT_ERROR)
    clip = _single(db, upload_retry_count=1)

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.status_code == 500
    db.expire_all()
    db.refresh(clip)
    assert clip.upload_retry_count == 1, "the channel's budget is not the clip's fault"
    assert clip.upload_error and "uploadLimitExceeded" in clip.upload_error
    assert clip.publish_due_at is not None, "a refused clip stays in the queue"
    assert clip.publish_due_at > datetime.utcnow() + timedelta(hours=5)
    assert clip.youtube_video_id is None


def test_a_real_upload_failure_still_costs_a_strike(db, client, uploader):
    """The fix must not silence genuine failures: the queue charges those, so the
    click charges those too."""
    uploader.fail_with = RuntimeError("transient network")
    clip = _single(db)

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert response.status_code == 500
    db.expire_all()
    db.refresh(clip)
    assert clip.upload_retry_count == 1
    assert clip.upload_error and "transient network" in clip.upload_error


# ------------------------------------------------------------------------- the batch button

def test_the_batch_button_publishes_one_clip_and_queues_the_rest(db, client, uploader):
    """The batch endpoint is the same bypass with a loop around it.

    `upload_all_clips_for_job` was called with `pace=False` ("a human pressing
    upload asked for them now") and no budget check, which is the shape that put
    five clips of job 91 on the channel inside four seconds. One clip may go now;
    the rest wait in the queue on the channel's own clock.
    """
    job, rows = _job(db, clips=3)

    response = client.post(f"/api/jobs/{job.id}/upload-all-youtube",
                           json={"privacy_status": "unlisted"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(uploader.calls) == 1, "a click is not permission to burst the channel"
    assert body["uploaded_count"] == 1
    assert body["total_clips"] == 3

    db.expire_all()
    for row in rows:
        db.refresh(row)
    assert rows[0].youtube_video_id == "vid1"
    assert rows[0].publish_due_at is None
    for row in rows[1:]:
        assert row.publish_due_at is not None, "the held clips are queued, not dropped"
        assert row.publish_due_at > datetime.utcnow()
    assert rows[2].scheduled_publish_at is None or \
        rows[2].scheduled_publish_at > rows[1].scheduled_publish_at


def test_the_batch_button_skips_a_clip_that_is_already_on_youtube(db, client, uploader):
    job, rows = _job(db, clips=2, published_back=timedelta(hours=2))

    response = client.post(f"/api/jobs/{job.id}/upload-all-youtube",
                           json={"privacy_status": "unlisted"})

    assert response.status_code == 200, response.text
    db.expire_all()
    db.refresh(rows[0])
    assert rows[0].youtube_video_id == "old0", "the already-published clip keeps its own id"
    assert rows[1].youtube_video_id == "vid1"
    assert len(uploader.calls) == 1


# ------------------------------------------------- the service-level batch sweep

def test_the_direct_batch_sweep_charges_no_strike_for_a_channel_refusal(db, monkeypatch):
    """`upload_all_clips_for_job` keeps the same line, so it keeps the same fix.

    A script or a REPL can still call it, and its `clip.upload_retry_count += 1`
    on any exception is the defect-2 bug in its second home.
    """
    from services import youtube_upload

    monkeypatch.setattr(youtube_upload, "get_youtube_auth_status",
                        lambda: {"authenticated": True, "channel_name": "tester"})

    def refused(**kwargs):
        raise Exception(LIMIT_ERROR)

    monkeypatch.setattr(youtube_upload, "upload_clip_to_youtube", refused)
    clip = _single(db, upload_retry_count=1)

    result = youtube_upload.upload_all_clips_for_job(clip.job_id, db,
                                                     privacy_status="unlisted")

    db.expire_all()
    db.refresh(clip)
    assert result["failed_count"] == 1
    assert clip.upload_retry_count == 1, "the refusal is about the channel, not the clip"
    assert "uploadLimitExceeded" in clip.upload_error
