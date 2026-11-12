"""
Audio energy scoring service for clip selection.

Measures per-segment RMS loudness from the audio track only, by reading the
source in bounded windows with ffmpeg and integrating the energy in Python.

Why it is built this way (the bug it replaced): both original ffmpeg passes
decoded the ENTIRE source - the astats pass failed instantly because
``,amix`` is invalid in a single-input filtergraph, and the ``volumedetect``
fallback decoded video too (83 s of wall clock on the 3541 s job-110 source)
while both carried a hardcoded ``timeout=30``.  On any long source the timeout
killed the fallback, the outer ``except Exception`` fabricated
``[0.3] * num_segments``, and the stage logged success.  A constant band has
coefficient of variation 0, so ``clip_selector._audio_dynamics_score`` returned
0.0 and 10% of the virality composite weight was dead on every clip.

Three properties this module now guarantees:

* bounded work - one ffmpeg invocation never decodes more than
  ``MAX_CHUNK_SECONDS`` of audio: audio-only, mono, sample-rate capped;
* a work-derived timeout - ``ffmpeg_timeout_for()`` scales with the window
  instead of a fixed 30 s, so a slow-but-healthy decode survives;
* no silent lie - a measurement that did not happen raises
  ``AudioEnergyMeasurementError``; only real samples produce real values, and a
  genuinely flat band is reported as measured-and-flat.
"""

import json
import subprocess
from typing import Any

import numpy as np
from loguru import logger

# --- measurement budget ---------------------------------------------------
# 8 kHz mono is ample for ranking loudness. Measured on the job-110 source, a
# whole-file read at this rate takes 7.1 s against 83.5 s for the old
# video-decoding volumedetect pass.
AUDIO_SAMPLE_RATE = 8000
MIN_AUDIO_SAMPLE_RATE = 1000
MAX_SAMPLES_PER_READ = 4_000_000          # ~8 MB of int16 per ffmpeg invocation

MAX_CHUNK_SECONDS = 300.0                 # audio decoded per ffmpeg invocation
TIMEOUT_PER_AUDIO_SECOND = 0.25           # healthy read is ~0.0035 s per audio second
MIN_TIMEOUT = 15.0                        # floor so process startup is not killed
PROBE_TIMEOUT = 10.0                      # ffprobe is a metadata read, never a decode

MAX_CONSECUTIVE_WINDOW_FAILURES = 3       # a broken file must not stall the worker
MIN_COVERAGE = 0.5                        # usable band needs this share of segments measured

DB_FLOOR = -60.0                          # dBFS mapped to 0.0, the scale the pipeline uses
DB_CEILING = 0.0                          # dBFS mapped to 1.0
NOISE_FLOOR_LINEAR = 1e-7                 # keeps log10 finite for digital silence

FLAT_BAND_SPREAD = 0.02                   # below this the band is flat, not broken


class AudioEnergyMeasurementError(RuntimeError):
    """No usable loudness band could be measured for this source.

    Raised instead of returning a fabricated constant, so the caller can tell
    "not measured" apart from "measured, and the source is genuinely quiet".
    Carries the status dict for callers that prefer the data to the exception.
    """

    def __init__(self, video_path: str, status: dict[str, Any], reason: str):
        self.video_path = str(video_path)
        self.status = status
        self.reason = reason
        super().__init__(
            f"Audio energy measurement failed for {self.video_path}: {reason} "
            f"(windows {status.get('windows_read')}/{status.get('windows_attempted')}, "
            f"segments measured {status.get('measured_segments')}"
            f"/{status.get('num_segments')})"
        )


def _as_text(value: Any) -> str:
    """Decode subprocess output that may arrive as bytes or str."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").strip()
    return (value or "").strip() if isinstance(value, str) else str(value)


def ffmpeg_timeout_for(work_seconds: float) -> float:
    """Timeout allowed for one ffmpeg pass over ``work_seconds`` of audio.

    Replaces the hardcoded ``timeout=30`` that killed every long source.  The
    factor is measured, not guessed: a bounded audio-only read runs at roughly
    280x realtime per window, so 0.25 s per audio second leaves a ~70x margin
    for a loaded machine, while the floor keeps short reads honest.
    """
    work = max(0.0, float(work_seconds))
    return max(MIN_TIMEOUT, work * TIMEOUT_PER_AUDIO_SECOND)


def _video_has_audio(video_path: str) -> bool:
    """Check if video has audio stream."""
    probe = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "a:0",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=PROBE_TIMEOUT,
    )
    if probe.returncode != 0:
        return False
    try:
        payload = json.loads(probe.stdout or "{}")
        streams = payload.get("streams", [])
        return any(stream.get("codec_type") == "audio" for stream in streams)
    except Exception:
        return False


def _get_video_duration(video_path: str) -> float:
    """Duration in seconds.

    Raises rather than defaulting to 1.0: a source of unknown length cannot be
    sliced into segments, and guessing it is how the old code logged success on
    a file it never read.
    """
    probe = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            video_path,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=PROBE_TIMEOUT,
    )
    try:
        value = float((probe.stdout or "").strip())
    except (TypeError, ValueError) as exc:
        raise AudioEnergyMeasurementError(
            video_path, {}, f"ffprobe returned no usable duration ({probe.stdout!r})"
        ) from exc
    if value <= 0:
        raise AudioEnergyMeasurementError(
            video_path, {}, f"ffprobe reported a non-positive duration ({value})"
        )
    return value


def _sample_rate_for(duration: float) -> int:
    """Mono sample rate that keeps one read bounded even on long sources."""
    per_second = MAX_SAMPLES_PER_READ / max(1.0, min(MAX_CHUNK_SECONDS, duration))
    rate = int(min(AUDIO_SAMPLE_RATE, max(MIN_AUDIO_SAMPLE_RATE, per_second)))
    return max(MIN_AUDIO_SAMPLE_RATE, rate)


def _iter_windows(duration: float) -> list[tuple[float, float]]:
    """Split the timeline into chunks of at most MAX_CHUNK_SECONDS."""
    windows: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        length = min(MAX_CHUNK_SECONDS, duration - start)
        windows.append((start, length))
        start += length
    return windows


def _read_pcm_window(video_path: str, start: float, length: float,
                     sample_rate: int) -> np.ndarray:
    """Decode one bounded window into mono int16 samples.

    ``-ss``/``-t`` sit before ``-i`` so the demuxer stops at the window edge,
    ``-vn`` keeps video frames out of the work entirely, and the timeout comes
    from the window length instead of a fixed guess.
    """
    command = [
        "ffmpeg",
        "-v", "error",
        "-ss", f"{start:.3f}",
        "-t", f"{length:.3f}",
        "-i", video_path,
        "-map", "0:a:0",
        "-vn",
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    result = subprocess.run(
        command,
        capture_output=True,
        timeout=ffmpeg_timeout_for(length),
        check=False,
    )
    raw = result.stdout or b""
    if result.returncode != 0:
        detail = _as_text(result.stderr)
        raise RuntimeError(f"ffmpeg rc={result.returncode}: {detail[:200]}")
    if len(raw) < 2:
        raise RuntimeError("ffmpeg returned no audio samples for the window")
    return np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2")


def _accumulate_window(samples: np.ndarray, start: float, length: float,
                       segment_duration: float, sample_rate: int,
                       num_segments: int, sums: np.ndarray,
                       counts: np.ndarray) -> None:
    """Add one window's energy into the global segment grid, exactly.

    Integrate-and-dump over a cumulative sum: segment edges are computed in the
    global time grid, so a segment straddling a window boundary receives two
    partial sums and keeps an honest per-segment sample count.
    """
    total = int(samples.size)
    first = max(0, int(start / segment_duration))
    last = min(num_segments - 1, int((start + length) / segment_duration))
    if last < first:
        return

    # boundaries of segments first..last, in samples relative to this window
    bounds = np.arange(first, last + 2, dtype=np.float64) * segment_duration
    edges = np.clip(np.round((bounds - start) * sample_rate), 0, total)
    edges = np.maximum.accumulate(edges).astype(np.int64)

    normed = samples.astype(np.float64) / 32768.0
    cumulative = np.concatenate(([0.0], np.cumsum(normed * normed)))

    a = edges[:-1]
    b = edges[1:]
    usable = b > a
    if not np.any(usable):
        return
    ids = np.arange(first, last + 1, dtype=np.int64)[usable]
    np.add.at(sums, ids, cumulative[b[usable]] - cumulative[a[usable]])
    np.add.at(counts, ids, (b[usable] - a[usable]).astype(np.float64))


def _to_linear_band(sums: np.ndarray, counts: np.ndarray,
                    measured: np.ndarray) -> np.ndarray:
    """Mean-square per segment -> dBFS -> the 0-1 scale clip_selector expects."""
    band = np.zeros(counts.shape, dtype=np.float64)
    if not np.any(measured):
        return band
    rms = np.sqrt(sums[measured] / counts[measured])
    db = 20.0 * np.log10(np.maximum(rms, NOISE_FLOOR_LINEAR))
    band[measured] = np.clip((db - DB_FLOOR) / (DB_CEILING - DB_FLOOR), 0.0, 1.0)
    return band


def measure_energy(video_path: str, segment_duration: float = 1.0,
                   status_out: dict[str, Any] | None = None) -> list[float]:
    """One RMS energy value per ``segment_duration`` slice of the whole source.

    Returns ``max(1, int(duration / segment_duration))`` values in 0-1, where
    0.0 maps to -60 dBFS and 1.0 to 0 dBFS.  Raises
    ``AudioEnergyMeasurementError`` when too little of the source could be read
    to build a band - it never substitutes a constant.

    ``status_out`` (a plain dict) receives what actually happened, so a caller
    can tell "measured, and the source really is flat" from "not measured".
    """
    video_path = str(video_path)
    segment_duration = float(segment_duration or 1.0)
    if segment_duration <= 0:
        segment_duration = 1.0

    duration = _get_video_duration(video_path)
    num_segments = max(1, int(duration / segment_duration))

    status: dict[str, Any] = {
        "video_path": video_path,
        "duration": duration,
        "segment_duration": segment_duration,
        "num_segments": num_segments,
        "has_audio": True,
        "windows_attempted": 0,
        "windows_read": 0,
        "windows_failed": 0,
        "measured_segments": 0,
        "unmeasured_segments": num_segments,
        "coverage": 0.0,
        "partial": False,
        "flat_band": False,
        "ok": False,
        "error": None,
    }

    if not _video_has_audio(video_path):
        # A missing audio track is a real finding, not a failure: silence.
        status.update(has_audio=False, ok=True, coverage=1.0,
                      measured_segments=num_segments, unmeasured_segments=0,
                      spread=0.0, flat_band=True)
        if status_out is not None:
            status_out.update(status)
        logger.warning(f"No audio stream in {video_path}: the energy band is real silence")
        return [0.0] * num_segments

    sample_rate = _sample_rate_for(duration)
    sums = np.zeros(num_segments, dtype=np.float64)
    counts = np.zeros(num_segments, dtype=np.float64)
    consecutive_failures = 0
    last_error = ""

    for start, length in _iter_windows(duration):
        status["windows_attempted"] += 1
        try:
            samples = _read_pcm_window(video_path, start, length, sample_rate)
        except subprocess.TimeoutExpired:
            consecutive_failures += 1
            last_error = (f"window {start:.0f}-{start + length:.0f}s timed out after "
                          f"{ffmpeg_timeout_for(length):.0f}s")
            logger.error(f"FFmpeg window timeout in {video_path}: {last_error}")
        except Exception as exc:  # one unreadable window must not abort a healthy file
            consecutive_failures += 1
            last_error = f"window {start:.0f}-{start + length:.0f}s: {exc}"
            logger.error(f"FFmpeg window unreadable in {video_path}: {last_error}")
        else:
            _accumulate_window(samples, start, length, segment_duration, sample_rate,
                               num_segments, sums, counts)
            status["windows_read"] += 1
            consecutive_failures = 0

        status["windows_failed"] = status["windows_attempted"] - status["windows_read"]
        if consecutive_failures >= MAX_CONSECUTIVE_WINDOW_FAILURES:
            logger.error(
                f"Giving up on {video_path} after {consecutive_failures} consecutive "
                f"unreadable windows ({last_error})")
            break

    measured = counts > 0
    measured_segments = int(np.count_nonzero(measured))
    coverage = measured_segments / float(num_segments)
    status.update(measured_segments=measured_segments,
                  unmeasured_segments=num_segments - measured_segments,
                  coverage=round(coverage, 4),
                  partial=measured_segments < num_segments)

    if coverage < MIN_COVERAGE:
        status["error"] = last_error or "no audio samples decoded"
        if status_out is not None:
            status_out.update(status)
        raise AudioEnergyMeasurementError(
            video_path, status,
            f"only {measured_segments}/{num_segments} segments measurable "
            f"(below the {MIN_COVERAGE:.0%} minimum); last error: "
            f"{last_error or 'no samples returned'}")

    band = _to_linear_band(sums, counts, measured)
    if measured_segments < num_segments:
        # Keep the one-value-per-segment contract: fill the holes with the
        # measured mean, say so out loud, and mark the status partial.
        fill = float(np.mean(band[measured]))
        band[~measured] = fill
        logger.warning(
            f"{video_path}: {num_segments - measured_segments} of {num_segments} "
            f"segments were unreadable and carry the measured mean {fill:.3f}")

    spread = float(band.max() - band.min())
    status["spread"] = round(spread, 4)
    status["flat_band"] = spread < FLAT_BAND_SPREAD
    status["ok"] = True
    if status_out is not None:
        status_out.update(status)

    if status["flat_band"]:
        logger.info(
            f"Audio energy band is genuinely flat (spread {spread:.3f}) - measured, "
            "not faked; audio dynamics will contribute little for this source")
    else:
        logger.info(
            f"Measured audio energy over {status['windows_read']} bounded windows: "
            f"band {band.min():.3f}-{band.max():.3f}, {num_segments} segments "
            f"at {segment_duration:.2f}s / {sample_rate} Hz mono")

    return [float(v) for v in band]


def _get_audio_rms_peaks(video_path: str, segment_duration: float = 1.0) -> list[float]:
    """RMS energy per segment, normalized 0-1 (0.0 == -60 dBFS).

    Kept as the module's historical entry point.  It used to run an astats pass
    (invalid filtergraph, exited instantly) and then a whole-file volumedetect
    fallback under a 30 s timeout; both decoded the entire source.  It is now
    one bounded windowed read, and it raises ``AudioEnergyMeasurementError``
    instead of returning fabricated numbers.
    """
    return measure_energy(video_path, segment_duration=segment_duration)


def _get_volume_peaks_fallback(video_path: str, segment_duration: float = 1.0) -> list[float]:
    """Deprecated alias, kept only so an old import does not break.

    There is no separate fallback any more: ``volumedetect`` decoded the whole
    source including video, which is what caused the timeout, and the
    "variation" it synthesised was a seeded random() guess over a single mean.
    """
    return measure_energy(video_path, segment_duration=segment_duration)


def score_segments(video_path: str, segments: list[dict[str, Any]],
                   status_out: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """
    Score each segment based on audio energy (RMS loudness).

    Args:
        video_path: Path to the video file
        segments: List of segment dicts with 'start' and 'end' keys
        status_out: optional dict that receives the measurement status

    Returns:
        Copy of the segments with an added 'audio_energy' field (0.0-1.0 float)

    Raises:
        AudioEnergyMeasurementError: when the source could not be measured.
        The old behaviour - stamp 0.3 (or 0.5) on everything and log success -
        is gone: a constant band is indistinguishable downstream from a
        genuinely quiet video, and it silently zeroes the audio-dynamics
        weight in the virality composite.
    """
    if not segments:
        if status_out is not None:
            status_out.update({"ok": True, "num_segments": 0, "measured": False,
                               "reason": "no segments passed"})
        return segments

    logger.info(f"Scoring audio energy for {len(segments)} segments in {video_path}")

    total_duration = _get_video_duration(video_path)
    # one energy value per segment slot across the whole source; a finer grid
    # buys nothing, every segment averages the samples inside its own span
    sample_rate = max(0.5, min(2.0, total_duration / len(segments)))
    status: dict[str, Any] = {}
    try:
        rms_values = measure_energy(video_path, segment_duration=sample_rate,
                                    status_out=status)
    finally:
        # a caller that catches the exception still has to be able to see what
        # was attempted, so the sink is filled on the failure path too
        if not status:
            status.update({"ok": False, "num_segments": len(segments),
                           "measured_segments": 0,
                           "error": "measurement never started"})
        if status_out is not None:
            status_out.update(status)

    scored_segments = []
    for segment in segments:
        start_time = float(segment.get("start") or 0.0)
        end_time = float(segment.get("end") or start_time + 1.0)

        start_idx = int(start_time / sample_rate)
        end_idx = int(end_time / sample_rate)
        start_idx = max(0, min(start_idx, len(rms_values) - 1))
        end_idx = max(start_idx, min(end_idx, len(rms_values) - 1))

        window = rms_values[start_idx:end_idx + 1]
        avg_energy = sum(window) / len(window) if window else 0.0
        audio_energy = max(0.0, min(1.0, avg_energy))

        scored_segment = segment.copy()
        scored_segment["audio_energy"] = audio_energy
        scored_segments.append(scored_segment)

    energies = [s["audio_energy"] for s in scored_segments]
    low, high = min(energies), max(energies)
    if not status.get("ok"):
        note = " (NOT MEASURED)"
    elif status.get("has_audio") is False:
        note = " [silent source]"
    elif status.get("partial"):
        note = (f" [partial: {status.get('unmeasured_segments')} segments filled "
                "with the measured mean]")
    else:
        note = ""
    logger.info(f"Audio energy scoring complete{note}. "
                f"Energy range: {low:.3f} - {high:.3f}")

    if status.get("ok") and high - low < FLAT_BAND_SPREAD:
        logger.warning(
            "Audio energy is flat for this source and it really is: the band was "
            "measured, so the audio-dynamics signal will be near zero")

    return scored_segments
