"""Restarting a job the worker still owns is how two workers ended up on one job.

`start_job` and `resume_job` both look at the status before touching the row.
`restart_job` did not: it deleted the job directory and wrote status='created'
while the running worker kept `claimed_by`, so its heartbeat (`touch_lease` keys
on that column) went on refreshing a lease that protected nothing, the poller
found a 'created' row, `claim_job` won the CAS, and a second worker started
rendering into the directory the first one was still reading. job 101 was
claimed four times in two hours that way (`Lease no longer names
thanasys:30240` at 03:10:22, `Clip 1/2 saved to DB with ID 399` twice), and the
row still carries ' [lease expired, requeued]'.

The guard is only half of it: every path that pushes a job back to 'created'
must also drop the claim, or the previous worker's heartbeat keeps the row
looking alive to `reclaim_expired_leases` while a new worker already runs it.
"""
import os
from datetime import datetime, timedelta

os.environ["TEST_DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from config import settings
from database.base import Base
from database.models import Job
from database.session import SessionLocal, engine
from main import app

Base.metadata.create_all(bind=engine)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.query(Job).delete()
        session.commit()
        session.close()


def _row(job_id):
    """Read past any session's identity map, the way a second worker sees it."""
    probe = SessionLocal()
    try:
        return probe.execute(
            text("SELECT status, claimed_by, claimed_at, error_message "
                 "FROM jobs WHERE id=:jid"),
            {"jid": job_id},
        ).one()
    finally:
        probe.close()


def _job(db, status="processing", claimed_by="thanasys:30240",
         claimed_at=None, tmp_path=None, artifacts=True):
    job = Job(youtube_url="https://youtu.be/x", status=status,
              claimed_by=claimed_by,
              claimed_at=claimed_at if claimed_at is not None else datetime.utcnow())
    db.add(job)
    db.commit()
    job_dir = os.path.join(settings.JOBS_DIR, str(job.id))
    os.makedirs(job_dir, exist_ok=True)
    if artifacts:
        with open(os.path.join(job_dir, "source.mp4"), "wb") as fh:
            fh.write(b"not-a-real-video")
    return job.id, job_dir


def test_restart_refuses_a_job_a_live_worker_holds(client, db):
    job_id, _ = _job(db)

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 409, response.text
    assert "is being processed" in response.text.lower()
    assert "thanasys:30240" in response.text


def test_refused_restart_leaves_the_rendered_artifacts_alone(client, db):
    """The deletion is the damage: an in-flight ffmpeg reads from that dir."""
    job_id, job_dir = _job(db)
    before = sorted(os.listdir(job_dir))

    client.post(f"/api/jobs/{job_id}/restart")

    assert sorted(os.listdir(job_dir)) == before
    status_, claimed_by, _, _ = _row(job_id)
    assert (status_, claimed_by) == ("processing", "thanasys:30240")


def test_restart_clears_the_claim_so_the_old_heartbeat_dies(client, db):
    """A reset row that still names a worker keeps its lease alive."""
    job_id, _ = _job(db, status="failed", claimed_at=datetime.utcnow() - timedelta(hours=3))

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 200, response.text
    status_, claimed_by, claimed_at, error = _row(job_id)
    assert status_ == "created"
    assert claimed_by is None and claimed_at is None
    assert error is None


def test_restart_of_an_abandoned_processing_job_is_allowed(client, db):
    """status='processing' with an expired lease means nobody is working on it."""
    stale = datetime.utcnow() - timedelta(minutes=int(settings.JOB_LEASE_MINUTES) + 1)
    job_id, _ = _job(db, status="processing", claimed_at=stale)

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 200, response.text
    status_, claimed_by, claimed_at, _ = _row(job_id)
    assert status_ == "created"
    assert claimed_by is None and claimed_at is None


def test_restart_still_wipes_the_directory_and_keeps_the_upload_source(client, db):
    job_id, job_dir = _job(db, status="failed", claimed_by=None, claimed_at=None)
    with open(os.path.join(job_dir, "segments.json"), "w", encoding="utf-8") as fh:
        fh.write("[]")

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 200, response.text
    assert not os.path.exists(os.path.join(job_dir, "segments.json"))


def test_restart_preserves_an_upload_source_across_the_cleanup(client, db):
    job_id, job_dir = _job(db, status="failed", claimed_by=None, claimed_at=None)
    db.query(Job).filter(Job.id == job_id).update({"youtube_url": "upload://clip.mp4"})
    db.commit()

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 200, response.text
    assert os.path.exists(os.path.join(job_dir, "source.mp4"))


@pytest.mark.parametrize("endpoint", ["start", "resume"])
def test_requeue_endpoints_do_not_leave_a_claim_behind(client, db, endpoint):
    """Same column, same bug: a queued row that names a worker heartbeats."""
    job_id, _ = _job(db, status="failed", claimed_at=datetime.utcnow() - timedelta(hours=3))

    response = client.post(f"/api/jobs/{job_id}/{endpoint}")

    assert response.status_code == 200, response.text
    status_, claimed_by, claimed_at, _ = _row(job_id)
    assert status_ == "created"
    assert claimed_by is None and claimed_at is None


def test_resume_accepts_a_partially_failed_job(client, db):
    """The new status is terminal, so Resume must be able to pick it up.

    A job that is not 'completed' is only a safe label if every path that used
    to close it out still reaches it; otherwise the operator has no way back.
    """
    job_id, _ = _job(db, status="completed_with_errors", claimed_by=None,
                     claimed_at=None)

    response = client.post(f"/api/jobs/{job_id}/resume")

    assert response.status_code == 200, response.text
    status_, _, _, _ = _row(job_id)
    assert status_ == "created"


def test_restart_accepts_a_partially_failed_job(client, db):
    job_id, _ = _job(db, status="completed_with_errors", claimed_by=None,
                     claimed_at=None)

    response = client.post(f"/api/jobs/{job_id}/restart")

    assert response.status_code == 200, response.text
    status_, _, _, _ = _row(job_id)
    assert status_ == "created"
