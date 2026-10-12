"""Two workers against one queue: who wins a job, and what a dead lease costs.

The claim is a compare-and-swap and the lease is what recovers a job whose worker
died. Neither had a test, and running more than one worker is exactly the case
where getting either wrong renders and publishes the same short twice.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from config import settings
from database.models import Job
from database.session import SessionLocal, init_database
import worker


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _isolated_worker_id(monkeypatch):
    """WORKER_ID is a module global built from hostname:pid."""
    monkeypatch.setattr(worker, "WORKER_ID", "alpha:1")


def _naive_now() -> datetime:
    """Naive UTC, the way the models and the lease SQL store it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _queue_job(db, status="created", claimed_by=None, claimed_at=None, url="https://youtu.be/x"):
    job = Job(youtube_url=url, status=status,
              claimed_by=claimed_by, claimed_at=claimed_at)
    db.add(job)
    db.commit()
    return job.id


def _row(job_id):
    """Read straight from the database, past any session's identity map."""
    probe = SessionLocal()
    try:
        return probe.execute(
            text("SELECT status, claimed_by, claimed_at, error_message "
                 "FROM jobs WHERE id=:jid"),
            {"jid": job_id},
        ).one()
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# claim_job()
# ---------------------------------------------------------------------------

def test_only_one_worker_claims_a_queued_job(db):
    job_id = _queue_job(db)
    rival = SessionLocal()
    try:
        assert worker.claim_job(db, job_id) is True
        worker.WORKER_ID = "beta:2"
        assert worker.claim_job(rival, job_id) is False
    finally:
        rival.close()

    row = _row(job_id)
    assert row.status == "processing"
    assert row.claimed_by == "alpha:1"
    assert row.claimed_at is not None


def test_a_running_job_cannot_be_claimed_by_another_worker(db):
    """The loser of the race must move on, not start a second render."""
    job_id = _queue_job(db, status="processing", claimed_by="alpha:1",
                        claimed_at=_naive_now())
    assert worker.claim_job(db, job_id) is False
    assert _row(job_id).claimed_by == "alpha:1"


@pytest.mark.parametrize("status", ["completed", "failed"])
def test_a_finished_job_is_not_claimable(db, status):
    job_id = _queue_job(db, status=status)
    assert worker.claim_job(db, job_id) is False
    assert _row(job_id).status == status


def test_two_workers_split_two_queued_jobs(db):
    first = _queue_job(db, url="https://youtu.be/one")
    second = _queue_job(db, url="https://youtu.be/two")

    assert worker.claim_job(db, first) is True
    worker.WORKER_ID = "beta:2"
    assert worker.claim_job(db, second) is True

    assert _row(first).claimed_by == "alpha:1"
    assert _row(second).claimed_by == "beta:2"


# ---------------------------------------------------------------------------
# reclaim_expired_leases()
# ---------------------------------------------------------------------------

def test_an_expired_lease_goes_back_to_the_queue(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 30)
    stale = _naive_now() - timedelta(minutes=31)
    job_id = _queue_job(db, status="processing", claimed_by="dead:1", claimed_at=stale)

    assert worker.reclaim_expired_leases(db) == 1

    row = _row(job_id)
    assert row.status == "created"
    assert row.claimed_by is None
    assert row.claimed_at is None
    assert "lease expired" in row.error_message


def test_a_lease_with_no_timestamp_is_abandoned(db):
    """Crashed between claiming and the first heartbeat: claimed_at is still NULL."""
    job_id = _queue_job(db, status="processing", claimed_by="dead:1")
    assert worker.reclaim_expired_leases(db) == 1
    assert _row(job_id).status == "created"


def test_a_live_lease_is_left_alone(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 30)
    job_id = _queue_job(db, status="processing", claimed_by="alpha:1",
                        claimed_at=_naive_now() - timedelta(minutes=5))

    assert worker.reclaim_expired_leases(db) == 0

    row = _row(job_id)
    assert row.status == "processing"
    assert row.claimed_by == "alpha:1"


def test_completed_jobs_are_never_requeued(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 30)
    job_id = _queue_job(db, status="completed", claimed_by="alpha:1",
                        claimed_at=_naive_now() - timedelta(days=2))
    assert worker.reclaim_expired_leases(db) == 0
    assert _row(job_id).status == "completed"


def test_a_requeued_job_can_be_claimed_by_the_surviving_worker(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 30)
    job_id = _queue_job(db, status="processing", claimed_by="dead:1",
                        claimed_at=_naive_now() - timedelta(minutes=45))

    worker.reclaim_expired_leases(db)
    assert worker.claim_job(db, job_id) is True
    assert _row(job_id).claimed_by == "alpha:1"


# ---------------------------------------------------------------------------
# touch_lease() - what keeps a long stage alive
# ---------------------------------------------------------------------------

def test_heartbeat_refresh_keeps_ownership_past_the_lease(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 1)
    job_id = _queue_job(db, status="processing", claimed_by="alpha:1",
                        claimed_at=_naive_now() - timedelta(minutes=5))

    assert worker.touch_lease(db, job_id) == 1
    assert worker.reclaim_expired_leases(db) == 0
    assert _row(job_id).status == "processing"


def test_touch_lease_does_not_extend_a_lease_another_worker_holds(db, monkeypatch):
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 1)
    job_id = _queue_job(db, status="processing", claimed_by="beta:2",
                        claimed_at=_naive_now() - timedelta(minutes=5))

    assert worker.touch_lease(db, job_id) == 0
    assert _row(job_id).claimed_by == "beta:2"


def test_touch_lease_reports_a_reclaimed_job(db, monkeypatch):
    """The heartbeat's signal to stop: the row no longer names this worker."""
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 1)
    job_id = _queue_job(db, status="processing", claimed_by="alpha:1",
                        claimed_at=_naive_now() - timedelta(minutes=5))

    worker.reclaim_expired_leases(db)
    assert worker.touch_lease(db, job_id) == 0


def test_touch_lease_only_moves_the_timestamp(db, monkeypatch):
    """A finished job still names its worker, so a late heartbeat can refresh it -
    but the reclaim only looks at 'processing', so nothing is resurrected."""
    monkeypatch.setattr(settings, "JOB_LEASE_MINUTES", 1)
    job_id = _queue_job(db, status="completed", claimed_by="alpha:1",
                        claimed_at=_naive_now() - timedelta(minutes=5))

    assert worker.touch_lease(db, job_id) == 1
    assert worker.reclaim_expired_leases(db) == 0
    assert _row(job_id).status == "completed"


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def test_arq_concurrency_comes_from_settings():
    assert worker.WorkerSettings.max_jobs == settings.WORKER_MAX_JOBS
    assert worker.WorkerSettings.max_jobs >= 1
