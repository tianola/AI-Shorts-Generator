"""Spacing between publishes, so ten rendered clips do not leave the channel
as ten uploads in four minutes.

The pacing clock is per process and per platform: it remembers when this
process last finished an upload. Two workers on the same machine therefore do
not pace against each other, which is why the worker keeps one process per
channel unless a job is expected to publish in parallel.
"""
import asyncio
import random
import threading
import time
from typing import Any, Callable, Optional

from config import settings

_lock = threading.Lock()
_last_upload: dict[str, float] = {}


def reset(platform: Optional[str] = None) -> None:
    """Forget the spacing history. Tests and a deliberate re-run use this."""
    with _lock:
        if platform is None:
            _last_upload.clear()
        else:
            _last_upload.pop(platform, None)


def note_upload(platform: str, when: Optional[float] = None) -> None:
    """Record that an upload finished at ``when`` (defaults to now)."""
    with _lock:
        _last_upload[platform] = time.monotonic() if when is None else when


def seconds_to_wait(platform: str) -> float:
    """How long this platform still owes before the next publish is plausible.

    The gap is re-rolled per call, and time already spent rendering the next
    clip counts against it: a clip that took four minutes to render needs only
    about eleven more minutes of a fifteen-minute gap, so a slow render does
    not turn into a two-hour job.
    """
    gap = max(0, int(settings.UPLOAD_MIN_GAP_SECONDS))
    if gap == 0:
        return 0.0
    jitter = max(0, int(settings.UPLOAD_GAP_JITTER_SECONDS))
    with _lock:
        last = _last_upload.get(platform)
    if last is None:
        return 0.0
    target = gap + (random.uniform(0, jitter) if jitter else 0.0)
    return max(0.0, target - (time.monotonic() - last))


def _sleep_chunks(seconds: float, chunk: float = 30.0):
    remaining = seconds
    while remaining > 0:
        step = min(chunk, remaining)
        yield step
        remaining -= step


def wait_for_slot(
    platform: str,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    stop: Optional[threading.Event] = None,
) -> float:
    """Blocking wait. Returns the seconds actually slept.

    Sleeping in chunks so a lost lease (``stop`` set by the heartbeat) ends
    the wait instead of holding a job that another worker already reclaimed.
    """
    waited = 0.0
    for step in _sleep_chunks(seconds_to_wait(platform)):
        if stop is not None and stop.is_set():
            break
        sleeper(step)
        waited += step
    return waited


async def await_for_slot(
    platform: str,
    *,
    stop: Optional[threading.Event] = None,
    sleeper: Optional[Callable[[float], Any]] = None,
) -> float:
    """``await``-friendly :func:`wait_for_slot`, for the async worker stages.

    ``sleeper`` exists so the test suite can drive a nine-hundred-second gap
    without pytest-asyncio or a real wait.
    """
    sleep = sleeper or asyncio.sleep
    waited = 0.0
    for step in _sleep_chunks(seconds_to_wait(platform)):
        if stop is not None and stop.is_set():
            break
        await sleep(step)
        waited += step
    return waited
