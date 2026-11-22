"""Technical quality checks on the file that ships, from measurements only.

evaluate_clip() looks at the finished clip after every render step (including the speed change
of the anti-fingerprint pass), so it measures what a viewer gets. Every check is one ffprobe or
ffmpeg call with a timeout, and every finding carries the value that triggered it. A check that
cannot run becomes an info finding; it never stops the pipeline.

Times: findings keep both the time in the shipped file and the time in the cut clip. The
shipped file runs `speed` times faster than the clip (1.02 with the transform), so
clip time = final time * speed.
"""

import io
import json
import os
import re
import subprocess
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger

from services.quality.models import (
    Evidence, Finding, QualityReport, has_evidence, status_for,
)

EVALUATOR_VERSION = "technical-1"
PROBE_TIMEOUT_S = 30
SCAN_TIMEOUT_S = 240

EXPECTED_SIZE = (1080, 1920)
LOUDNESS_TARGET_I = -14.0
LOUDNESS_TOLERANCE_LU = 2.5
TRUE_PEAK_WARN_DBTP = -0.5
TRUE_PEAK_FAIL_DBTP = 0.0
DURATION_WARN_S = 0.5
DURATION_FAIL_S = 2.0
AV_GAP_WARN_S = 0.3
AV_GAP_FAIL_S = 1.0
BLACK_MIN_S = 0.5
BLACK_FAIL_SHARE = 0.5          # black for more than half of the clip is a failure
FREEZE_MIN_S = 2.0
SILENCE_MIN_S = 2.5
SILENCE_OVER_SPEECH_S = 1.0     # silence overlapping captions by this much is reported
CAPTION_BAND = (1380, 1580)     # the rows of the 1920-high frame where captions sit
CAPTION_MIN_WHITE_SHARE = 0.004
CAPTION_SAMPLES = 5


def _run(cmd: List[str], timeout: int) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="ignore", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning(f"Quality check command failed ({cmd[0]}): {exc}")
        return None


class _Collector:
    """Gathers findings, drops any without evidence, and keeps per-check results."""

    def __init__(self, speed: float):
        self.speed = speed if speed and speed > 0 else 1.0
        self.findings: List[Finding] = []
        self.checks: Dict[str, str] = {}
        self._n = 0

    def add(self, check: str, category: str, severity: str, issue: str, explanation: str,
            confidence: float, evidence: Evidence, recommendation: str = "",
            t_final: Optional[float] = None) -> None:
        if not has_evidence(evidence):
            return
        self._n += 1
        self.findings.append(Finding(
            id=f"finding_{self._n:03d}", category=category, severity=severity, issue=issue,
            explanation=explanation, confidence=confidence, evidence=evidence,
            recommendation=recommendation,
            timestamp_final=None if t_final is None else round(t_final, 3),
            timestamp_clip=None if t_final is None else round(t_final * self.speed, 3),
            evaluator_version=EVALUATOR_VERSION,
        ))
        rank = {"critical": "failed", "warning": "warning", "info": "passed"}[severity]
        order = {"passed": 0, "warning": 1, "failed": 2}
        if order[rank] > order.get(self.checks.get(check, "passed"), 0):
            self.checks[check] = rank
        else:
            self.checks.setdefault(check, rank)

    def ok(self, check: str) -> None:
        self.checks.setdefault(check, "passed")

    def skipped(self, check: str, why: str) -> None:
        self.checks[check] = "skipped"
        self.add(check, "file", "info", f"{check} check did not run", why, 1.0,
                 Evidence(kind="measurement", ref=check, value=why))


def probe(path: str) -> Optional[Dict]:
    """ffprobe facts: {'video': {...}|None, 'audio': {...}|None, 'duration': float|None}."""
    result = _run(["ffprobe", "-v", "error", "-show_entries",
                   "stream=codec_type,width,height,duration,sample_rate:format=duration",
                   "-of", "json", path], PROBE_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or "{}")
    except ValueError:
        return None

    def _f(value) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    video = audio = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and video is None:
            video = {"width": stream.get("width"), "height": stream.get("height"), "duration": _f(stream.get("duration"))}
        elif stream.get("codec_type") == "audio" and audio is None:
            audio = {"duration": _f(stream.get("duration")), "sample_rate": _f(stream.get("sample_rate"))}
    return {"video": video, "audio": audio, "duration": _f(data.get("format", {}).get("duration"))}


def _check_file(c: _Collector, path: str, facts: Optional[Dict], expect: Dict) -> bool:
    """Structure: readable, has video, right size, audio when expected, durations. False stops the rest."""
    if facts is None:
        c.add("file", "file", "critical", "The file cannot be read",
              "ffprobe could not open the clip, so it is corrupt or incomplete.", 1.0,
              Evidence(kind="measurement", ref="ffprobe", value="failed to read " + os.path.basename(path)),
              "Re-render the clip.")
        return False
    if not facts["video"]:
        c.add("file", "file", "critical", "The clip has no video stream",
              "A short without a picture cannot be published.", 1.0,
              Evidence(kind="measurement", ref="ffprobe", value="streams: no video"), "Re-render the clip.")
        return False

    width, height = facts["video"]["width"], facts["video"]["height"]
    want = expect.get("size") or EXPECTED_SIZE
    if (width, height) != tuple(want):
        c.add("visual", "visual", "critical", f"The clip is {width}x{height}, not {want[0]}x{want[1]}",
              "Shorts need the 9:16 frame the layouts are built for.", 1.0,
              Evidence(kind="measurement", ref="ffprobe", value=f"{width}x{height}"),
              "Check the layout and reframe settings.")
    else:
        c.ok("visual")

    if expect.get("expect_audio") and not facts["audio"]:
        c.add("audio", "audio", "critical", "The clip has no audio stream",
              "The source had sound, so a silent clip means the audio was lost in the render.", 1.0,
              Evidence(kind="measurement", ref="ffprobe", value="streams: no audio"), "Re-render the clip.")
    else:
        c.ok("audio")

    expected = expect.get("expected_duration")
    video_d = facts["video"]["duration"] or facts["duration"]
    if expected and video_d:
        want_d = expected / c.speed
        gap = abs(video_d - want_d)
        if gap > DURATION_FAIL_S:
            sev = "critical"
        elif gap > DURATION_WARN_S:
            sev = "warning"
        else:
            sev = None
        if sev:
            c.add("sync", "sync", sev, f"Duration is {video_d:.2f}s, expected about {want_d:.2f}s",
                  "The rendered length differs from the planned cut; the end may be cut off or padded.", 0.9,
                  Evidence(kind="measurement", ref="ffprobe duration", value=round(video_d, 3)),
                  "Re-render the clip.", t_final=video_d)
        else:
            c.ok("sync")

    if facts["audio"] and facts["audio"]["duration"] and facts["video"]["duration"]:
        av_gap = abs(facts["audio"]["duration"] - facts["video"]["duration"])
        if av_gap > AV_GAP_FAIL_S:
            sev = "critical"
        elif av_gap > AV_GAP_WARN_S:
            sev = "warning"
        else:
            sev = None
        if sev:
            c.add("sync", "sync", sev, f"Audio and video differ by {av_gap:.2f}s",
                  "The voice drifts against the picture and the burned-in captions.", 0.95,
                  Evidence(kind="measurement", ref="ffprobe stream durations",
                           value={"audio": facts["audio"]["duration"], "video": facts["video"]["duration"]}),
                  "Re-render the clip.", t_final=min(facts["audio"]["duration"], facts["video"]["duration"]))
    return True


def _check_loudness(c: _Collector, path: str) -> None:
    try:
        from services.audio_polish import measure_loudness
        measured = measure_loudness(path)
    except Exception as exc:
        c.skipped("loudness", f"measurement failed: {exc}")
        return
    if not measured:
        c.skipped("loudness", "no audio to measure")
        return
    try:
        integrated = float(measured["input_i"])
        peak = float(measured["input_tp"])
    except (KeyError, TypeError, ValueError):
        c.skipped("loudness", "the measurement was unreadable")
        return
    c.ok("loudness")
    if peak > TRUE_PEAK_FAIL_DBTP:
        c.add("loudness", "audio", "critical", f"True peak {peak:.1f} dBTP: the audio clips",
              "Peaks above 0 dBTP are distorted and platforms may re-encode them harshly.", 0.95,
              Evidence(kind="measurement", ref="ebur128 true peak", value=peak),
              "Lower the effect gain or the loudness target.")
    elif peak > TRUE_PEAK_WARN_DBTP:
        c.add("loudness", "audio", "warning", f"True peak {peak:.1f} dBTP is close to the ceiling",
              "Little headroom is left, so the platform's encode may clip.", 0.7,
              Evidence(kind="measurement", ref="ebur128 true peak", value=peak))
    if abs(integrated - LOUDNESS_TARGET_I) > LOUDNESS_TOLERANCE_LU:
        c.add("loudness", "audio", "warning",
              f"Loudness {integrated:.1f} LUFS, target {LOUDNESS_TARGET_I:.0f} LUFS",
              "The delivered level differs from the other clips and from the platform's reference.", 0.8,
              Evidence(kind="measurement", ref="ebur128 integrated", value=integrated),
              "Check the speed and EQ steps that run after the loudness pass.")


_BLACK = re.compile(r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)")
_FREEZE_START = re.compile(r"freeze_start:\s*([\d.]+)")
_FREEZE_DUR = re.compile(r"freeze_duration:\s*([\d.]+)")
_SILENCE_START = re.compile(r"silence_start:\s*([\d.]+)")
_SILENCE_END = re.compile(r"silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)")


def scan(path: str, has_audio: bool,
         duration: Optional[float] = None) -> Optional[Dict[str, List[Tuple[float, float]]]]:
    """One ffmpeg pass: black stretches, frozen stretches and silent stretches as (start, duration)."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", path,
           "-vf", f"blackdetect=d={BLACK_MIN_S}:pix_th=0.10,freezedetect=n=-60dB:d={FREEZE_MIN_S}"]
    if has_audio:
        cmd += ["-af", f"silencedetect=n=-45dB:d={SILENCE_MIN_S}"]
    cmd += ["-f", "null", "-"]
    result = _run(cmd, SCAN_TIMEOUT_S)
    if result is None or result.returncode != 0:
        return None
    text = result.stderr or ""
    black = [(float(a), float(d)) for a, _b, d in _BLACK.findall(text)]
    # freezedetect prints freeze_duration only when the freeze ends, so a freeze that runs to the
    # end of the file has a start and no duration; close it against the file length.
    freeze = []
    freeze_open = None
    for line in text.splitlines():
        fs = _FREEZE_START.search(line)
        if fs:
            freeze_open = float(fs.group(1))
        fd = _FREEZE_DUR.search(line)
        if fd and freeze_open is not None:
            freeze.append((freeze_open, float(fd.group(1))))
            freeze_open = None
    if freeze_open is not None and duration is not None and duration > freeze_open:
        freeze.append((freeze_open, duration - freeze_open))
    silence = []
    open_start = None
    for line in text.splitlines():
        s = _SILENCE_START.search(line)
        if s:
            open_start = float(s.group(1))
        e = _SILENCE_END.search(line)
        if e:
            end, dur = float(e.group(1)), float(e.group(2))
            silence.append((open_start if open_start is not None else end - dur, dur))
            open_start = None
    return {"black": black, "freeze": freeze, "silence": silence}


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return max(0.0, min(a_end, b_end) - max(a_start, b_start))


def _check_scan(c: _Collector, path: str, facts: Dict, expect: Dict, duration: Optional[float]) -> None:
    found = scan(path, has_audio=bool(facts["audio"]), duration=duration)
    if found is None:
        c.skipped("scan", "the black, freeze and silence scan did not finish")
        return
    c.ok("black"); c.ok("freeze"); c.ok("silence")

    for start, length in found["black"]:
        share = length / duration if duration else 0.0
        severe = share >= BLACK_FAIL_SHARE
        c.add("black", "visual", "critical" if severe else "warning",
              f"Black picture for {length:.1f}s from {start:.1f}s",
              "A stretch of black frames reads as a broken clip.", 0.9 if severe else 0.75,
              Evidence(kind="filter_output", ref="blackdetect", value={"start": start, "duration": length}),
              "Check the source window and any overlay that covers the frame.", t_final=start)
    for start, length in found["freeze"]:
        c.add("freeze", "visual", "warning", f"Picture frozen for {length:.1f}s from {start:.1f}s",
              "A frozen frame can be a paused game or a stalled decode; confirm it is intended.", 0.55,
              Evidence(kind="filter_output", ref="freezedetect", value={"start": start, "duration": length}),
              "Watch this stretch.", t_final=start)

    caption_windows = [(float(e["start"]), float(e["end"])) for e in expect.get("caption_events", [])]
    for start, length in found["silence"]:
        speech = sum(_overlap(start * c.speed, (start + length) * c.speed, w0, w1) for w0, w1 in caption_windows)
        if speech >= SILENCE_OVER_SPEECH_S:
            c.add("silence", "audio", "warning", f"Silent for {length:.1f}s while captions show speech",
                  "The captions say something is being said, but the audio is silent: the voice may be missing here.",
                  0.8, Evidence(kind="filter_output", ref="silencedetect", value={"start": start, "duration": length}),
                  "Check the audio around this time.", t_final=start)
        elif not caption_windows and length >= SILENCE_MIN_S * 2:
            c.add("silence", "audio", "info", f"Silent for {length:.1f}s from {start:.1f}s",
                  "A long silent stretch with no captions.", 0.5,
                  Evidence(kind="filter_output", ref="silencedetect", value={"start": start, "duration": length}),
                  t_final=start)


def _frame_at(path: str, t: float):
    """The frame at time t as a PIL image, or None."""
    from PIL import Image
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{max(t, 0):.3f}", "-i", path, "-frames:v", "1",
         "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True, timeout=PROBE_TIMEOUT_S)
    if result.returncode != 0 or not result.stdout:
        return None
    return Image.open(io.BytesIO(result.stdout)).convert("RGB")


def _check_captions(c: _Collector, path: str, expect: Dict) -> None:
    events = expect.get("caption_events") or []
    if not events:
        c.skipped("captions", "no caption events expected for this clip")
        return
    picks = [events[int(i * (len(events) - 1) / max(1, CAPTION_SAMPLES - 1))] for i in range(min(CAPTION_SAMPLES, len(events)))]
    c.ok("captions")
    missing = []
    try:
        import numpy as np
        for event in picks:
            mid_clip = (float(event["start"]) + float(event["end"])) / 2.0
            t_final = mid_clip / c.speed
            frame = _frame_at(path, t_final)
            if frame is None:
                continue
            band = np.asarray(frame)[CAPTION_BAND[0]:CAPTION_BAND[1]]
            white = float((band.min(axis=2) > 235).mean())
            if white < CAPTION_MIN_WHITE_SHARE:
                missing.append((t_final, white))
    except Exception as exc:
        c.skipped("captions", f"frame sampling failed: {exc}")
        return
    for t_final, white in missing:
        c.add("captions", "captions", "warning", f"No caption text found at {t_final:.1f}s",
              "A caption was due here but the caption band has almost no white text pixels.", 0.5,
              Evidence(kind="frame", ref=f"frame at {t_final:.2f}s, rows {CAPTION_BAND[0]}-{CAPTION_BAND[1]}",
                       value=round(white, 5)),
              "Look at this frame.", t_final=t_final)


def evaluate_clip(path: str, expectations: Optional[Dict] = None) -> QualityReport:
    """Measure the shipped file and return a report.

    expectations (all optional): expect_audio (bool), expected_duration (seconds of the cut clip,
    before the speed change), speed (the transform's speed factor, 1.0 for none), size (w, h),
    caption_events ([{start, end}] in clip time).
    """
    expect = dict(expectations or {})
    c = _Collector(speed=float(expect.get("speed") or 1.0))
    facts = probe(path)
    if _check_file(c, path, facts, expect):
        duration = facts["video"]["duration"] or facts["duration"]
        for name, step in (("loudness", lambda: _check_loudness(c, path) if facts["audio"] else c.skipped("loudness", "no audio stream")),
                           ("scan", lambda: _check_scan(c, path, facts, expect, duration)),
                           ("captions", lambda: _check_captions(c, path, expect))):
            try:
                step()
            except Exception as exc:  # one broken check must not hide the others
                logger.warning(f"Quality check '{name}' crashed: {exc}")
                c.skipped(name, f"crashed: {exc}")
    return QualityReport(
        status=status_for(c.findings),
        findings=c.findings,
        checks=c.checks,
        evaluator_versions={"technical": EVALUATOR_VERSION},
        metadata={"speed": c.speed, "file": os.path.basename(path)},
    )
