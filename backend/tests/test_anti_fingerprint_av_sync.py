"""
The anti-fingerprint pass runs AFTER the captions are burned in, so anything it
does to the relative length of the audio and video tracks reaches the viewer as
captions drifting out of sync with the voice.

Two properties have to hold at once, and both depend on the sample rate the
transform is calculated against:

  - audio and video must stay the same length, or the voice runs ahead of the
    captions and the tail of the clip plays silent;
  - the pitch must actually move, or the audio fingerprint is left intact.

`asetrate` is a tape-speed trick: it raises pitch AND shortens duration by the
same factor, relative to the rate you name. Hardcoding 44100 got neither right
for 48 kHz sources, and shortening the audio by 9% for 44.1 kHz ones.

Run from backend/:  ../.venv/Scripts/python.exe -m pytest tests/test_anti_fingerprint_av_sync.py -q
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import wave

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="requires ffmpeg and ffprobe on PATH",
)

SOURCE_DURATION = 6.0
TONE_HZ = 440.0
PITCH_FACTOR = 2 ** (1.5 / 12.0)


def _make_source(rate: int) -> str:
    path = os.path.join(tempfile.mkdtemp(prefix=f"antifp_src_{rate}_"), "source.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", f"testsrc2=duration={SOURCE_DURATION}:size=320x240:rate=30",
            "-f", "lavfi", "-i", f"sine=frequency={TONE_HZ}:duration={SOURCE_DURATION}:sample_rate={rate}",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", str(rate), "-b:a", "96k",
            path,
        ],
        check=True, capture_output=True, text=True,
    )
    return path


def _stream_durations(path: str) -> dict:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=codec_type,duration",
            "-print_format", "json",
            path,
        ],
        check=True, capture_output=True, text=True,
    ).stdout
    durations = {}
    for stream in json.loads(out).get("streams", []):
        if stream.get("duration"):
            durations[stream["codec_type"]] = float(stream["duration"])
    return durations


def _dominant_tone(path: str) -> float:
    wav_path = os.path.join(os.path.dirname(path), "probe.wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", path, "-ac", "1", "-ar", "48000", wav_path],
        check=True, capture_output=True, text=True,
    )
    with wave.open(wav_path) as handle:
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype=np.int16).astype(float)
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(len(samples))))
    return float(np.fft.rfftfreq(len(samples), 1 / 48000)[np.argmax(spectrum)])


@pytest.mark.parametrize("rate", [44100, 48000], ids=["44.1k", "48k"])
def test_transform_keeps_audio_and_video_the_same_length(rate, tmp_path):
    from services.anti_copyright import _run_transform

    src = _make_source(rate)
    dst = os.path.join(str(tmp_path), "transformed.mp4")
    _run_transform(src, dst)

    streams = _stream_durations(dst)
    video, audio = streams["video"], streams["audio"]

    # 0.25s is generous: aac frames quantize by ~23ms each, while the bug this
    # guards against was a 9% gap - 0.54s on a 6s clip.
    assert abs(video - audio) < 0.25, (
        f"voice drifts against the picture: video {video:.3f}s vs audio {audio:.3f}s"
    )


@pytest.mark.parametrize("rate", [44100, 48000], ids=["44.1k", "48k"])
def test_transform_actually_shifts_the_pitch(rate, tmp_path):
    from services.anti_copyright import _run_transform

    src = _make_source(rate)
    dst = os.path.join(str(tmp_path), "transformed.mp4")
    _run_transform(src, dst)

    tone = _dominant_tone(dst)
    assert abs(tone - TONE_HZ * PITCH_FACTOR) < 3.0, (
        f"expected +1.5 semitones ({TONE_HZ * PITCH_FACTOR:.1f} Hz), measured {tone:.1f} Hz"
    )


def test_transform_speeds_video_up_by_the_documented_two_percent(tmp_path):
    from services.anti_copyright import _run_transform

    src = _make_source(48000)
    dst = os.path.join(str(tmp_path), "transformed.mp4")
    _run_transform(src, dst)

    video = _stream_durations(dst)["video"]
    assert abs(video - SOURCE_DURATION / 1.02) < 0.25
