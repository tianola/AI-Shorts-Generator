"""
Precision clip trimming service.

Provides functional, frame-aware trimming of video clips. The primary
operations are:

- ``validate_trim_range`` : validate a requested [start, end) window against
  the source media's real duration, returning descriptive error messages.
- ``trim_clip``           : cut a clip from a source video to the exact
  requested span, choosing a fast stream-copy path when the cut points align
  with keyframes and a frame-accurate re-encode path otherwise.
- ``probe_media``         : ffprobe metadata helpers used by the API layer.

Design notes
------------
* Input validation lives *before* any ffmpeg work so invalid ranges never
  create output files (hard requirement of the feature).
* Stream-copy (-c copy) preserves quality and is fast, but only yields
  frame-accurate results when the requested points land on keyframes.  When
  they do not, we fall back to a re-encode so the caller gets the exact span
  instead of a silently longer/shorter clip.
* Audio/video sync is preserved by trimming with the same seek parameters for
  video and audio and by container-aware muxing; metadata is carried over with
  -map_metadata 0.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional, Tuple

from config import settings
from utils.logger import job_info, job_error, job_warning

# Tolerance in seconds for the requested-vs-produced duration check.
TRIM_DURATION_TOLERANCE = 0.15

# Container extensions for which +faststart is a good default (MP4 family).
_FASTSTART_EXTS = {".mp4", ".m4v", ".mov", ".webm"}


class TrimValidationError(ValueError):
    """Raised when a trim request describes an invalid [start, end) window."""


def probe_media(video_path: str) -> dict:
    """
    Probe a media file and return its streams and format metadata.

    Returns:
        dict with keys:
          - duration: float seconds
          - has_video: bool
          - has_audio: bool
          - video_codec: Optional[str]
          - audio_codec: Optional[str]
          - width / height: Optional[int]

    Raises:
        TrimValidationError: if the file is missing, unreadable, or has no
            video stream.
    """
    if not video_path or not os.path.exists(video_path):
        raise TrimValidationError(f"Source media not found: {video_path}")

    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise TrimValidationError(f"Timed out probing media: {video_path}")
    except OSError as e:
        raise TrimValidationError(f"ffprobe not available: {e}")

    if result.returncode != 0:
        raise TrimValidationError(
            f"Could not probe media '{video_path}': {result.stderr.strip()}"
        )

    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        raise TrimValidationError(f"Invalid ffprobe output for '{video_path}': {e}")

    streams = data.get("streams", [])
    video_stream = next(
        (s for s in streams if s.get("codec_type") == "video"), None
    )
    audio_stream = next(
        (s for s in streams if s.get("codec_type") == "audio"), None
    )

    try:
        duration = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    # Fall back to per-stream duration when the container duration is missing.
    if duration <= 0.0 and video_stream:
        try:
            duration = float(video_stream.get("duration") or 0.0)
        except (TypeError, ValueError):
            duration = 0.0

    return {
        "duration": duration,
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "video_codec": video_stream.get("codec_name") if video_stream else None,
        "audio_codec": audio_stream.get("codec_name") if audio_stream else None,
        "width": video_stream.get("width") if video_stream else None,
        "height": video_stream.get("height") if video_stream else None,
    }


def _as_seconds(value: object, field: str) -> float:
    """
    Coerce a trim time value to seconds.

    Accepts int/float seconds, "HH:MM:SS" / "MM:SS" timestamps, and
    "SS.mmm" strings. Raises TrimValidationError with a descriptive message
    for anything else.
    """
    if isinstance(value, bool):
        raise TrimValidationError(f"{field} must be a number or timestamp, got boolean")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds < 0:
            raise TrimValidationError(f"{field} must be non-negative")
        if seconds >= 1e7:
            raise TrimValidationError(f"{field} out of range: {value}")
        return seconds
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise TrimValidationError(f"{field} cannot be empty")
        try:
            seconds = float(text)
            if seconds < 0:
                raise TrimValidationError(f"{field} must be non-negative")
            if seconds >= 1e7:
                raise TrimValidationError(f"{field} out of range: {value}")
            return seconds
        except ValueError:
            pass
        # Timestamp format: [HH:]MM:SS[.millis]
        parts = text.split(":")
        if 2 <= len(parts) <= 3:
            try:
                nums = [float(p) for p in parts]
            except ValueError:
                nums = None
            if nums:
                seconds = nums[0] * 60 + nums[1]
                if len(nums) == 3:
                    seconds = nums[0] * 3600 + nums[1] * 60 + nums[2]
                if seconds < 0:
                    raise TrimValidationError(f"{field} must be non-negative")
                if seconds >= 1e7:
                    raise TrimValidationError(f"{field} out of range: {value}")
                return seconds
    raise TrimValidationError(
        f"{field} must be seconds or MM:SS / HH:MM:SS timestamp, got: {value!r}"
    )


def validate_trim_range(
    source_video: str,
    start_time: object,
    end_time: object,
) -> Tuple[float, float, dict]:
    """
    Validate a trim request and return (start, end, media_info).

    Rules enforced (each raises TrimValidationError with a descriptive
    message):
      - source media exists, has a video stream, and a known duration
      - start >= 0 and end <= duration
      - start < end
      - the resulting span must be at least 1 frame (>= ~0.001s)

    This function performs NO filesystem writes.
    """
    start = _as_seconds(start_time, "start_time")
    end = _as_seconds(end_time, "end_time")

    if start < 0:
        raise TrimValidationError("start_time must be non-negative")
    if end < 0:
        raise TrimValidationError("end_time must be non-negative")
    if start >= end:
        raise TrimValidationError(
            f"start_time must be less than end_time (got start={start}, end={end})"
        )

    media = probe_media(source_video)
    if not media["has_video"]:
        raise TrimValidationError(f"Source media has no video stream: {source_video}")
    duration = media["duration"]
    if duration <= 0:
        raise TrimValidationError(
            f"Could not determine source duration (invalid media?): {source_video}"
        )

    # Allow a tiny epsilon so a request equal to the container duration
    # (within container rounding) is accepted.
    epsilon = 0.05
    if end > duration + epsilon:
        raise TrimValidationError(
            f"end_time {end:.3f}s exceeds source duration {duration:.3f}s"
        )

    end = min(end, duration)

    if end - start < 0.001:
        raise TrimValidationError(
            f"Trim span too short: start={start:.3f}s end={end:.3f}s"
        )

    return start, end, media


def _format_time(seconds: float) -> str:
    """Format seconds for an ffmpeg -ss / -to argument (millisecond precision)."""
    return f"{seconds:.3f}"


def _detect_keyframe_alignment(source_video: str, start: float, end: float) -> bool:
    """
    Best-effort check whether the cut points are keyframe aligned.

    Uses ffprobe on the video stream's keyframe packet timestamps
    (skip_frame nokey) and returns True only when BOTH boundaries land on a
    reported keyframe.

    NOTE: this is inherently unreliable across ffmpeg builds/containers
    (some report every packet as a keyframe for MP4+x264). It is used as a
    *fast-path hint* only — the caller still validates the produced duration
    and falls back to a frame-accurate re-encode when the stream copy does
    not match the requested span.
    """
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-skip_frame", "nokey",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        source_video,
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False
        keyframes = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                keyframes.append(float(line))
            except ValueError:
                continue
    except Exception:
        return False

    if not keyframes:
        return False

    tolerance = 0.03  # ~1 frame at 30fps
    start_ok = any(abs(kf - start) <= tolerance for kf in keyframes)
    end_ok = any(abs(kf - end) <= tolerance for kf in keyframes)
    return start_ok and end_ok


def _validate_produced_duration(
    output_path: str,
    expected: float,
    tolerance: float = TRIM_DURATION_TOLERANCE,
) -> None:
    """Verify the produced file's duration is close to the requested span.

    The check is asymmetric by design:
      - if the produced file is *shorter* than expected by more than
        tolerance, that's a hard failure (we lost content).
      - if it is *longer* (frame/keyframe quantization), it is still a valid,
        playable file, so we only warn. The caller decides whether the extra
        length is acceptable (see ``trim_clip`` which falls back to a
        frame-accurate re-encode when a precise span is required).
    """
    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
        raise TrimValidationError(f"Trim produced no output file: {output_path}")

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        produced = float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        produced = 0.0

    if produced <= 0:
        raise TrimValidationError(f"Trim output has unreadable duration: {output_path}")
    if produced < expected - tolerance:
        raise TrimValidationError(
            f"Trim output too short: expected {expected:.3f}s, got {produced:.3f}s"
        )


def _validate_copy_duration_within_tolerance(output_path: str, expected: float) -> bool:
    """Return True when the produced copy duration is within tolerance of the
    requested span (both directions). Used to decide whether a stream copy is
    precise enough or the auto path must fall back to re-encoding."""
    if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        produced = float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        return False
    if produced <= 0:
        return False
    # Stream-copy quantization may over- or under-shoot by up to ~1 frame; a
    # tolerance of TRIM_DURATION_TOLERANCE (0.15s) keeps the fast path while
    # guaranteeing the "duration matches requested span" contract.
    return abs(produced - expected) <= TRIM_DURATION_TOLERANCE


def trim_clip(
    source_video: str,
    start_time: object,
    end_time: object,
    output_path: str,
    *,
    force_reencode: bool = False,
    max_fallback: bool = True,
    video_codec: Optional[str] = None,
    audio_codec: Optional[str] = None,
    crf: Optional[int] = None,
    preset: Optional[str] = None,
    delete_output_on_error: bool = True,
) -> dict:
    """
    Trim a clip from ``source_video`` to the exact ``[start_time, end_time)``
    span and return metadata about the produced file.

    Args:
        source_video: Path to the source media file.
        start_time / end_time: Seconds (float) or "MM:SS"/"HH:MM:SS" timestamp.
        output_path: Destination file path. Must not equal source_video.
        force_reencode: Always re-encode (frame-accurate) even when the cut
            points align to keyframes.
        max_fallback: When True (default), fall back to re-encode if the
            stream-copy pass fails validation; if False, propagate the error.
        video_codec / audio_codec: Codec overrides for the re-encode path
            (defaults: libx264 / aac).
        crf / preset: Encoding quality knobs (defaults from settings).
        delete_output_on_error: Remove a partial output file when the trim
            fails validation.

    Returns:
        dict with keys:
          - output_path
          - duration (produced, seconds)
          - method ("stream_copy" | "reencode")
          - used_fallback (bool)

    Raises:
        TrimValidationError for every invalid request *before* any output
        file is created, or any ffmpeg command fails.
    """
    if not output_path:
        raise TrimValidationError("output_path is required")
    output_abs = os.path.abspath(output_path)
    source_abs = os.path.abspath(source_video)
    if output_abs == source_abs:
        raise TrimValidationError("output_path must differ from source_video")

    start, end, _ = validate_trim_range(source_video, start_time, end_time)
    expected_duration = end - start

    os.makedirs(os.path.dirname(output_abs) or ".", exist_ok=True)

    def _cleanup_on_error():
        if delete_output_on_error and os.path.exists(output_abs):
            try:
                os.remove(output_abs)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Fast path: stream copy (only kept when it hits the requested span)
    # ------------------------------------------------------------------
    if not force_reencode:
        keyframe_aligned = _detect_keyframe_alignment(source_video, start, end)
        if keyframe_aligned:
            copy_cmd = [
                "ffmpeg", "-y",
                "-ss", _format_time(start),
                "-to", _format_time(end),
                "-i", source_video,
                "-map", "0:v:0",
                "-map", "0:a?",
                "-map_metadata", "0",
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
            ]
            # Container metadata preservation for MP4-family outputs.
            if os.path.splitext(output_abs)[1].lower() in _FASTSTART_EXTS:
                copy_cmd += ["-movflags", "+faststart"]
            copy_cmd.append(output_abs)

            try:
                subprocess.run(copy_cmd, check=True, capture_output=True, text=True)
                _validate_produced_duration(output_abs, expected_duration)
                copy_precise = _validate_copy_duration_within_tolerance(
                    output_abs, expected_duration
                )
                if copy_precise or not max_fallback:
                    job_info(0, f"Trim (stream copy): {source_video} -> {output_abs} "
                                f"({expected_duration:.3f}s)")
                    return {
                        "output_path": output_abs,
                        "duration": _probe_duration_quiet(output_abs),
                        "method": "stream_copy",
                        "used_fallback": not copy_precise,
                    }
                # Copy succeeded but quantized to a keyframe boundary beyond
                # tolerance — the user asked for a precise span, so fall back.
                job_warning(0, "Stream-copy trim quantized to keyframe boundary "
                               "(" + f"{_probe_duration_quiet(output_abs):.3f}s "
                               f"vs {expected_duration:.3f}s); re-encoding for "
                               "frame accuracy")
            except subprocess.CalledProcessError as e:
                _cleanup_on_error()
                if not max_fallback:
                    err = e.stderr if isinstance(e.stderr, str) else str(e)
                    raise TrimValidationError(f"Stream-copy trim failed: {err}")
                job_warning(0, f"Stream-copy trim failed, falling back to re-encode: {e}")
            except TrimValidationError:
                _cleanup_on_error()
                if not max_fallback:
                    raise
                job_warning(0, "Stream-copy trim failed duration validation, "
                               "falling back to re-encode")

    # ------------------------------------------------------------------
    # Frame-accurate path: re-encode
    # ------------------------------------------------------------------
    vcodec = video_codec or "libx264"
    acodec = audio_codec or "aac"
    crf_val = crf if crf is not None else settings.FFMPEG_CRF
    preset_val = preset or settings.FFMPEG_PRESET

    enc_cmd = [
        "ffmpeg", "-y",
        "-ss", _format_time(start),
        "-to", _format_time(end),
        "-i", source_video,
        "-map", "0:v:0",
        "-map", "0:a?",
        "-map_metadata", "0",
        "-c:v", vcodec,
        "-preset", preset_val,
        "-crf", str(crf_val),
        "-fps_mode", "cfr",
        "-pix_fmt", "yuv420p",
        "-c:a", acodec,
        "-b:a", "128k",
    ]
    if os.path.splitext(output_abs)[1].lower() in _FASTSTART_EXTS:
        enc_cmd += ["-movflags", "+faststart"]
    enc_cmd.append(output_abs)

    try:
        subprocess.run(enc_cmd, check=True, capture_output=True, text=True)
        _validate_produced_duration(output_abs, expected_duration)
        job_info(0, f"Trim (re-encode): {source_video} -> {output_abs} "
                    f"({expected_duration:.3f}s)")
        return {
            "output_path": output_abs,
            "duration": _probe_duration_quiet(output_abs),
            "method": "reencode",
            "used_fallback": True,
        }
    except subprocess.CalledProcessError as e:
        _cleanup_on_error()
        err = e.stderr if isinstance(e.stderr, str) else str(e)
        job_error(0, f"Trim re-encode failed: {err}")
        raise TrimValidationError(f"Trim re-encode failed: {err}")
    except TrimValidationError:
        _cleanup_on_error()
        raise


def _probe_duration_quiet(path: str) -> float:
    """Best-effort duration probe; returns 0.0 on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return float(result.stdout.strip()) if result.stdout.strip() else 0.0
    except Exception:
        return 0.0