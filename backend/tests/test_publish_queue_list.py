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
