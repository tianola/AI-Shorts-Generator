"""The stall watchdog must not kill a job that is only waiting for its slot.

`await_for_slot("tiktok")` (worker.py, the per-clip publish step) is a pure
sleep: no `update_progress`, and no CPU while it sleeps. With the shipped pacing
that wait can be `UPLOAD_MIN_GAP_SECONDS` + `UPLOAD_GAP_JITTER_SECONDS` = 4200 s,
against `WORKER_STALL_TIMEOUT_MINUTES=30` / `WORKER_STALL_MIN_CPU_SECONDS=120`.
The watchdog's own rule is "no tick and negligible CPU for the whole window", so
an intentional, bounded wait for the channel's slot is indistinguishable from a
wedge: it marks the job failed and calls `os._exit(1)`, mid-render, and NSSM
brings the worker back to a queue it just broke.

These tests drive a stubbed clock, never the real 30-minute window, and they pin
both halves: the paced wait must survive, and a genuine wedge (no tick, no CPU)
must still be caught. The watchdog's thresholds are untouched.
"""
import os

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import pytest

import worker
from utils import upload_pacing

STALL_SECONDS = int(worker.settings.WORKER_STALL_TIMEOUT_MINUTES) * 60
MIN_CPU_SECONDS = float(worker.settings.WORKER_STALL_MIN_CPU_SECONDS)


class FakeClock:
    """Wall time moves, process CPU does not - exactly a sleeping main thread."""

    def __init__(self):
        self.wall = 1_000_000.0
        self.proc = 10.0
        self.ticks = []

    def time(self):
        return self.wall

    def process_time(self):
        return self.proc

    def advance(self, seconds):
        self.wall += float(seconds)


@pytest.fixture
def clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(worker.time, "time", clock.time)
    monkeypatch.setattr(worker.time, "process_time", clock.process_time)
    worker._CURRENT_JOB[0] = 5
    worker._MAIN_TICK[0] = clock.wall
    worker._MAIN_PROC[0] = clock.proc
    yield clock
    worker._CURRENT_JOB[0] = None


def _watchdog_would_kill(clock):
    return worker._should_stall_exit(
        worker._CURRENT_JOB[0], worker._MAIN_TICK[0], clock.time(),
        STALL_SECONDS, clock.process_time() - worker._MAIN_PROC[0], MIN_CPU_SECONDS)


@pytest.mark.asyncio
async def test_a_full_pacing_gap_is_not_read_as_a_wedge(clock, monkeypatch):
    gap = int(worker.settings.UPLOAD_MIN_GAP_SECONDS) + int(
        worker.settings.UPLOAD_GAP_JITTER_SECONDS)
    assert gap > STALL_SECONDS, "the premise: one wait outlasts the stall window"
    monkeypatch.setattr(upload_pacing, "seconds_to_wait", lambda platform: float(gap))

    async def fake_sleep(step):
        clock.advance(step)

    waited = await upload_pacing.await_for_slot("tiktok", sleeper=fake_sleep)

    assert waited == pytest.approx(gap)
    assert not _watchdog_would_kill(clock), (
        "the watchdog would fail this job and os._exit(1) while it slept")


@pytest.mark.asyncio
async def test_the_paced_wait_ticks_inside_the_stall_window(clock, monkeypatch):
    """It is not enough to tick at the end: the gap between successive liveness
    signals has to stay under the window, or a 70-minute wait still reads as
    wedged at 30."""
    monkeypatch.setattr(upload_pacing, "seconds_to_wait", lambda platform: 4200.0)
    start = worker._MAIN_TICK[0]
    observed = []

    async def fake_sleep(step):
        clock.advance(step)
        observed.append(worker._MAIN_TICK[0])

    await upload_pacing.await_for_slot("tiktok", sleeper=fake_sleep)

    assert observed, "the wait ran without any liveness signal at all"
    gaps = [observed[0] - start] + [b - a for a, b in zip(observed, observed[1:])]
    assert max(gaps) < STALL_SECONDS, f"largest silent gap {max(gaps):.0f}s"
    assert worker._MAIN_TICK[0] == pytest.approx(clock.time())


def test_the_blocking_pacing_wait_ticks_too(clock, monkeypatch):
    """The completion sweep waits through wait_for_slot inside services/tiktok_upload,
    still with _CURRENT_JOB set - the same false kill, one module over."""
    monkeypatch.setattr(upload_pacing, "seconds_to_wait", lambda platform: 4200.0)

    def fake_sleep(step):
        clock.advance(step)

    waited = upload_pacing.wait_for_slot("tiktok", sleeper=fake_sleep)

    assert waited == pytest.approx(4200.0)
    assert not _watchdog_would_kill(clock)


def test_a_genuine_wedge_is_still_a_wedge(clock):
    """The fix must not blind the watchdog: no tick, no CPU, past the window."""
    clock.advance(STALL_SECONDS + 1)

    assert _watchdog_would_kill(clock) is True


def test_a_busy_long_stage_is_still_spared(clock):
    clock.advance(STALL_SECONDS + 1)
    clock.proc += MIN_CPU_SECONDS + 1

    assert _watchdog_would_kill(clock) is False


def test_worker_registers_a_pacing_liveness_hook():
    """The wiring the two tests above depend on, pinned so it cannot rot."""
    assert upload_pacing.get_liveness_hook() is worker._touch_main_tick
