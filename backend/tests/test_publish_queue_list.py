"""The Next releases list: the clips waiting for their YouTube release, in release order.

It reads the same rows the worker schedules from, so the list and the channel agree. Runs against
the in-memory database set by conftest; no request reaches YouTube.
"""
from datetime import datetime, timedelta

import pytest

from api.publish_queue import publish_queue
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


def _clip(db, job, index, release=None, due=None, video_id=None, uploaded=False):
    clip = Clip(job_id=job.id, index=index, start=0.0, end=9.0, title=f"Clip {index}",
                file_path="jobs/1/clip_0/final_video.mp4", uploaded=uploaded,
                youtube_video_id=video_id, scheduled_publish_at=release, publish_due_at=due)
    db.add(clip)
    db.flush()
    return clip


def test_the_list_holds_only_waiting_clips_in_release_order(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 3600, raising=False)
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=True)
    db.add(job)
    db.flush()
    t0 = datetime(2026, 11, 24, 9, 0)
    _clip(db, job, 2, release=t0 + timedelta(hours=2), due=t0 + timedelta(hours=1))
    _clip(db, job, 0, release=t0, due=t0 - timedelta(hours=1))
    _clip(db, job, 1, release=t0 + timedelta(hours=1), due=t0)
    _clip(db, job, 3, release=t0 - timedelta(days=1), video_id="abc123")   # already on YouTube
    _clip(db, job, 4)                                                       # never queued
    db.commit()

    body = publish_queue(db=db)

    assert [item["index"] for item in body["items"]] == [0, 1, 2]
    assert body["gap_seconds"] == 3600
    assert body["items"][0]["release_at"] == "2026-11-24T09:00:00Z"
    assert body["items"][0]["upload_at"] == "2026-11-24T08:00:00Z"


def test_an_empty_queue_is_an_empty_list(db):
    assert publish_queue(db=db)["items"] == []


def test_a_backlog_shows_when_each_clip_will_really_go_out(db, monkeypatch):
    """Clips due yesterday do not all go now: one per upload gap after the last publish, and a
    release that has passed moves to just after the upload, as the worker will do it."""
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_LEAD_SECONDS", 900, raising=False)
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=True)
    db.add(job)
    db.flush()
    now = datetime(2026, 11, 26, 17, 20)
    published = _clip(db, job, 9, release=now - timedelta(hours=2), video_id="done1")
    published.published_at = now - timedelta(minutes=5)
    _clip(db, job, 0, release=now - timedelta(days=1), due=now - timedelta(days=1, hours=6))
    _clip(db, job, 1, release=now - timedelta(hours=20), due=now - timedelta(days=1, hours=5))
    _clip(db, job, 2, release=now + timedelta(days=2), due=now + timedelta(days=1))
    db.commit()

    items = publish_queue(db=db, now=now)["items"]

    assert [item["index"] for item in items] == [0, 1, 2]
    assert items[0]["expected_upload_at"] == "2026-11-26T18:15:00Z"   # last publish + 1 h
    assert items[0]["expected_release_at"] == "2026-11-26T18:30:00Z"  # passed release moves
    assert items[1]["expected_upload_at"] == "2026-11-26T19:15:00Z"
    assert items[2]["expected_upload_at"] == "2026-11-27T17:20:00Z"   # its own due time is later
    assert items[2]["expected_release_at"] == "2026-11-28T17:20:00Z"  # a future release is kept
