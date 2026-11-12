"""Audio polish utilities for cleaner short-form narration."""
import json
import math
import os
import re
import subprocess

# Every ffmpeg call in the render chain is bounded: a decoder wedged on a odd
# stream used to hold the whole job open forever, with nothing in the log.
PROBE_TIMEOUT_S = 30      # ffprobe: header read only
ANALYSIS_TIMEOUT_S = 120  # audio-only passes: decode, no encode (~60x realtime)
ENCODE_TIMEOUT_S = 1800   # a full re-encode of one short clip

# EBU R128 delivery target. YouTube/TikTok/Reels all normalise to roughly this
# on upload anyway; rendering to it ourselves is what makes two clips cut from
# one source play at the same perceived volume.
LOUDNORM_TARGET_I = -14.0
LOUDNORM_TARGET_TP = -1.5
LOUDNORM_TARGET_LRA = 11.0
AUDIO_SAMPLE_RATE = 48000

# loudnorm's first pass prints these; all four (plus target_offset) have to be
# finite numbers before a second pass can use them.
_MEASURE_KEYS = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
_MEASURE_JSON_RE = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.S)


def _finite(value):
    """The value as a finite float, or None.

    Digital silence measures "-inf" LUFS, which json.loads happily turns into
    float('-inf'); handed to loudnorm that yields a filter that does nothing at
    all, so it is rejected here rather than in the graph builder.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def measure_loudness(media_path, start=None, duration=None):
    """Pass 1 of two-pass loudnorm: read the clip's R128 numbers.

    Returns the parsed JSON block loudnorm prints
    (``input_i``/``input_tp``/``input_lra``/``input_thresh``/``target_offset``),
    or None when there is no audio, nothing usable, or the run dies — in which
    case the caller renders un-normalised instead of failing the job.

    ``start``/``duration`` must be passed for a trimmed render: measuring the
    whole source and normalising a window out of it applies the wrong gain.
    """
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start:
        cmd += ["-ss", f"{float(start):.3f}"]
    if duration:
        cmd += ["-t", f"{float(duration):.3f}"]
    cmd += [
        "-i", media_path,
        "-map", "0:a:0",
        "-vn",
        "-af", (f"loudnorm=I={LOUDNORM_TARGET_I}:TP={LOUDNORM_TARGET_TP}"
                f":LRA={LOUDNORM_TARGET_LRA}:print_format=json"),
        "-f", "null", "-",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="ignore",
            timeout=ANALYSIS_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        # A clip with no audio stream and a wedged decoder look the same here.
        return None
    match = _MEASURE_JSON_RE.search(result.stderr or "")
    if not match:
        return None
    try:
        measured = json.loads(match.group(0))
    except ValueError:
        return None
    if not _usable_measurement(measured):
        return None
    return measured


def _usable_measurement(measured):
    """True when every value pass 2 needs is present and finite."""
    if not measured:
        return False
    for key in _MEASURE_KEYS:
        if _finite(measured.get(key)) is None:
            return False
    # loudnorm only ever reports inside the R128 measurement range
    return _finite(measured["input_i"]) >= -70.0


def build_loudnorm_filter(measured):
    """Pass 2 of two-pass loudnorm: the filter chain that applies ``measured``.

    ``linear=true`` asks for one fixed gain over the whole clip — the transparent
    kind, no pumping — and loudnorm only falls back to its own dynamic limiter
    when the true-peak ceiling forbids that gain. ``loudnorm`` resamples to
    192 kHz internally, so the chain must end by coming back to stereo 48 kHz;
    aresample belongs AFTER it, never before.

    Empty string when there is nothing usable to apply, so a caller can splice
    the result in unconditionally.
    """
    if not _usable_measurement(measured):
        return ""
    return (
        f"loudnorm=I={LOUDNORM_TARGET_I}:TP={LOUDNORM_TARGET_TP}"
        f":LRA={LOUDNORM_TARGET_LRA}"
        f":measured_I={_finite(measured['input_i']):.2f}"
        f":measured_TP={_finite(measured['input_tp']):.2f}"
        f":measured_LRA={_finite(measured['input_lra']):.2f}"
        f":measured_thresh={_finite(measured['input_thresh']):.2f}"
        f":offset={_finite(measured['target_offset']):.2f}"
        ":linear=true:print_format=summary"
        ",aformat=sample_fmts=fltp:channel_layouts=stereo"
        f",aresample={AUDIO_SAMPLE_RATE}"
    )


_FILLER_WORDS = {
	"uh",
	"um",
	"uhm",
	"like",
	"you know",
	"basically",
	"literally",
	"actually",
}


def remove_filler_words(text: str) -> str:
	"""Strip very common filler words from transcript text."""
	if not text:
		return ""
	cleaned = text
	for filler in sorted(_FILLER_WORDS, key=len, reverse=True):
		cleaned = re.sub(rf"\b{re.escape(filler)}\b", "", cleaned, flags=re.IGNORECASE)
	cleaned = re.sub(r"\s+", " ", cleaned).strip()
	return cleaned


def polish_audio(input_audio: str, output_audio: str) -> str:
	"""Run lightweight audio cleanup.

	This creates a cleaner voice track without requiring heavy denoising models.
	"""
	os.makedirs(os.path.dirname(output_audio) if os.path.dirname(output_audio) else ".", exist_ok=True)

	cmd = [
		"ffmpeg",
		"-y",
		"-i", input_audio,
		"-af",
		"silenceremove=1:0:-50dB,highpass=f=80,lowpass=f=8000,dynaudnorm=p=0.9:s=5,volume=1.2",
		"-ar", "16000",
		output_audio,
	]

	subprocess.run(cmd, check=True, capture_output=True, timeout=ENCODE_TIMEOUT_S)
	return output_audio
