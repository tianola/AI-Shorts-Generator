"""Tests for services/audio_energy_scorer.py.

The bug these lock down: both ffmpeg passes used to decode the ENTIRE source
with a hardcoded ``timeout=30``.  On a long source (job 110 - 3541 s, 900
segments) both passes expired, the outer ``except Exception`` fabricated
``[0.3] * num_segments``, and the stage still logged success.  A constant
series has coefficient of variation 0, so the audio-dynamics leg of the
virality composite died on every clip while nothing in the log said so.

Three contracts asserted here:

1. bounded work - no single ffmpeg invocation decodes the whole timeline, and
   every audio read is video-less, mono and sample-rate capped;
2. duration-scaled timeout - a slow-but-healthy pass is not killed, a hang is
   not allowed to stall the worker forever;
3. honesty - a measurement that genuinely failed is an exception, never a
   flat list that looks like a real flat band.

``subprocess.run`` is monkeypatched for the logic tests, so no ffmpeg binary is
required; the one real-media test is marked ``FFMPEG_REQUIRED`` and skips when
the job-110 source is absent.
"""
import json
import math
import os
import shutil
import subprocess
import time

import numpy as np
import pytest
from services import audio_energy_scorer as aes

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REAL_SOURCE = os.path.join(REPO_ROOT, "storage", "jobs", "110", "source.mp4")
LONG_DURATION = 3541.414  # what ffprobe reports for storage/jobs/110/source.mp4
SEGMENT_DURATION = 2.0


# --------------------------------------------------------------------------
# fake ffmpeg/ffprobe ------------------------------------------------------
# --------------------------------------------------------------------------

def _pcm_for_window(start, length, sample_rate, amplitude_for):
    """int16 mono bytes for [start, start+length) with a per-second amplitude law."""
    n = round(length * sample_rate)
    if n <= 0:
        return b""
    idx = np.arange(n, dtype=np.float64)
    times = start + idx / float(sample_rate)
    amps = np.asarray(amplitude_for(times), dtype=np.float64) * np.ones(n)
    # a steady ~120 Hz sine; its RMS is amplitude/sqrt(2) whatever the window
    phase = 2 * math.pi * 120.0 * (idx / float(sample_rate))
    samples = np.sin(phase) * amps
    return (samples * 32767.0).astype("<i2").tobytes()


class Completed:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def flat_amplitude(_t):
    return 0.2


def varying_amplitude(t):
    """loud / quiet alternation every 100 s: a real dynamic band (vectorized)."""
    second = np.floor(np.asarray(t, dtype=np.float64)).astype(np.int64)
    return np.where((second // 100) % 2 == 0, 0.6, 0.03)


class FakeFfmpeg:
    """Stand-in for the ``subprocess`` module attribute inside the scorer.

    Records every invocation, answers the two ffprobe queries, and streams PCM
    for each audio window it is asked for.  ``fail_window`` (by -ss value) and
    ``fail_all_audio`` simulate a broken or hung decoder without a real binary.
    """

    TimeoutExpired = subprocess.TimeoutExpired  # the fake stands in for the module

    def __init__(self, duration=LONG_DURATION, has_audio=True, amplitude=varying_amplitude,
                 fail_windows=(), fail_all_audio=False, timeout_side_effect=False):
        self.duration = duration
        self.has_audio = has_audio
        self.amplitude = amplitude
        self.fail_windows = {round(float(w), 3) for w in fail_windows}
        self.fail_all_audio = fail_all_audio
        self.timeout_side_effect = timeout_side_effect
        self.calls = []

    # --- subprocess.run -----------------------------------------------------
    def run(self, cmd, *args, **kwargs):
        self.calls.append({"cmd": list(cmd), "kwargs": kwargs})
        argv = list(cmd)
        if argv[0] == "ffprobe":
            joined = " ".join(argv)
            if "codec_type" in joined:
                streams = [{"codec_type": "audio"}] if self.has_audio else []
                return Completed(stdout=json.dumps({"streams": streams}))
            if "format=duration" in joined:
                return Completed(stdout=f"{self.duration}\n")
            return Completed(stderr="unhandled probe", returncode=1)

        if argv[0] == "ffmpeg":
            if self.timeout_side_effect:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
            if self.fail_all_audio:
                # bytes, because the PCM read is not opened in text mode
                return Completed(stderr=b"decoder blew up", returncode=1)
            start = float(argv[argv.index("-ss") + 1])
            if round(start, 3) in self.fail_windows:
                return Completed(stderr=b"window unreadable", returncode=1)
            length = float(argv[argv.index("-t") + 1])
            rate = int(argv[argv.index("-ar") + 1])
            return Completed(stdout=_pcm_for_window(start, length, rate, self.amplitude))
        raise AssertionError(f"unexpected invocation: {argv}")

    # --- helpers for the assertions ----------------------------------------
    def audio_reads(self):
        """ffmpeg invocations that actually decode audio."""
        return [c["cmd"] for c in self.calls if c["cmd"][0] == "ffmpeg"]

    def timeouts(self):
        return [c["kwargs"].get("timeout") for c in self.calls]


def _install(monkeypatch, fake):
    monkeypatch.setattr(aes, "subprocess", fake)
    return fake


def _segments(count=3, span=60.0):
    return [{"start": i * span, "end": (i + 1) * span, "text": f"seg {i}"} for i in range(count)]


# --------------------------------------------------------------------------
# 1. bounded work ----------------------------------------------------------
# --------------------------------------------------------------------------

def test_long_source_is_never_decoded_in_one_full_pass(monkeypatch):
    """No ffmpeg invocation may be asked to read the whole 3541 s timeline."""
    fake = _install(monkeypatch, FakeFfmpeg())
    aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)

    reads = fake.audio_reads()
    assert reads, "expected at least one ffmpeg audio read"
    for cmd in reads:
        assert "-ss" in cmd, f"audio read is not seek-bounded: {cmd}"
        assert "-t" in cmd, f"audio read has no length bound: {cmd}"
        window = float(cmd[cmd.index("-t") + 1])
        assert window <= aes.MAX_CHUNK_SECONDS + 1e-6, (
            f"single ffmpeg read asked for {window}s of audio "
            f"(cap {aes.MAX_CHUNK_SECONDS})")
    # -ss must come before -i, otherwise the seek is an output seek = full decode
    for cmd in reads:
        assert cmd.index("-ss") < cmd.index("-i"), f"seek placed after input: {cmd}"
    total = sum(float(c[c.index("-t") + 1]) for c in reads)
    assert total <= LONG_DURATION + aes.MAX_CHUNK_SECONDS, (
        f"windows overlap: {total}s of audio read for a {LONG_DURATION}s source")


def test_audio_reads_skip_video_and_stay_mono_low_rate(monkeypatch):
    """The cheap read is what made the 83s volumedetect pass unnecessary."""
    fake = _install(monkeypatch, FakeFfmpeg())
    aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)

    for cmd in fake.audio_reads():
        assert "-vn" in cmd, f"video frames would be decoded: {cmd}"
        assert cmd[cmd.index("-ac") + 1] == "1"
        assert int(cmd[cmd.index("-ar") + 1]) <= aes.AUDIO_SAMPLE_RATE
        assert "-i" in cmd and cmd[cmd.index("-i") + 1] == "/fake/source.mp4"
        assert "volumedetect" not in " ".join(cmd), "video-decoding fallback is back"
        assert "amix" not in " ".join(cmd), (
            "amix in a single-input filtergraph is what made the astats pass "
            "exit instantly with rc=-22")


def test_short_source_still_measured_with_a_bounded_window(monkeypatch):
    fake = _install(monkeypatch, FakeFfmpeg(duration=6.0))
    values = aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=1.0)
    assert len(values) == 6
    assert fake.audio_reads() and all("-t" in c for c in fake.audio_reads())


# --------------------------------------------------------------------------
# 2. timeout derived from the work -----------------------------------------
# --------------------------------------------------------------------------

def test_timeout_scales_with_the_audio_being_decoded():
    short = aes.ffmpeg_timeout_for(1.0)
    mid = aes.ffmpeg_timeout_for(60.0)
    long_ = aes.ffmpeg_timeout_for(aes.MAX_CHUNK_SECONDS)

    assert short <= mid <= long_, "timeout must never shrink as the work grows"
    assert mid < long_, "a 300s window must be allowed longer than a 60s window"
    assert short == aes.MIN_TIMEOUT, "short reads get the floor, not a scaled-down sliver"
    assert long_ == aes.MAX_CHUNK_SECONDS * aes.TIMEOUT_PER_AUDIO_SECOND
    assert short >= aes.MIN_TIMEOUT, "short reads need a floor so startup jitter is not killed"
    # the old bug: a fixed 30s, far below what a long window legitimately needs
    assert long_ > 30.0


def test_no_pass_inherits_the_old_fixed_30_second_ceiling(monkeypatch):
    """Every subprocess call the scorer makes must carry a real timeout."""
    fake = _install(monkeypatch, FakeFfmpeg())
    aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)

    timeouts = fake.timeouts()
    assert timeouts, "no subprocess calls recorded"
    for call, timeout in zip(fake.calls, timeouts):
        assert timeout is not None, f"unbounded hang allowed: {call['cmd'][:3]}"
        assert timeout != 30, "the hardcoded 30s timeout is back"
    windows = [float(c[c.index("-t") + 1]) for c in fake.audio_reads()]
    for window, timeout in zip(windows, [t for c, t in zip(fake.calls, timeouts)
                                        if c["cmd"][0] == "ffmpeg"]):
        assert timeout >= window * aes.TIMEOUT_PER_AUDIO_SECOND


def test_a_slow_but_healthy_long_pass_is_not_killed(monkeypatch):
    """Regression: 3541s of audio in <=MAX_CHUNK_SECONDS windows must fit."""
    fake = _install(monkeypatch, FakeFfmpeg())
    t0 = time.perf_counter()
    values = aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)
    assert values
    # the fake is instant, so the point is only that the code path completed
    assert time.perf_counter() - t0 < 10
    audio_timeouts = [c["kwargs"]["timeout"] for c in fake.calls if c["cmd"][0] == "ffmpeg"]
    assert all(t > 30.0 or fake.duration <= aes.MAX_CHUNK_SECONDS for t in audio_timeouts)


# --------------------------------------------------------------------------
# 3. a failed measurement must not masquerade as a flat band ---------------
# --------------------------------------------------------------------------

def test_total_failure_raises_instead_of_fabricating_the_0_3_baseline(monkeypatch):
    _install(monkeypatch, FakeFfmpeg(fail_all_audio=True))
    segments = _segments()
    status = {}
    with pytest.raises(aes.AudioEnergyMeasurementError) as excinfo:
        aes.score_segments("/fake/source.mp4", segments, status_out=status)

    assert "/fake/source.mp4" in str(excinfo.value)
    assert status["ok"] is False
    # the old lie: [0.3] * num_segments reported as a completed measurement
    assert not all(s.get("audio_energy") == 0.3 for s in segments)
    assert not status.get("ok") and status["measured_segments"] == 0


def test_timeout_on_every_window_is_a_failure_not_a_silent_zero(monkeypatch):
    class TimeoutOnly(FakeFfmpeg):
        def run(self, cmd, *args, **kwargs):
            self.calls.append({"cmd": list(cmd), "kwargs": kwargs})
            if cmd[0] == "ffprobe":
                return super().run(cmd, *args, **kwargs)
            raise subprocess.TimeoutExpired(list(cmd), kwargs.get("timeout") or 0)

    _install(monkeypatch, TimeoutOnly())
    with pytest.raises(aes.AudioEnergyMeasurementError):
        aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)


def test_genuinely_flat_audio_is_still_a_real_measurement(monkeypatch):
    """Flat data is legitimate; it must not be conflated with a failed pass."""
    _install(monkeypatch, FakeFfmpeg(duration=600.0, amplitude=flat_amplitude))
    segments = _segments(count=6, span=100.0)
    status = {}
    scored = aes.score_segments("/fake/source.mp4", segments, status_out=status)

    energies = [s["audio_energy"] for s in scored]
    assert len(scored) == len(segments)
    assert all(0.0 <= e <= 1.0 for e in energies)
    assert max(energies) - min(energies) < 0.05
    assert energies != [0.3] * len(energies), "the fabricated baseline is back"
    assert status["ok"] is True
    assert status["flat_band"] is True, "a flat band must be labelled, not faked"
    assert status["measured_segments"] == status["num_segments"]


def test_partially_readable_source_reports_coverage(monkeypatch):
    """Half the windows unreadable is still a measurement - but it must say so."""
    fake = FakeFfmpeg()
    starts = [i * aes.MAX_CHUNK_SECONDS for i in range(20)]
    fake.fail_windows = [s for s in starts if s >= 1800]  # last ~1741s unreadable
    _install(monkeypatch, fake)
    status = {}
    values = aes.measure_energy("/fake/source.mp4", SEGMENT_DURATION, status_out=status)

    assert status["ok"] is True
    assert status["partial"] is True
    assert 0 < status["unmeasured_segments"]
    assert len(values) == status["num_segments"]


def test_worst_coverage_below_threshold_raises(monkeypatch):
    _install(monkeypatch, FakeFfmpeg(fail_windows=[i * aes.MAX_CHUNK_SECONDS + 300
                                                          for i in range(20)]))
    status = {}
    with pytest.raises(aes.AudioEnergyMeasurementError):
        aes.measure_energy("/fake/source.mp4", SEGMENT_DURATION, status_out=status)
    assert status["ok"] is False


def test_dynamic_audio_produces_a_varying_band(monkeypatch):
    """What the pipeline actually needs: non-degenerate energies for clip_selector.

    The 0-1 scale is the one the module has always used - (-60 dBFS .. 0 dBFS)
    mapped linearly - so it is compressive: a 40 dB spread lands around
    0.44 .. 0.88, not 0 .. 1.  What matters downstream is that the band has
    variance, because clip_selector scores the coefficient of variation.
    """
    _install(monkeypatch, FakeFfmpeg())
    values = aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=SEGMENT_DURATION)
    loud = values[:50]
    quiet = values[50:100]
    assert mean(loud) > mean(quiet) * 1.5, (loud[:5], quiet[:5])
    assert mean(quiet) > 0.0
    assert max(values) - min(values) > 0.1
    assert _cv(values) > 0.1, "a flat CV would leave the audio-dynamics weight dead"


def test_segment_count_contract_holds(monkeypatch):
    for duration, seg, expected in ((3541.414, 2.0, 1770), (10.0, 1.0, 10), (5.0, 3.0, 1),
                                    (60.0, 0.5, 120), (600.0, 2.0, 300)):
        _install(monkeypatch, FakeFfmpeg(duration=duration))
        values = aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=seg)
        assert len(values) == expected, (duration, seg, len(values))
        assert all(0.0 <= v <= 1.0 for v in values)


def test_score_segments_never_annotations_a_good_run_as_unmeasured(monkeypatch):
    """The honesty note must not itself become a lie.

    Without a status_out dict the stage used to log "(NOT MEASURED)" on a run
    that measured every window, because the status only travelled through the
    caller's sink.  The completion line is now driven by a local status.
    """
    from loguru import logger

    records = []
    _install(monkeypatch, FakeFfmpeg())
    sink = logger.add(lambda message: records.append(str(message)), level="INFO")
    try:
        scored = aes.score_segments("/fake/source.mp4", _segments(count=6, span=100.0))
    finally:
        logger.remove(sink)

    text = " | ".join(records)
    assert "NOT MEASURED" not in text, text
    assert "Energy range:" in text
    assert all("audio_energy" in s for s in scored)


def test_unknown_duration_is_a_failure_not_one_bogus_value(monkeypatch):
    """ffprobe returning 0 used to be clamped to 1.0 and scored as if real."""
    _install(monkeypatch, FakeFfmpeg(duration=0.0))
    with pytest.raises(aes.AudioEnergyMeasurementError):
        aes._get_audio_rms_peaks("/fake/source.mp4", segment_duration=1.0)


def test_silent_source_is_reported_as_real_silence_not_an_error(monkeypatch):
    _install(monkeypatch, FakeFfmpeg(has_audio=False))
    status = {}
    values = aes.measure_energy("/fake/source.mp4", SEGMENT_DURATION, status_out=status)
    assert values == [0.0] * status["num_segments"]
    assert status["ok"] is True and status["has_audio"] is False


def mean(values):
    return sum(values) / len(values)


# --------------------------------------------------------------------------
# 4. one real measurement on the source that broke -------------------------
# --------------------------------------------------------------------------

FFMPEG_REQUIRED = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe on PATH",
)


@FFMPEG_REQUIRED
@pytest.mark.skipif(not os.path.exists(REAL_SOURCE), reason="job 110 source not on disk")
def test_real_900_segment_source_is_measured_in_bounded_time():
    """storage/jobs/110/source.mp4 - the file that produced '0.300 - 0.300'."""
    segments = _real_segments(20)
    status = {}
    t0 = time.perf_counter()
    scored = aes.score_segments(REAL_SOURCE, segments, status_out=status)
    wall = time.perf_counter() - t0

    energies = [s["audio_energy"] for s in scored]
    assert status["ok"] is True
    assert len(energies) == len(segments)
    spread = max(energies) - min(energies)
    assert spread > 0.01, f"flat band on real audio: {energies[:5]}"
    assert not all(abs(e - 0.3) < 1e-9 for e in energies)
    assert wall < 60.0, f"bounded read took {wall:.1f}s"
    print(f"\nreal source: {wall:.2f}s wall, spread {spread:.3f}, "
          f"windows {status['windows_read']}/{status['windows_attempted']}, "
          f"cv={_cv(energies):.3f}")


def _real_segments(count):
    """Uniform slices across the real 3541s source, as the worker would pass them."""
    span = LONG_DURATION / count
    return [{"start": i * span, "end": (i + 1) * span, "text": "x"} for i in range(count)]


def _cv(values):
    m = mean(values)
    if m <= 0:
        return 0.0
    return (mean([(v - m) ** 2 for v in values])) ** 0.5 / m
