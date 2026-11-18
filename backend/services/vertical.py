"""
Vertical video conversion service.
Converts horizontal (16:9) video to vertical (9:16) with smart face tracking.
Includes dynamic multi-layout rendering: Solo speaker follow, Podcast split-screen, and Blurred fit.
"""
import cv2
import numpy as np
from typing import List, Tuple, Optional
from utils.logger import logger


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

    if mode == "gaming":
        src_w, src_h = _probe_size(input_video)
        try:
            from services.gaming_layout import build_gaming_filter, detect_facecam
            cam = detect_facecam(input_video)
            if cam:
                return build_gaming_filter(src_w, src_h, cam, target_width, target_height
                                           ).replace("[outv]", "[vsingle]")
            logger.info("gaming layout: no facecam found, showing the whole frame over a blur")
        except Exception as e:
            logger.warning(f"gaming layout unavailable, using the blurred fit: {e}")
        return _build_blur(src_w=src_w, src_h=src_h, target_w=target_width,
                           target_h=target_height).replace("[outv]", "[vsingle]")

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
                    return f"[0:v]{frag},scale={target_width}:{target_height}:force_original_aspect_ratio=increase,crop={target_width}:{target_height}[vsingle]"
        except Exception as e:
            logger.warning(f"face-tracked reframing unavailable, falling back to static crop: {e}")
        # fall through to static

    # 'static' + all fallbacks: uniform center crop to 9:16
    return (
        f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)/2:0,"
        f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height}[vsingle]"
    )
