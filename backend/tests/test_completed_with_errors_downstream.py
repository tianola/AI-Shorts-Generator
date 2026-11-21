"""Everything downstream of the new `completed_with_errors` status.

Splitting a partially rendered job out of `completed` is only safe if the places
that treat `completed` as terminal learn about the second finished shape. Two of
them are one line each, and both fail *quietly*: the websocket poller never
stops (it keeps polling a job that is done), and the storage pruner stops
counting those jobs as finished (so their multi-GB sources never get reclaimed
on a machine that runs unattended).

`api/clips.py`'s pending-upload filter also keys off `Job.status == "completed"`,
but that file belongs to another agent. Its behaviour after this change is the
safe direction - a half-rendered job is no longer advertised as publishable by
the legacy list - and the survivors are not stranded, because
`publish_queued_clips` reads `clips.publish_due_at`, not the job status.
"""
import asyncio
import os
import types
from datetime import datetime, timedelta

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import pytest

from api import realtime
from database.models import Job, JobProgress
from database.session import SessionLocal, init_database
from scripts import prune_storage


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(JobProgress).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


class _FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(text)


@pytest.mark.asyncio
async def test_the_websocket_poller_stops_on_a_partially_finished_job(db, monkeypatch,
                                                                     caplog):
    """redis_listener's DB fallback breaks on ('completed', 'failed') only."""
    job = Job(youtube_url="https://youtu.be/x", status="completed_with_errors")
    db.add(job)
    db.commit()

    def no_redis(*a, **k):
        raise OSError("no redis in this test")

    monkeypatch.setattr(realtime.redis, "Redis", no_redis)
    monkeypatch.setattr(realtime, "SessionLocal", lambda: SessionLocal())
    socket = _FakeSocket()

    await asyncio.wait_for(realtime.redis_listener(socket, job.id), timeout=5)

    assert socket.sent, "the final state must still be reported"
    assert "completed_with_errors" in socket.sent[-1]


def test_the_pruner_counts_a_half_rendered_job_as_finished(tmp_path):
    """FINISHED_STATUSES is what releases a job's source.mp4 for reclaim."""
    jobs_dir = str(tmp_path / "jobs")
    old = datetime.utcnow() - timedelta(days=30)
    job = types.SimpleNamespace(id=7, status="completed_with_errors", created_at=old)
    source = tmp_path / "jobs" / "7"
    source.mkdir(parents=True)
    (source / "source.mp4").write_bytes(b"x" * 10)

    items = prune_storage.plan_finished_sources(
        jobs_dir, {7: job}, {}, older_than_days=7, keep_last=0)

    assert [i.path for i in items] == [os.path.join(jobs_dir, "7", "source.mp4")]


def test_finished_and_resumable_statuses_are_one_list():
    """api/jobs.py owns the set of statuses a human may requeue from."""
    from api.jobs import _RESUMABLE_STATUSES

    assert prune_storage.FINISHED_STATUSES <= set(_RESUMABLE_STATUSES)
