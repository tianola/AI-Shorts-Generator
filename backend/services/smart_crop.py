"""Smart-crop wrapper for OpusClip-grade multi-layout vertical (9:16) conversion."""
from typing import Optional
import os
import subprocess
import cv2
from services.vertical import make_vertical, make_vertical_static
from services.focus_tracking import build_split_screen_filter, build_blurred_fit_filter
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
    Apply the requested vertical layout mode:
      - 'auto' / 'adaptive': Automatically select the optimal layout based on video context
      - 'single_speaker' or 'face_tracked': Smooth YOLO follow of active speaker
      - 'split_screen': Stacked 9:16 layout for 2 speakers (podcast style)
      - 'fit_blur': Centered 16:9 on top of atmospheric blurred 9:16 background
      - 'static': Direct center crop
    """
    if mode is None or mode in ("auto", "adaptive", ""):
        mode = detect_adaptive_layout(input_file)
        job_info(0, f"Adaptive layout resolved to '{mode}' for {input_file}")

    os.makedirs(os.path.dirname(output_file) if os.path.dirname(output_file) else ".", exist_ok=True)

    if mode == "split_screen":
        try:
            from services import reframe_plan
            analysis = reframe_plan.analyse_clip(input_file)
            two_shot = analysis.get("two_shot")
            if two_shot:
                boxes = [reframe_plan.representative_box(t) for t in two_shot]
            else:
                ranked = sorted(analysis.get("tracks") or [], key=lambda tr: len(tr["timeline"]), reverse=True)
                boxes = [reframe_plan.representative_box(tr) for tr in ranked[:2]] if len(ranked) >= 2 else []
            if len(boxes) == 2:
                p1_box, p2_box = boxes
                # Probe real source dimensions — never assume 1920x1080
                _cap = cv2.VideoCapture(input_file)
                _src_w = int(_cap.get(cv2.CAP_PROP_FRAME_WIDTH)) if _cap.isOpened() else 1920
                _src_h = int(_cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) if _cap.isOpened() else 1080
                _cap.release()
                filtergraph = build_split_screen_filter(p1_box, p2_box, src_w=_src_w, src_h=_src_h, target_w=width, target_h=height)
                cmd = [
                    "ffmpeg", "-y", "-i", input_file,
                    "-filter_complex", filtergraph,
                    "-map", "[outv]", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET,
                    "-crf", str(settings.FFMPEG_CRF), "-c:a", "copy",
                    "-movflags", "+faststart", output_file
                ]
                creationflags = 0
                if os.name == 'nt':
                    creationflags = 0x08000000 # subprocess.CREATE_NO_WINDOW
                subprocess.run(cmd, check=True, capture_output=True, creationflags=creationflags)
                job_info(0, f"Split-screen vertical conversion: {output_file}")
                return output_file
        except Exception as e:
            job_error(0, f"Split-screen fallback to face_tracked: {e}")

    elif mode == "fit_blur":
        try:
            filtergraph = build_blurred_fit_filter(src_w=1920, src_h=1080, target_w=width, target_h=height)
            cmd = [
                "ffmpeg", "-y", "-i", input_file,
                "-filter_complex", filtergraph,
                "-map", "[outv]", "-map", "0:a?",
                "-c:v", "libx264", "-preset", settings.FFMPEG_PRESET,
                "-crf", str(settings.FFMPEG_CRF), "-c:a", "copy",
                "-movflags", "+faststart", output_file
            ]
            subprocess.run(cmd, check=True, capture_output=True)
            job_info(0, f"Blurred fit vertical conversion: {output_file}")
            return output_file
        except Exception as e:
            job_error(0, f"Blurred fit fallback to static: {e}")

    if mode in ["face_tracked", "single_speaker"]:
        return make_vertical(input_file, output_file, width, height)
    elif mode == "static":
        return make_vertical_static(input_file, output_file, width, height)

    return make_vertical(input_file, output_file, width, height)
