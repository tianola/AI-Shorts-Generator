"""The stall watchdog: a worker whose main thread is wedged must fail the job and
exit so NSSM relaunches it, instead of holding the lease forever and freezing the
queue silently (the job-99 failure mode).

The exit decision is a pure function so it can be tested without threads, a real
clock, or os._exit.
"""
import time

import worker


def test_idle_worker_never_stalls():
    # No job in flight: sleeping between polls is healthy, not a wedge.
    assert worker._should_stall_exit(
        current_job_id=None, last_tick=time.time() - 99999, now=time.time(),
        timeout_seconds=60) is False


def test_active_job_with_recent_tick_is_alive():
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 10, now=now, timeout_seconds=60) is False


def test_active_job_with_stale_tick_is_stalled():
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 61, now=now, timeout_seconds=60) is True


def test_update_progress_bumps_the_main_tick():
    # A stage boundary must move the liveness marker the watchdog reads, otherwise a
    # healthy long stage would be mistaken for a wedge.
    worker._CURRENT_JOB[0] = 42
    worker._MAIN_TICK[0] = 0.0
    db_stub = _FakeDB()
    update = worker.make_progress_writer(db_stub, _FakeJob(), 42)
    update("Transcribing audio (0%)...", 25)
    assert worker._MAIN_TICK[0] > 0.0
    assert worker._MAIN_TICK[0] >= time.time() - 2
    worker._CURRENT_JOB[0] = None


class _FakeJob:
    claimed_at = None


class _FakeProgress:
    current_stage = ""
    progress_percent = 0


class _FakeDB:
    def query(self, *a, **k):
        q = self
        q.filter = lambda *a, **k: q
        q.first = lambda: _FakeProgress()
        return q

    def add(self, *a, **k):
        pass

    def commit(self):
        pass
