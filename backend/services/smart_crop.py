"""Smart-crop wrapper for OpusClip-grade multi-layout vertical (9:16) conversion."""
from typing import Optional
import os
import subprocess
import cv2
from services.vertical import make_vertical, make_vertical_static
from services.focus_tracking import sample_frames, build_split_screen_filter, build_blurred_fit_filter
from config import settings
from utils.logger import job_info, job_error


def detect_adaptive_layout(video_path: str, sample_interval: float = 0.5) -> str:
    """
    Intelligently analyze video content across sampled frames to automatically select the optimal layout:
      - 'split_screen': 2 distinct separated speakers (e.g. podcast / interview).
      - 'single_speaker': 1 active speaker / tracked focal person.
      - 'fit_blur': wide landscape, screen share, gaming, or scene without clear faces.
    """
    try:
        samples = sample_frames(video_path, sample_interval=sample_interval)
        if not samples:
            return "single_speaker"

        two_speaker_count = 0
        single_speaker_count = 0
        total_samples = len(samples)

        for ts, dets in samples:
            persons = [d for d in dets if d.get("cls") == 0 and d.get("conf", 0) >= 0.25]
            if len(persons) >= 2:
                # Check horizontal separation between the two primary persons
                p_sorted = sorted(persons, key=lambda d: d["box"][0])
                box1 = p_sorted[0]["box"]
                box2 = p_sorted[1]["box"]
                c1 = box1[0] + box1[2] / 2.0
                c2 = box2[0] + box2[2] / 2.0
                # If they are separated by at least 15% of width (~200px)
                if abs(c2 - c1) >= 200:
                    two_speaker_count += 1
                else:
                    single_speaker_count += 1
            elif len(persons) == 1:
                single_speaker_count += 1

        if total_samples > 0 and (two_speaker_count / total_samples) >= 0.30:
            return "split_screen"
        elif total_samples > 0 and ((single_speaker_count + two_speaker_count) / total_samples) >= 0.20:
            return "single_speaker"
        else:
            return "fit_blur"
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
            samples = sample_frames(input_file, sample_interval=0.5)
            # Find sample with at least 2 people
            persons_list = []
            for ts, dets in samples:
                p = [d for d in dets if d["cls"] == 0]
                if len(p) >= 2:
                    p.sort(key=lambda d: d["box"][0])  # left to right
                    persons_list = p
                    break
            if len(persons_list) >= 2:
                p1_box = persons_list[0]["box"]
                p2_box = persons_list[1]["box"]
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
