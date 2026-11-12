"""An upload that succeeded must never be able to look like one that never happened.

`publish_queued_clips` used to hand the file to YouTube first and only then write
`uploaded` / `youtube_video_id` / `published_at` and clear `publish_due_at`. If the
process died - or that commit raised - in the window between the API returning a
video id and the row being written, YouTube had the video and the database did not:
the queue's eligibility filter (`youtube_video_id IS NULL OR youtube_video_id = ''`)
made the same clip due again and it would have uploaded a second copy of itself.

The discipline is the reverse order: write the *intent* (`publish_attempt_at`) and
commit it before the side effect, clear it once the outcome is known, and treat a
row that still carries an attempt with no outcome as one that must not be retried
automatically. What that buys is detectability, not invulnerability - the window
narrows to "the attempt is recorded, the result is unknown", which the queue now
stops on instead of uploading again.

In-memory database, fake uploader, no request reaches YouTube.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app

NOW = datetime(2026, 11, 12, 3, 0)
VIDEO_FILE = "jobs/1/clip_0/final_video.mp4"


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
def _clocks(monkeypatch):
    """The queue mechanics only: the budget, window and same-source guards are off."""
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_DAILY_BUDGET", 0, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_LEAD_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_UPLOAD_LEAD_SECONDS", 21600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RETRY_MINUTES", 30, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_LIMIT_RETRY_HOURS", 6, raising=False)
    monkeypatch.setattr(settings, "MAX_UPLOAD_RETRIES", 3, raising=False)


@pytest.fixture(autouse=True)
def _rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    for index in range(3):
        directory = tmp_path / "jobs" / "1" / f"clip_{index}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "final_video.mp4").write_bytes(b"0" * 16)
    return tmp_path


@pytest.fixture(autouse=True)
def _connected(monkeypatch):
    monkeypatch.setattr("services.youtube_upload.get_youtube_auth_status",
                        lambda: {"authenticated": True, "channel_name": "tester"})


def _clip(db, *, index=0, due=NOW - timedelta(seconds=1), retries=0):
    job = db.query(Job).first()
    if not job:
        job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=False)
        db.add(job)
        db.flush()
    clip = Clip(job_id=job.id, index=index, start=0.0, end=9.0, title=f"Clip {index}",
                description="d", hashtags=["#Shorts"], file_path=VIDEO_FILE,
                uploaded=False, upload_retry_count=retries, publish_due_at=due)
    db.add(clip)
    db.commit()
    return clip


def _attempt_of(db, clip_id):
    return db.execute(text("SELECT publish_attempt_at FROM clips WHERE id=:i"),
                      {"i": clip_id}).scalar()


def _set_attempt(db, clip_id, value):
    db.query(Clip).filter(Clip.id == clip_id).update(
        {"publish_attempt_at": value}, synchronize_session=False)
    db.commit()


class _SessionSpy:
    """Wraps the session so the test sees what the database held at each commit.

    Only a commit can survive the crash this ordering exists for, so the test
    records the marker as it reads back from the row once each commit finished.
    """

    def __init__(self, session, clip_id, events):
        self._session = session
        self._clip_id = clip_id
        self._events = events

    def commit(self):
        self._session.commit()
        self._events.append(("commit", _attempt_of(self._session, self._clip_id)))

    def __getattr__(self, name):
        return getattr(self._session, name)


class _Watcher:
    """The fake uploader: records the row as the database saw it at the call."""

    def __init__(self, session, clip_id, events, fail_with=None):
        self.session = session
        self.clip_id = clip_id
        self.events = events
        self.fail_with = fail_with
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        row = self.session.execute(
            text("SELECT publish_attempt_at, youtube_video_id FROM clips WHERE id=:i"),
            {"i": self.clip_id}).one()
        self.attempt_at_upload = row[0]
        self.video_id_at_upload = row[1]
        self.events.append(("upload", None))
        if self.fail_with:
            raise self.fail_with
        return {"video_id": f"vid{len(self.calls)}",
                "shorts_url": f"https://youtube.com/shorts/vid{len(self.calls)}",
                "title": kwargs.get("title")}


def _first_durable_attempt(events):
    """Index of the first commit that left an attempt marker in the row, if any."""
    for position, (kind, attempt) in enumerate(events):
        if kind == "commit" and attempt is not None:
            return position
    return None


# --------------------------------------------------- the intent lands before the bytes

def test_the_attempt_is_committed_before_the_upload_is_handed_the_clip(db):
    """Everything the queue can promise about a crash is decided by this ordering.

    The marker has to be in the database before the API call, so a process that
    dies with a video id only it knows about leaves a row that says "an upload
    started here and nobody recorded what happened to it".
    """
    clip = _clip(db)
    events = []
    watcher = _Watcher(db, clip.id, events)

    assert worker.publish_queued_clips(_SessionSpy(db, clip.id, events), now=NOW,
                                       uploader=watcher) == 1

    uploaded_at = next(i for i, (kind, _) in enumerate(events) if kind == "upload")
    recorded = _first_durable_attempt(events)
    assert recorded is not None, "no attempt marker ever reached the database"
    assert recorded < uploaded_at, f"the upload ran before the intent was stored: {events}"
    assert watcher.video_id_at_upload is None


def test_a_successful_publish_clears_the_attempt(db):
    """A row that resolved must not stay marked, or the next pass holds a healthy clip."""
    clip = _clip(db)
    events = []
    watcher = _Watcher(db, clip.id, events)

    worker.publish_queued_clips(_SessionSpy(db, clip.id, events), now=NOW, uploader=watcher)

    db.expire_all()
    db.refresh(clip)
    assert clip.publish_attempt_at is None
    assert clip.youtube_video_id == "vid1"
    assert clip.publish_due_at is None


def test_a_failed_publish_clears_the_attempt_so_the_retry_path_stays_normal(db):
    """A refusal from YouTube is an outcome: the clip goes back to waiting, not to holding."""
    clip = _clip(db)
    events = []
    watcher = _Watcher(db, clip.id, events, fail_with=RuntimeError("transient network"))

    assert worker.publish_queued_clips(_SessionSpy(db, clip.id, events), now=NOW,
                                       uploader=watcher) == 0

    db.expire_all()
    db.refresh(clip)
    assert clip.publish_attempt_at is None
    assert clip.upload_retry_count == 1
    assert clip.publish_due_at == NOW + timedelta(minutes=30)


# ---------------------------------------------------------------- what a crash leaves

def test_an_unresolved_attempt_is_never_offered_a_second_upload(db):
    """The window that remains, and the behaviour that makes it survivable.

    The marker is 45 minutes old and there is no video id: either the upload died
    on the way to YouTube, or it reached YouTube and the record did not survive.
    Uploading again is the one action that turns a mystery into a duplicate, so
    the clip stops with a message instead.
    """
    clip = _clip(db)
    _set_attempt(db, clip.id, NOW - timedelta(minutes=45))
    watcher = _Watcher(db, clip.id, [])

    assert worker.publish_queued_clips(db, now=NOW, uploader=watcher) == 0
    assert watcher.calls == [], "a second copy of the same clip would be live on the channel"

    db.expire_all()
    db.refresh(clip)
    message = (clip.upload_error or "").lower()
    assert "may already be on youtube" in message, clip.upload_error
    assert "reconcile_uploads" in message, "the recovery has to be named, not guessed"
    assert "publish_attempt_at" in message, "the row says how to release itself"
    assert clip.publish_due_at is None, "the hold is announced once, not on every pass"
    assert clip.publish_attempt_at == NOW - timedelta(minutes=45), "the evidence stays"


def test_a_second_pass_on_a_held_clip_still_uploads_nothing(db):
    clip = _clip(db)
    _set_attempt(db, clip.id, NOW - timedelta(minutes=45))
    watcher = _Watcher(db, clip.id, [])

    assert worker.publish_queued_clips(db, now=NOW, uploader=watcher) == 0
    assert worker.publish_queued_clips(db, now=NOW + timedelta(hours=3),
                                       uploader=watcher) == 0
    assert watcher.calls == []


def test_an_attempt_that_is_still_running_is_not_offered_to_the_uploader(db):
    """A fresh marker means somebody is mid-upload right now, not that they crashed."""
    clip = _clip(db)
    _set_attempt(db, clip.id, NOW - timedelta(minutes=2))
    watcher = _Watcher(db, clip.id, [])

    assert worker.publish_queued_clips(db, now=NOW, uploader=watcher) == 0
    assert watcher.calls == []

    db.expire_all()
    db.refresh(clip)
    assert clip.publish_due_at is not None, "still in flight: the row keeps its own claim"
    assert "may already be on youtube" not in (clip.upload_error or "").lower()


def test_the_manual_button_refuses_a_clip_with_an_unresolved_attempt(db, client, monkeypatch):
    """The click is the other door to the same upload, so it stops on the same mark."""
    clip = _clip(db, due=None)
    _set_attempt(db, clip.id, datetime.utcnow() - timedelta(minutes=90))

    calls = []
    monkeypatch.setattr("services.youtube_upload.upload_clip_to_youtube",
                        lambda **kwargs: calls.append(kwargs))

    response = client.post(f"/api/clips/{clip.id}/upload-youtube",
                           json={"privacy_status": "public"})

    assert calls == []
    assert response.status_code == 409
    assert "publish_attempt_at" in response.json()["detail"]
