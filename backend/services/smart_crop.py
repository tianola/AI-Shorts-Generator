"""Smart-crop wrapper for OpusClip-grade multi-layout vertical (9:16) conversion."""
from typing import Optional
import os
import subprocess
from services.vertical import build_vertical_filter
from services.audio_polish import ENCODE_TIMEOUT_S  # every ffmpeg call is bounded
from config import settings
from utils.logger import job_info, job_error


def detect_adaptive_layout(video_path: str, sample_interval: float = 0.5, diarization=None, attention=None) -> str:
    """Pick the layout a human editor would pick for this clip.

    Delegates to :mod:`services.reframe_plan`, which requires exactly two
    persistent, comparably sized people before it stacks the frame — the old
    rule here asked whether two boxes existed anywhere in the clip and said
    yes to a presenter with a passer-by, or a three-person shot. ``diarization``
    (the clip's speaker turns) lets the voices outrank the motion read, and
    ``attention`` (its scene read) moves the crop onto what is being described.
    """
    try:
        from services import reframe_plan
        mode, _focus = reframe_plan.resolve(video_path, sample_interval=sample_interval,
                                            diarization=diarization, attention=attention)
        return mode
    except Exception as e:
        job_error(0, f"Adaptive layout detection error: {e}")
        return "single_speaker"


def smart_crop_video(
    input_file: str,
    output_file: str,
    width: int = 1080,
    height: int = 1920,
    mode: Optional[str] = None,
) -> str:
    """
    Convert a clip to vertical (9:16) in one ffmpeg encode. The filter graph is the one
    the single-pass render uses (services.vertical.build_vertical_filter), so both paths
    frame a clip identically:
      - 'auto' / 'adaptive': pick the layout from the clip (people, speakers, scene)
      - 'single_speaker' or 'face_tracked': smooth follow of the active speaker
      - 'split_screen': stacked layout for 2 speakers (podcast style)
      - 'fit_blur': centered 16:9 on top of an atmospheric blurred 9:16 background
      - 'gaming': gameplay on top, the corner facecam in its own panel below
      - 'static': direct center crop
    """
    if mode is None or mode in ("auto", "adaptive", ""):
        mode = detect_adaptive_layout(input_file)
        job_info(0, f"Adaptive layout resolved to '{mode}' for {input_file}")

    os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".", exist_ok=True)

    try:
        fragment = build_vertical_filter(input_file, mode=mode, target_width=width, target_height=height)
    except Exception as e:
        job_error(0, f"Layout '{mode}' unavailable ({e}); using a static center crop")
        fragment = build_vertical_filter(input_file, mode="static", target_width=width, target_height=height)

    cmd = [
        "ffmpeg", "-y", "-i", input_file,
        "-filter_complex", fragment,
        "-map", "[vsingle]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF), "-c:a", "copy",
        "-movflags", "+faststart", output_file,
    ]
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    subprocess.run(cmd, check=True, capture_output=True, creationflags=creationflags,
                   timeout=ENCODE_TIMEOUT_S)
    job_info(0, f"Vertical conversion ({mode}): {output_file}")
    return output_file
