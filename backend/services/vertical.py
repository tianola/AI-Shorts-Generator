"""
Vertical video conversion service.
Converts horizontal (16:9) video to vertical (9:16) with smart face tracking.
Includes dynamic multi-layout rendering: Solo speaker follow, Podcast split-screen, and Blurred fit.
"""
import cv2
import numpy as np
import subprocess
from typing import List, Tuple, Optional
from config import settings
from utils.logger import job_warning, logger

def _probe_size(video_path: str) -> Tuple[int, int]:
    """Return (width, height) of a video, releasing the capture on every path."""
    cap = cv2.VideoCapture(video_path)
    try:
        if not cap.isOpened():
            return 1920, 1080
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1920
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 1080
        return w, h
    finally:
        cap.release()

_mp_face_detector = None

def get_mp_face_detector():
    global _mp_face_detector
    if _mp_face_detector is None:
        try:
            import mediapipe as mp
            if hasattr(mp, "solutions") and hasattr(mp.solutions, "face_detection"):
                _mp_face_detector = mp.solutions.face_detection.FaceDetection(
                    model_selection=1, min_detection_confidence=0.5
                )
        except Exception:
            _mp_face_detector = False
    return _mp_face_detector if _mp_face_detector is not False else None

def detect_faces_in_frame(frame: np.ndarray) -> List[Tuple[int, int, int, int]]:
    """
    Detect faces in a video frame using MediaPipe / OpenCV fallback.

    Args:
        frame: OpenCV frame (BGR format)

    Returns:
        List of (x, y, w, h) bounding boxes
    """
    detector = get_mp_face_detector()
    if detector:
        try:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = detector.process(rgb_frame)
            if results and results.detections:
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
    try:
        if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data") and hasattr(cv2.data, "haarcascades"):
            if not hasattr(detect_faces_in_frame, "face_cascade"):
                cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                detect_faces_in_frame.face_cascade = cv2.CascadeClassifier(cascade_path)

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            detections = detect_faces_in_frame.face_cascade.detectMultiScale(
                gray, scaleFactor=1.1, minNeighbors=5, minSize=(30, 30)
            )
            return [(int(x), int(y), int(w), int(h)) for (x, y, w, h) in detections]
    except Exception:
        pass

    return []

def sample_frames_for_face_tracking(
    video_path: str,
    sample_interval: float = 0.5
) -> List[Tuple[float, List[Tuple[int, int, int, int]]]]:
    """
    Sample frames from video and detect faces in each.
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0

    frame_interval = max(1, int(fps * sample_interval))
    results = []
    frame_idx = 0

    try:
        while True:
            if frame_idx % frame_interval == 0:
                ret, frame = cap.read()
                if not ret:
                    break
                results.append((frame_idx / fps, detect_faces_in_frame(frame)))
            elif not cap.grab():
                break
            frame_idx += 1
    finally:
        cap.release()
    return results

def compute_crop_box(
    frame_width: int,
    frame_height: int,
    face_boxes: List[Tuple[int, int, int, int]]
) -> Tuple[int, int, int, int]:
    """
    Compute 9:16 crop box based on detected faces.
    """
    target_aspect = 9 / 16
    crop_height = frame_height
    crop_width = int(crop_height * target_aspect)

    if crop_width > frame_width:
        crop_width = frame_width
        crop_height = int(crop_width / target_aspect)

    if not face_boxes:
        crop_x = (frame_width - crop_width) // 2
        crop_y = (frame_height - crop_height) // 2
        return (crop_x, crop_y, crop_width, crop_height)

    center_x = sum(x + w / 2 for x, y, w, h in face_boxes) / len(face_boxes)
    center_y = sum(y + h / 2 for x, y, w, h in face_boxes) / len(face_boxes)

    crop_x = int(center_x - crop_width / 2)
    crop_y = int(center_y - crop_height / 2)

    crop_x = max(0, min(frame_width - crop_width, crop_x))
    crop_y = max(0, min(frame_height - crop_height, crop_y))

    return (crop_x, crop_y, crop_width, crop_height)

def _piecewise_expr(points: list, coord_idx: int) -> str:
    """Nested if(lt(t, ...)) expression interpolating one coordinate over time.

    ffmpeg's if() takes exactly three arguments, so the timeline is walked
    backwards: each keyframe wraps the previous result, and the innermost value
    holds the last keyframe. The clip() in every segment clamps the
    interpolation, which also freezes the first position before t0.
    """
    def seg_expr(p1, p2):
        t1, a1 = p1[0], p1[coord_idx]
        t2, a2 = p2[0], p2[coord_idx]
        if t2 <= t1:
            return f"{int(a1)}"
        return (
            f"{int(a1)}+({int(a2)}-{int(a1)})*"
            f"(clip((t-{t1:.3f})/{t2 - t1:.3f},0,1))"
        )

    expr = str(int(points[-1][coord_idx]))
    for i in range(len(points) - 1, 0, -1):
        expr = f"if(lt(t,{points[i][0]:.3f}),{seg_expr(points[i - 1], points[i])},{expr})"
    return expr


def _build_timed_crop(trajectory: list) -> str:
    """
    Convert a smoothed trajectory [(ts, x, y, w, h), ...] into an ffmpeg crop
    filter with piecewise-linear x/y expressions.

    Between keyframes the crop position is linearly interpolated (smooth
    follow, no jitter); before the first keyframe the first position is held,
    and after the last keyframe it is frozen. Falls back to a static center
    crop when the trajectory is empty or unusable.
    """
    if not trajectory or len(trajectory) < 1:
        return ""

    # Normalize entries (accept dicts or tuples)
    points = []
    for entry in trajectory:
        if isinstance(entry, dict):
            ts = entry.get("t", entry.get("ts", entry.get("time", 0)))
            x = entry.get("x", 0)
            y = entry.get("y", 0)
            w = entry.get("w", entry.get("crop_w", 0))
            h = entry.get("h", entry.get("crop_h", 0))
        else:
            ts, x, y, w, h = entry[:5]
        points.append((float(ts), float(x), float(y), float(w), float(h)))

    # Sort by timestamp
    points.sort(key=lambda p: p[0])

    # Use the first keyframe's crop size across the whole clip
    *_, cw, ch = points[0]
    cw = int(cw)
    ch = int(ch)
    if cw < 2 or ch < 2:
        return ""

    x_expr = _piecewise_expr(points, 1)
    y_expr = _piecewise_expr(points, 2)

    return f"crop={cw}:{ch}:x='{x_expr}':y='{y_expr}'"


def build_vertical_filter(
    input_video: str,
    mode: Optional[str] = None,
    target_width: int = 1080,
    target_height: int = 1920,
    diarization: Optional[list] = None,
    attention: Optional[dict] = None,
) -> str:
    """
    Build a filter_complex fragment `[0:v] ... [vsingle]` that converts the
    input to vertical (9:16) WITHOUT encoding. Used as the first stage of the
    single-pass render pipeline.

    Modes:
      - 'static': uniform center crop
      - 'fit_blur': centered fit over an atmospheric blurred 9:16 background
      - 'split_screen': two-speaker stacked split (only for a real two-shot)
      - 'single_speaker' / 'face_tracked': smooth YOLO follow of the subject

    'auto' resolves through :mod:`services.reframe_plan`, which also names the
    subject to follow: the person carrying the clip, or the object the clip is
    about. ``diarization`` (speaker turns) lets the voice pick the subject when
    it can be tied to a track, and ``attention`` (the clip's scenes) moves the
    crop onto the object being described; both are optional and change nothing
    when absent.
    """
    from services import reframe_plan

    analysis = reframe_plan.analyse_clip(input_video)
    if mode is None or mode in ("auto", "adaptive", ""):
        mode, focus = reframe_plan.decide_layout(analysis, diarization=diarization, attention=attention)
    else:
        focus = reframe_plan.choose_focus(analysis, diarization=diarization, attention=attention)

    from services.focus_tracking import (
        build_split_screen_filter,
        build_face_crop_filter,
        build_blurred_fit_filter as _build_blur,
    )

    if mode == "fit_blur":
        src_w, src_h = _probe_size(input_video)
        return _build_blur(
            src_w=src_w, src_h=src_h,
            target_w=target_width, target_h=target_height,
        ).replace("[outv]", "[vsingle]")

    if mode == "split_screen":
        try:
            two_shot = analysis.get("two_shot")
            if two_shot:
                boxes = [reframe_plan.representative_box(t) for t in two_shot]
            else:
                # An explicit request is honoured even when the detector found
                # no qualifying pair: take the two people the clip actually
                # shows, from the analysis rather than the first lucky frame.
                ranked = sorted(
                    analysis.get("tracks") or [],
                    key=lambda tr: len(tr["timeline"]),
                    reverse=True,
                )
                if len(ranked) < 2:
                    ranked = []
                boxes = [reframe_plan.representative_box(tr) for tr in ranked[:2]]
            if len(boxes) == 2:
                _src_w, _src_h = _probe_size(input_video)
                return build_split_screen_filter(
                    boxes[0], boxes[1],
                    src_w=_src_w, src_h=_src_h,
                    target_w=target_width, target_h=target_height,
                ).replace("[outv]", "[vsingle]")
        except Exception as e:
            logger.warning(f"split_screen reframing unavailable, falling back to static crop: {e}")
        # fall through to static

    if mode in ("face_tracked", "single_speaker"):
        try:
            src_w, src_h = _probe_size(input_video)
            samples = analysis.get("samples") or []
            if samples:
                frag = build_face_crop_filter(
                    samples, src_w, src_h,
                    target_width, target_height,
                    focus_boxes=(focus or {}).get("boxes"),
                )
                if frag:
                    return f"[0:v]{frag},scale={target_width}:{target_height}:force_original_aspect_ratio=decrease[vsingle]"
        except Exception as e:
            logger.warning(f"face-tracked reframing unavailable, falling back to static crop: {e}")
        # fall through to static

    # 'static' + all fallbacks: uniform center crop to 9:16
    return (
        f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height}[vsingle]"
    )


def make_vertical(
    input_video: str,
    output_video: str,
    target_width: int = 1080,
    target_height: int = 1920
) -> str:
    """
    Convert horizontal video to vertical 9:16 format with smart face tracking.
    """
    cap = cv2.VideoCapture(input_video)
    if not cap.isOpened():
        raise Exception(f"Could not open video file: {input_video}")

    orig_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    samples = sample_frames_for_face_tracking(input_video, sample_interval=0.5)

    if samples:
        all_faces = [face for _, faces in samples for face in faces]
        crop_x, crop_y, crop_w, crop_h = compute_crop_box(orig_width, orig_height, all_faces)
    else:
        target_aspect = 9 / 16
        crop_h = orig_height
        crop_w = int(crop_h * target_aspect)
        if crop_w > orig_width:
            crop_w = orig_width
            crop_h = int(crop_w / target_aspect)
        crop_x = (orig_width - crop_w) // 2
        crop_y = (orig_height - crop_h) // 2

    filter_complex = (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height}"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_video,
        "-vf", filter_complex,
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        output_video
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_video
    except subprocess.CalledProcessError as e:
        err = e.stderr
        error_msg = err.decode(errors='ignore') if isinstance(err, (bytes, bytearray)) else str(e)
        raise Exception(f"Vertical conversion failed: {error_msg}")

def make_vertical_static(
    input_video: str,
    output_video: str,
    target_width: int = 1080,
    target_height: int = 1920
) -> str:
    """
    Center crop without face tracking.
    """
    filter_complex = f"crop=ih*9/16:ih:(iw-ih*9/16)/2:0,scale={target_width}:{target_height}"
    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_video,
        "-vf", filter_complex,
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        output_video
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_video
    except subprocess.CalledProcessError as e:
        err = e.stderr
        error_msg = err.decode(errors='ignore') if isinstance(err, (bytes, bytearray)) else str(e)
        raise Exception(f"Static vertical conversion failed: {error_msg}")

def make_vertical_blurred_fit(
    input_video: str,
    output_video: str,
    target_width: int = 1080,
    target_height: int = 1920
) -> str:
    """
    Convert video to 9:16 using blurred background fit.
    """
    # Half-resolution backdrop blur, upscaled afterwards - same look, ~4x less
    # filter work (see build_blurred_fit_filter for the measurement).
    bg_w, bg_h = target_width // 2, target_height // 2
    filter_complex = (
        f"[0:v]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,crop={bg_w}:{bg_h},boxblur=16:2,scale={target_width}:{target_height}[bg];"
        f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_video,
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        output_video
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_video
    except subprocess.CalledProcessError as e:
        err = e.stderr
        error_msg = err.decode(errors='ignore') if isinstance(err, (bytes, bytearray)) else str(e)
        raise Exception(f"Blurred fit conversion failed: {error_msg}")

def make_vertical_split_screen(
    input_video: str,
    output_video: str,
    target_width: int = 1080,
    target_height: int = 1920
) -> str:
    """
    Convert horizontal video to 2-speaker vertical split-screen.
    Probes actual source dimensions and locates speaker bounding boxes
    before building the split-screen filter.
    """
    from services.focus_tracking import build_split_screen_filter, sample_frames

    # Probe real source dimensions
    cap = cv2.VideoCapture(input_video)
    if not cap.isOpened():
        raise Exception(f"Could not open video: {input_video}")
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()

    # Locate the two speaker boxes via YOLO sampling
    samples = sample_frames(input_video, sample_interval=0.5)
    persons_list = []
    for _ts, dets in samples:
        p = [d for d in dets if d.get("cls") == 0]
        if len(p) >= 2:
            p.sort(key=lambda d: d["box"][0])
            persons_list = p
            break

    if len(persons_list) < 2:
        # Fall back to static center-split when speakers can't be located
        job_warning(0, "make_vertical_split_screen: could not locate 2 speakers; falling back to static")
        return make_vertical_static(input_video, output_video, target_width, target_height)

    filter_complex = build_split_screen_filter(
        persons_list[0]["box"], persons_list[1]["box"],
        src_w=src_w, src_h=src_h,
        target_w=target_width, target_h=target_height,
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", input_video,
        "-filter_complex", filter_complex,
        "-c:v", "libx264",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        output_video
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_video
    except subprocess.CalledProcessError as e:
        err = e.stderr
        error_msg = err.decode(errors='ignore') if isinstance(err, (bytes, bytearray)) else str(e)
        raise Exception(f"Split-screen conversion failed: {error_msg}")
