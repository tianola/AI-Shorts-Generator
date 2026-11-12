"""One failed publish costs one strike, and only the queue charges it.

`MAX_UPLOAD_RETRIES=3` is the budget, and `worker._record_publish_failure` is
what spends it: a clip that fails three times loses its release slot
(`publish_due_at`/`scheduled_publish_at` go null) and leaves the queue for a
human. `PATCH /api/clips/{id}/upload-status` was charging a strike for the very
same failure from the other side - the Studio panel reports the outcome of a
publish it asked for, the queue already recorded it - so a clip retired after
about 1.5 real attempts and dropped off the schedule quietly.

Since bcd651d the manual publish endpoints go through
`worker.publish_clip_for_operator` -> `_ship_clip_to_youtube` ->
`_record_publish_failure`, which also forgives an `uploadLimitExceeded` refusal
(a channel budget, not a clip fault) and clears both clocks on success. The
endpoint therefore has nothing left to decide: it records what the caller saw,
the queue decides what it costs.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app

NOW = datetime(2026, 11, 12, 3, 0)
VALID_ID = "abcdefghijk"


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


@pytest.fixture
def clip(db):
    job = Job(youtube_url="https://youtu.be/strikes", status="completed")
    db.add(job)
    db.commit()
    db.refresh(job)
    row = Clip(job_id=job.id, index=0, start=0.0, end=20.0, title="Queued",
               uploaded=False, upload_retry_count=0,
               publish_due_at=NOW + timedelta(minutes=30),
               scheduled_publish_at=NOW + timedelta(hours=1),
               publish_attempt_at=None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_reporting_a_failure_does_not_charge_a_strike(client, db, clip):
    response = client.patch(f"/api/clips/{clip.id}/upload-status",
                            json={"uploaded": False, "upload_error": "403 forbidden"})

    assert response.status_code == 200, response.text
    db.refresh(clip)
    assert clip.upload_retry_count == 0, (
        "the endpoint spent a strike the queue also spends: one failed publish "
        "came out of MAX_UPLOAD_RETRIES twice")
    assert clip.upload_error == "403 forbidden"
    assert clip.uploaded is False


def test_one_failed_publish_costs_exactly_one_strike(client, db, clip):
    """The real sequence: the queue charges, then the panel reports the same thing."""
    worker._record_publish_failure(db, clip, NOW, "upload refused")
    db.refresh(clip)
    assert clip.upload_retry_count == 1

    response = client.patch(f"/api/clips/{clip.id}/upload-status",
                            json={"uploaded": False, "upload_error": "upload refused"})

    assert response.status_code == 200, response.text
    db.refresh(clip)
    assert clip.upload_retry_count == 1, (
        f"one failed attempt charged {clip.upload_retry_count} strikes; with "
        f"MAX_UPLOAD_RETRIES={settings.MAX_UPLOAD_RETRIES} the clip retires early "
        "and loses its release slot")


def test_the_queue_still_owns_retirement(client, db, clip):
    """Retiring a clip must stay possible from the queue, so removing the
    endpoint's increment cannot turn a permanent-failure clip into an eternal one."""
    assert settings.MAX_UPLOAD_RETRIES == 3

    for attempt in range(settings.MAX_UPLOAD_RETRIES):
        worker._record_publish_failure(db, clip, NOW + timedelta(minutes=attempt),
                                       f"failure {attempt}")
    db.refresh(clip)

    assert clip.upload_retry_count == 3
    assert clip.publish_due_at is None
    assert clip.scheduled_publish_at is None


def test_reporting_success_clears_the_publish_clocks(client, db, clip):
    """A row that says "published" and "a publish is due" at once is the clip-461
    phantom: every consumer of publish_due_at reads it as work still to do."""
    clip.publish_attempt_at = NOW
    db.commit()

    response = client.patch(f"/api/clips/{clip.id}/upload-status",
                            json={"uploaded": True, "youtube_video_id": VALID_ID})

    assert response.status_code == 200, response.text
    db.refresh(clip)
    assert clip.uploaded is True
    assert clip.youtube_video_id == VALID_ID
    assert clip.published_at is not None
    assert clip.publish_due_at is None
    assert clip.publish_attempt_at is None
    assert clip.upload_error is None


def test_reporting_a_failure_leaves_the_clip_queued(client, db, clip):
    due = clip.publish_due_at
    response = client.patch(f"/api/clips/{clip.id}/upload-status",
                            json={"uploaded": False, "upload_error": "network down"})

    assert response.status_code == 200, response.text
    db.refresh(clip)
    assert clip.publish_due_at == due, (
        "the endpoint must not touch the queue's clocks: a failure reported by a "
        "caller is not the queue's decision to retry or retire")
