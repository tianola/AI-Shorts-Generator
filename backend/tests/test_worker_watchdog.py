"""The stall watchdog: a worker whose main thread is genuinely wedged (0% CPU)
must fail the job and exit so NSSM relaunches it, instead of holding the lease
forever and freezing the queue silently (the job-99 failure mode).

The exit decision is a pure function so it can be tested without threads, a real
clock, or os._exit.

The liveness signal is twofold: a recent progress tick OR real process CPU burned
since the last tick. A long compute stage (pyannote diarization on an hour of
audio, YOLO temporal localization) produces no tick for well over the timeout yet
keeps crunching CPU - that is working, not wedged, and must not be killed.
"""
import time

import worker


def test_idle_worker_never_stalls():
    # No job in flight: sleeping between polls is healthy, not a wedge.
    assert worker._should_stall_exit(
        current_job_id=None, last_tick=time.time() - 99999, now=time.time(),
        timeout_seconds=60, cpu_seconds=0.0) is False


def test_active_job_with_recent_tick_is_alive():
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 10, now=now, timeout_seconds=60,
        cpu_seconds=0.0) is False


def test_active_job_stale_tick_and_no_cpu_is_wedged():
    # The job-99 signature: past the timeout, no progress, and the process burned
    # negligible CPU - the main thread is blocked on a lock/hung call.
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 61, now=now, timeout_seconds=60,
        cpu_seconds=0.0, min_cpu_seconds=30) is True


def test_active_job_stale_tick_but_high_cpu_is_a_long_stage_not_stalled():
    # A legitimately slow CPU-bound stage (long-source diarization) has no tick
    # past the window but has burned real CPU - it must NOT be killed. This is the
    # false positive that failed job 104 (58-minute source).
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 1801, now=now, timeout_seconds=60,
        cpu_seconds=1500.0, min_cpu_seconds=30) is False


def test_active_job_stale_tick_below_cpu_floor_is_still_stalled():
    # Just under the floor is treated as idle/blocking, not as productive work.
    now = time.time()
    assert worker._should_stall_exit(
        current_job_id=5, last_tick=now - 1801, now=now, timeout_seconds=60,
        cpu_seconds=29.0, min_cpu_seconds=30) is True


def test_update_progress_bumps_both_liveness_baselines():
    # A stage boundary must move both the wall tick and the process-CPU baseline the
    # watchdog measures against, otherwise a healthy long stage's CPU since the last
    # tick is computed from a stale origin.
    worker._CURRENT_JOB[0] = 42
    worker._MAIN_TICK[0] = 0.0
    worker._MAIN_PROC[0] = -1.0
    db_stub = _FakeDB()
    update = worker.make_progress_writer(db_stub, _FakeJob(), 42)
    update("Transcribing audio (0%)...", 25)
    assert worker._MAIN_TICK[0] > 0.0
    assert worker._MAIN_TICK[0] >= time.time() - 2
    assert worker._MAIN_PROC[0] >= time.process_time() - 2
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
