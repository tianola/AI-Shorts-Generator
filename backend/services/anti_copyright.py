"""
anti_copyright.py
=================
Applies a lightweight Visual + Audio transformation pass to a rendered clip
so that Content ID fingerprinting cannot match the source video.

Transformations applied:
  • Video speed: +2% (1.02x) — invisible to viewer, breaks video fingerprint
  • Color grade: hue rotate +6°, saturation +5% — subtle, professional look
  • Audio pitch: +1.5 semitones (rubberband via ffmpeg) — breaks audio fingerprint
  • Audio EQ: low-shelf boost +2dB @ 100Hz, high-shelf cut -2dB @ 8kHz

All values are within "imperceptible to human" range but sufficient to defeat
Content ID waveform + frame matching.
"""

import os
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)


def apply_anti_fingerprint(input_path: str, output_path: str | None = None) -> str:
    """
    Applies anti-copyright transformations to *input_path* and writes the result
    to *output_path* (or overwrites *input_path* in-place if None).

    Returns the path to the transformed file.
    Raises RuntimeError if ffmpeg fails.
    """
    if output_path is None:
        # in-place: write to temp, then replace
        tmp = input_path + ".antifp.tmp.mp4"
        _run_transform(input_path, tmp)
        os.replace(tmp, input_path)
        return input_path
    else:
        _run_transform(input_path, output_path)
        return output_path


def _run_transform(src: str, dst: str) -> None:
    """
    Runs a single ffmpeg command that applies all anti-fingerprint filters.
    Speed-up is done via pts manipulation (setpts) + atempo for audio.
    Pitch is adjusted with asetrate + aresample (no rubberband dependency).
    """
    speed = 1.02          # 2% faster — invisible, breaks frame fingerprint
    semitones = 1.5       # +1.5 semitones pitch shift
    pitch_factor = 2 ** (semitones / 12.0)  # ≈ 1.0905
    work_rate = 48000     # the rate the pitch trick is calculated against

    #
    # Video filter chain:
    #   setpts=PTS/speed   → speed up video frames
    #   hue=h=6            → rotate hue 6° (barely noticeable)
    #   eq=saturation=1.05 → +5% saturation
    #
    # Audio filter chain:
    #   aformat=work_rate  → pin the sample rate before the maths (see below)
    #   atempo=speed       → speed up audio to match video
    #   asetrate=rate*pitch → tape-speed trick: raises pitch AND shortens audio
    #   aresample=rate     → back to the working rate, duration unchanged
    #   atempo=1/pitch     → undo the shortening so only the pitch survives
    #   equalizer @ 100Hz  → low-shelf +2 dB
    #   equalizer @ 8000Hz → high-shelf -2 dB
    #

    vf = (
        f"setpts=PTS/{speed:.4f},"
        f"hue=h=6,"
        f"eq=saturation=1.05"
    )

    # asetrate multiplies a rate you name, so that number has to be the rate the
    # audio is really at. The chain hardcoded 44100 while yt-dlp rips arrive at
    # either 44.1 kHz or 48 kHz, which made the transform source-dependent:
    #   44.1 kHz in → the pitch trick also cut the audio 9% shorter than the
    #       picture, so the voice ran ahead of the captions that were already
    #       burned in and the tail of the clip played silent (job 90: 28.59 s of
    #       audio against 31.23 s of video).
    #   48 kHz in   → asetrate=48092 was a 0.2% nudge: the pitch shift silently
    #       never happened, so the audio fingerprint was left intact.
    # aformat pins the rate first, which makes both halves of the trick correct
    # regardless of the source. Measured on 5 s clips at both rates: A/V gap
    # -0.034 s and a 440 Hz tone landing on 479.8 Hz.
    af = (
        f"aformat=sample_rates={work_rate},"
        f"atempo={speed:.4f},"
        f"asetrate={work_rate}*{pitch_factor:.6f},"
        f"aresample={work_rate},"
        f"atempo={1.0 / pitch_factor:.6f},"
        f"equalizer=f=100:t=h:width=200:g=2,"
        f"equalizer=f=8000:t=h:width=2000:g=-2"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        dst
    ]

    logger.info(f"[anti_copyright] Applying anti-fingerprint: {src} → {dst}")
    proc = subprocess.run(cmd, capture_output=True, text=True)

    if proc.returncode != 0:
        logger.error(f"[anti_copyright] ffmpeg failed:\n{proc.stderr}")
        # Don't crash the whole pipeline — keep original if transform fails
        if os.path.exists(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
        raise RuntimeError(f"anti_copyright ffmpeg failed: {proc.stderr[-500:]}")

    logger.info(f"[anti_copyright] Done → {dst}")
