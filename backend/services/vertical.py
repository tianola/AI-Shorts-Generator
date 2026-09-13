"""
Vertical video conversion service.
Converts horizontal videos to vertical 9:16 format.
Supports both static center crop and face-tracked dynamic reframing.
"""
import json
import os
import subprocess
import cv2
import numpy as np
from pathlib import Path
from typing import Optional, List, Tuple
from config import settings
from utils.logger import job_info, job_error, logger

def make_vertical_static(
    input_file: str,
    output_file: str,
    width: int = 1080,
    height: int = 1920
) -> str:
    """
    Convert video to vertical using static center crop.

    Args:
        input_file: Path to input video
        output_file: Path to output video
        width: Output width (default: 1080)
        height: Output height (default: 1920)

    Returns:
        Path to output file
    """
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    # Get video info
    cmd_probe = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        input_file
    ]

    result = subprocess.run(cmd_probe, capture_output=True, text=True)
    if result.returncode != 0:
        err = result.stderr
        error_msg = str(err) if err else "Unknown error"
        raise Exception(f"Failed to probe video: {error_msg}")

    import json
    video_info = json.loads(result.stdout)
    streams = video_info.get("streams", [])

    if not streams:
        # ffprobe returned no streams — fall back to a safe ffmpeg scale+pad without probing
        cmd = [
            "ffmpeg",
            "-y",
            "-i", input_file,
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-crf", str(settings.FFMPEG_CRF),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_file
        ]

        try:
            subprocess.run(cmd, check=True, capture_output=True)
            job_info(0, f"Static vertical conversion (probe-less): {input_file} -> {output_file}")
            return output_file
        except subprocess.CalledProcessError as e:
            err = e.stderr
            error_msg = err.decode(errors='ignore') if isinstance(err, (bytes, bytearray)) else str(err) if err else str(e)
            raise Exception(f"Static vertical conversion failed (probe-less): {error_msg}")

    in_width = streams[0].get("width")
    in_height = streams[0].get("height")

    # Calculate crop for center
    aspect_ratio = in_width / in_height

    if aspect_ratio > (width / height):
        # Wide video - crop sides
        new_width = int(in_height * (width / height))
        crop_x = (in_width - new_width) // 2
        crop_y = 0
        crop_w = new_width
        crop_h = in_height
    else:
        # Tall or square video - crop top/bottom
        new_height = int(in_width * (height / width))
        crop_x = 0
        crop_y = (in_height - new_height) // 2
        crop_w = in_width
        crop_h = new_height

    # FFmpeg command with crop and scale
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_file,
        "-vf", f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={width}:{height}",
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_file
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Static vertical conversion: {input_file} -> {output_file}")
        return output_file
    except subprocess.CalledProcessError as e:
        raise Exception(f"Static vertical conversion failed: {e.stderr.decode() if e.stderr else str(e)}")

def detect_faces_in_frame(frame: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """
    Detect faces in a video frame using MediaPipe / OpenCV fallback.

    Args:
        frame: OpenCV frame (BGR format)

    Returns:
        List of (x, y, w, h) bounding boxes
    """
    try:
        import mediapipe as mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection"):
            if not hasattr(detect_faces_in_frame, "mp_face_detection"):
                detect_faces_in_frame.mp_face_detection = mp.solutions.face_detection.FaceDetection(
                    model_selection=1, min_detection_confidence=0.5
                )
            detector = detect_faces_in_frame.mp_face_detection
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = detector.process(rgb_frame)
            if not results.detections:
                return []
            h, w, _ = frame.shape
            boxes = []
            for detection in results.detections:
                bboxC = detection.location_data.relative_bounding_box
                x, y = int(bboxC.xmin * w), int(bboxC.ymin * h)
                box_w, box_h = int(bboxC.width * w), int(bboxC.height * h)
                x, y = max(0, x), max(0, y)
                box_w, box_h = min(w - x, box_w), min(h - y, box_h)
                if box_w > 0 and box_h > 0:
                    boxes.append((x, y, box_w, box_h))
            return boxes
    except Exception:
        pass

    # OpenCV Haar Cascade Fallback
    if not hasattr(detect_faces_in_frame, "face_cascade"):
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        detect_faces_in_frame.face_cascade = cv2.CascadeClassifier(cascade_path)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    detections = detect_faces_in_frame.face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
    )
    return [(x, y, w, h) for (x, y, w, h) in detections]

def sample_frames_for_face_tracking(
    video_path: str,
    sample_interval: float = 0.5
) -> List[Tuple[float, List[Tuple[int, int, int, int]]]]:
    """
    Sample frames from video and detect faces in each.

    Args:
        video_path: Path to video file
        sample_interval: Time between samples in seconds

    Returns:
        List of (timestamp, faces) tuples
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise Exception(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30  # Default

    frame_interval = int(fps * sample_interval)
    if frame_interval < 1:
        frame_interval = 1

    results = []
    frame_count = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = frame_count / fps

        # Sample every N frames
        if frame_count % frame_interval == 0:
            faces = detect_faces_in_frame(frame)
            results.append((timestamp, faces))

        frame_count += 1

    cap.release()
    return results

def compute_crop_trajectory(
    face_samples: List[Tuple[float, List[Tuple[int, int, int, int]]]],
    video_duration: float,
    target_width: int = 1080,
    target_height: int = 1920,
    source_width: int = None,
    source_height: int = None
) -> List[Tuple[float, int, int, int, int]]:
    """
    Compute a smooth crop trajectory based on face positions.

    The crop window always matches the target aspect ratio (9:16) so the
    output is never stretched. The window is centred on the detected face
    (or the screen centre when no face is visible) and clamped to bounds.

    Args:
        face_samples: List of (timestamp, faces) from sampling
        video_duration: Total duration of video
        target_width: Target output width
        target_height: Target output height
        source_width: Actual source video width (for clamping crop bounds)
        source_height: Actual source video height (for clamping crop bounds)

    Returns:
        List of (timestamp, crop_x, crop_y, crop_w, crop_h)
    """
    src_w = source_width or target_width
    src_h = source_height or target_height

    # 9:16 window that fits entirely inside the source frame (no stretch)
    crop_w = int(src_h * target_width / target_height)
    crop_h = src_h
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(src_w * target_height / target_width)

    # Keep dimensions even so libx264 + yuv420p stays happy
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2

    if crop_w < 2 or crop_h < 2:
        return []

    if not face_samples:
        x = max(0, (src_w - crop_w) // 2)
        y = max(0, (src_h - crop_h) // 2)
        return [(0, x, y, crop_w, crop_h)]

    # For each sample, compute the optimal crop center
    crop_centers = []

    for timestamp, faces in face_samples:
        if not faces:
            # No faces in this frame, use previous center or default
            if crop_centers:
                crop_centers.append(crop_centers[-1])
            else:
                crop_centers.append((src_w // 2, src_h // 2))
            continue

        if len(faces) == 1:
            best_face = faces[0]
            face_center_x = best_face[0] + best_face[2] // 2
            face_center_y = best_face[1] + best_face[3] // 2
        else:
            # Multi-speaker active framing (Opus-style): enclose all detected faces
            min_x = min(f[0] for f in faces)
            min_y = min(f[1] for f in faces)
            max_x = max(f[0] + f[2] for f in faces)
            max_y = max(f[1] + f[3] for f in faces)
            face_center_x = (min_x + max_x) // 2
            face_center_y = (min_y + max_y) // 2

        crop_centers.append((face_center_x, face_center_y))

    # Smooth the trajectory with moving average
    window_size = 3
    smoothed_centers = []

    for i, center in enumerate(crop_centers):
        if i < window_size // 2 or i >= len(crop_centers) - window_size // 2:
            smoothed_centers.append(center)
        else:
            window = crop_centers[i - window_size // 2 : i + window_size // 2 + 1]
            avg_x = sum(c[0] for c in window) // len(window)
            avg_y = sum(c[1] for c in window) // len(window)
            smoothed_centers.append((avg_x, avg_y))

    # Convert to crop parameters
    trajectory = []
    for i, (timestamp, _) in enumerate(face_samples):
        center_x, center_y = smoothed_centers[i]

        # Centre the 9:16 window on the face, clamped to source bounds
        crop_x = max(0, min(center_x - crop_w // 2, src_w - crop_w))
        crop_y = max(0, min(center_y - crop_h // 2, src_h - crop_h))

        trajectory.append((timestamp, crop_x, crop_y, crop_w, crop_h))

    return trajectory

def _resample_trajectory(
    trajectory: List[Tuple[float, int, int, int, int]],
    max_keys: int = 12
) -> List[Tuple[float, int, int, int, int]]:
    """Thin a trajectory down to at most ``max_keys`` evenly-spaced keyframes."""
    if len(trajectory) <= max_keys:
        return trajectory
    idx = sorted(set(round(i * (len(trajectory) - 1) / (max_keys - 1)) for i in range(max_keys)))
    return [trajectory[i] for i in idx]


def _build_timed_crop(trajectory: List[Tuple[float, int, int, int, int]]) -> str:
    """Build a ffmpeg ``crop`` filter whose x/y follow the face over time.

    Uses piecewise-linear interpolation between trajectory keyframes via
    nested ``if(lt(t, ...), ...)`` expressions. The crop window size is
    constant (9:16) so the output is never stretched.

    Returns a string like ``crop=540:960:<x_expr>:<y_expr>`` suitable for
    inclusion in a ``-vf`` filtergraph. Commas inside expression function
    calls are escaped (``\\,``) so the filtergraph parser keeps them intact.
    """
    keys = _resample_trajectory(trajectory)
    w = keys[0][3]
    h = keys[0][4]

    def x_interp(i: int) -> str:
        t0, x0, _, _, _ = keys[i]
        t1, x1, _, _, _ = keys[i + 1]
        dt = max(t1 - t0, 0.001)
        return f"{x0}+({x1}-{x0})*(t-{t0})/{dt}"

    def y_interp(i: int) -> str:
        t0, _, y0, _, _ = keys[i]
        t1, _, y1, _, _ = keys[i + 1]
        dt = max(t1 - t0, 0.001)
        return f"{y0}+({y1}-{y0})*(t-{t0})/{dt}"

    def build_expr(interp) -> str:
        if len(keys) == 1:
            return str(keys[0][1])
        expr = interp(len(keys) - 2)
        for i in range(len(keys) - 3, -1, -1):
            t0 = keys[i][0]
            expr = f"if(lt(t,{t0}),{interp(i)},{expr})"
        return expr

    # Build x and y as independent piecewise-linear expressions, then escape
    # the commas exactly once so the filtergraph parser keeps eval intact.
    x_expr = build_expr(x_interp).replace(",", "\\,")
    y_expr = build_expr(y_interp).replace(",", "\\,")

    return f"crop={w}:{h}:{x_expr}:{y_expr}"


def make_vertical_face_tracked(
    input_file: str,
    output_file: str,
    width: int = 1080,
    height: int = 1920
) -> str:
    """
    Convert video to vertical using modern YOLO focus tracking.

    Detects people/objects, builds a dynamic 9:16 crop that follows the main
    subject and alternates between two subjects ("one up, one down" when the
    source allows). Falls back to the old MediaPipe face tracker, then static.

    Args:
        input_file: Path to input video
        output_file: Path to output video

    Returns:
        Path to output file
    """
    try:
        # 1) Try the modern YOLO focus tracker first
        try:
            from services.focus_tracking import sample_frames, compute_yolo_trajectory
            cmd_info = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-show_entries", "stream=width,height",
                "-select_streams", "v:0", "-of", "json", input_file
            ]
            result = subprocess.run(cmd_info, capture_output=True, text=True)
            info = json.loads(result.stdout)
            video_duration = float(info["format"]["duration"])
            stream = next(s for s in info["streams"] if s.get("width"))
            src_w = int(stream["width"])
            src_h = int(stream["height"])
            yolo_samples = sample_frames(input_file, sample_interval=0.5)
            trajectory = compute_yolo_trajectory(
                yolo_samples, src_w, src_h, width, height
            ) if yolo_samples else []
        except Exception as yolo_err:
            job_info(0, f"YOLO focus tracking unavailable ({yolo_err}); using MediaPipe face tracker")
            face_samples = sample_frames_for_face_tracking(input_file)
            cmd_info = [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-show_entries", "stream=width,height",
                "-select_streams", "v:0", "-of", "json", input_file
            ]
            result = subprocess.run(cmd_info, capture_output=True, text=True)
            info = json.loads(result.stdout)
            video_duration = float(info["format"]["duration"])
            stream = next(s for s in info["streams"] if s.get("width"))
            trajectory = compute_crop_trajectory(
                face_samples, video_duration, width, height,
                int(stream["width"]), int(stream["height"])
            )

        if not trajectory or len(trajectory) < 2:
            job_info(0, "Focus tracking: insufficient data, falling back to static crop")
            return make_vertical_static(input_file, output_file, width, height)

        crop_filter = _build_timed_crop(trajectory)
        logger.info(f"Focus-tracked crop filter: {crop_filter[:120]}...")
        vf = f"{crop_filter},scale={width}:{height}"

        cmd = [
            "ffmpeg", "-y", "-i", input_file,
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", settings.FFMPEG_PRESET,
            "-crf", str(settings.FFMPEG_CRF),
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_file
        ]

        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Focus-tracked vertical conversion: {input_file} -> {output_file}")
        return output_file

    except Exception as e:
        job_error(0, f"Focus-tracked conversion failed: {str(e)}")
        job_info(0, "Falling back to static vertical conversion")
        return make_vertical_static(input_file, output_file, width, height)

def make_vertical(
    input_file: str,
    output_file: str,
    width: int = 1080,
    height: int = 1920
) -> str:
    """
    Convert video to vertical format based on VERTICAL_MODE setting.

    Args:
        input_file: Path to input video
        output_file: Path to output video
        width: Output width (default: 1080)
        height: Output height (default: 1920)

    Returns:
        Path to output file
    """
    if settings.VERTICAL_MODE == "face_tracked":
        return make_vertical_face_tracked(input_file, output_file, width, height)
    else:
        return make_vertical_static(input_file, output_file, width, height)