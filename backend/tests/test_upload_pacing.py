"""Upload pacing: the first publish is immediate, the rest are spaced.

The point of the module is that a rendered job does not leave the channel as
ten uploads in four minutes, so the tests pin the arithmetic (gap minus time
already spent rendering), the disable path, and the lease-abort.
"""
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from config import settings
from utils import upload_pacing as pacing


@pytest.fixture(autouse=True)
def _clean_clock(monkeypatch):
    pacing.reset()
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    yield
    pacing.reset()


def test_first_publish_never_waits():
    assert pacing.seconds_to_wait("youtube") == 0.0


def test_gap_is_measured_from_the_last_publish():
    pacing.note_upload("youtube")
    assert pacing.seconds_to_wait("youtube") == pytest.approx(900, abs=1)


def test_time_already_spent_rendering_counts_against_the_gap():
    """A clip that took four minutes to render owes about eleven more."""
    pacing.note_upload("youtube", when=time.monotonic() - 240)
    assert pacing.seconds_to_wait("youtube") == pytest.approx(660, abs=1)


def test_platforms_have_separate_clocks():
    pacing.note_upload("youtube")
    assert pacing.seconds_to_wait("tiktok") == 0.0


def test_zero_gap_disables_pacing(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 0)
    pacing.note_upload("youtube")
    assert pacing.seconds_to_wait("youtube") == 0.0


def test_jitter_stays_inside_its_band(monkeypatch):
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 300)
    pacing.note_upload("youtube")
    for _ in range(50):
        waited = pacing.seconds_to_wait("youtube")
        assert 900 <= waited <= 1200


def test_wait_for_slot_sleeps_in_chunks_and_returns_the_total():
    pacing.note_upload("youtube", when=time.monotonic() - 800)
    slept = []
    waited = pacing.wait_for_slot("youtube", sleeper=slept.append)
    assert waited == pytest.approx(100, abs=1)
    assert sum(slept) == pytest.approx(100, abs=1)
    assert max(slept) <= 30  # chunked, never one uninterruptible sleep


def test_a_lost_lease_cuts_the_wait_short():
    stop = threading.Event()
    stop.set()
    pacing.note_upload("youtube")
    slept = []
    assert pacing.wait_for_slot("youtube", sleeper=slept.append, stop=stop) == 0.0


def test_nothing_is_slept_when_the_slot_is_already_free():
    pacing.note_upload("youtube", when=time.monotonic() - 5000)
    slept = []
    assert pacing.wait_for_slot("youtube", sleeper=slept.append) == 0.0
    assert slept == []


def test_await_for_slot_is_paced_without_blocking_the_loop():
    """The worker's per-clip path is async; drive the coroutine directly."""
    import asyncio

    slept = []

    async def fake_sleep(step):
        slept.append(step)

    async def drive():
        pacing.note_upload("youtube")
        return await pacing.await_for_slot("youtube", sleeper=fake_sleep)

    waited = asyncio.run(drive())
    assert waited == pytest.approx(900, abs=1)
    assert sum(slept) == pytest.approx(900, abs=1)
    assert max(slept) <= 30


def test_await_for_slot_stops_when_the_lease_is_lost():
    import asyncio

    stop = threading.Event()
    stop.set()
    pacing.note_upload("youtube")
    assert asyncio.run(pacing.await_for_slot("youtube", stop=stop)) == 0.0
