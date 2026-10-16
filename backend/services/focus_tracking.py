"""
Modern YOLO-based focus tracking and multi-layout reframing for vertical (9:16) conversion.
Supports:
  - Solo Speaker Follow (with deadband & Exponential Moving Average smoothing)
  - Split-Screen Podcast (Top & Bottom Speaker stacked 9:16)
  - Blurred Fit (Atmospheric background for wide scenes)
"""
import os
import numpy as np
import cv2
from typing import Tuple, Optional

# YOLO COCO classes we treat as "focus-worthy objects".
# person is the tracking subject; the rest are the things a clip can be *about*
# (the demo on a desk, the pet in the shot, the product being described), which
# is what :mod:`services.scene_attention` matches spoken words against.
DEFAULT_FOCUS_CLASSES = {
    0: "person",
    62: "tv",
    63: "laptop",
    67: "cell phone",
    32: "sports ball",
    33: "kite",
    41: "cup",
    39: "bottle",
    53: "pizza",
    46: "banana",
    2: "car",
    15: "cat",
    16: "dog",
    65: "remote",
    66: "keyboard",
    73: "book",
}


def resolve_focus_classes(env_value: Optional[str] = None) -> dict:
    """The focus-worthy classes, overridable with YOLO_FOCUS_CLASSES.

    The override names classes (``laptop,cell phone,dog``), because a name is
    what every consumer here reads back — and an unknown name is dropped rather
    than silently widening the detector. ``person`` is always kept: the tracking
    that every layout decision reads would stop without it.
    """
    raw = (os.getenv("YOLO_FOCUS_CLASSES", "") if env_value is None else env_value).strip()
    if not raw:
        return dict(DEFAULT_FOCUS_CLASSES)
    by_name = {name.lower(): (cls_id, name) for cls_id, name in DEFAULT_FOCUS_CLASSES.items()}
    resolved = {0: "person"}
    for token in raw.split(","):
        hit = by_name.get(token.strip().lower())
        if hit:
            resolved[hit[0]] = hit[1]
    return resolved


FOCUS_CLASSES = resolve_focus_classes()

# COCO classes considered "unsafe content" for smart blurring. The ids here are
# the real COCO 80-class indices — the previous list was off by one in several
# places (29 is frisbee, 44 spoon, 45 bowl, 76 scissors, 77 teddy bear), so it
# blurred the wrong objects while naming the right ones.
UNSAFE_CLASSES = {
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    39: "bottle",
    40: "wine glass",
    42: "fork",
    43: "knife",
    44: "spoon",
    66: "keyboard",
    67: "cell phone",
}

# The actually-unsafe subset we blur by default: alcohol containers and blades.
# COCO has no tobacco / drug classes, so anything tighter would be guesswork.
# Overridable at runtime via SMART_BLUR_CLASSES (comma-separated COCO ids).
SMART_BLUR_DEFAULT_CLASSES = frozenset({39, 40, 43})

_model = None


def _get_yolo():
    """Lazy-load the YOLO model (yolov8n.pt). Raises if unavailable."""
    global _model
    if _model is None:
        from ultralytics import YOLO
        model_path = os.environ.get("YOLO_MODEL", "yolov8n.pt")
        _model = YOLO(model_path)
    return _model


def detect_objects_in_frame(frame: np.ndarray, conf: float = 0.25) -> list:
    """Return [{'cls','conf','box':(x,y,w,h)} for focus-worthy detections]."""
    try:
        model = _get_yolo()
        res = model.predict(frame, conf=conf, verbose=False, device="cpu")
        if not res or not res[0].boxes:
            return []
        boxes = res[0].boxes
        out = []
        for b in boxes:
            cls = int(b.cls)
            if cls not in FOCUS_CLASSES:
                continue
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            w = x2 - x1
            h = y2 - y1
            out.append({
                "cls": cls,
                "name": FOCUS_CLASSES[cls],
                "conf": float(b.conf),
                "box": (int(x1), int(y1), int(w), int(h))
            })
        return out
    except Exception:
        return []


def sample_frames(video_path: str, sample_interval: float = 0.5) -> list:
    """Sample frames and return [(timestamp, [{cls,name,conf,box}...]), ...]."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise Exception(f"Cannot open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0
    frame_interval = max(1, int(fps * sample_interval))
    results = []
    count = 0
    try:
        while True:
            if count % frame_interval == 0:
                ok, frame = cap.read()
                if not ok:
                    break
                dets = detect_objects_in_frame(frame, conf=0.25)
                dets.sort(key=lambda d: (d["cls"] != 0, -d["conf"]))
                results.append((count / fps, dets))
            else:
                # grab() demuxes without decoding — sampling a long source
                # with read() on every frame made this the slowest stage.
                if not cap.grab():
                    break
            count += 1
    finally:
        cap.release()
    return results


def _person_centers(dets: list) -> list:
    """[(cx, cy, area, box)] for person detections (class 0)."""
    centers = []
    for d in dets:
        if d["cls"] == 0:
            x, y, w, h = d["box"]
            centers.append((x + w // 2, y + h // 2, w * h, (x, y, w, h)))
    return centers


def compute_smooth_yolo_trajectory(
    samples: list,
    src_w: int,
    src_h: int,
    target_w: int = 1080,
    target_h: int = 1920,
    deadband_pct: float = 0.05,
    ema_alpha: float = 0.35,
    focus_boxes: Optional[list] = None
) -> list:
    """
    Build a smooth (timestamp, crop_x, crop_y, crop_w, crop_h) trajectory.
    Uses Deadband filtering and Exponential Moving Average (EMA) to prevent camera jitter.

    ``focus_boxes`` ([(timestamp, (x, y, w, h)), ...]) pins the follow to one
    subject — the person who is talking, or the object the clip is about.
    Without it the largest box in each frame wins, which in a three-person
    shot drifts to whoever happens to be closest to the camera. Samples where
    the focus is not in frame are skipped, so the crop holds its last position
    through a brief occlusion instead of jumping to a bystander.
    """
    crop_w = int(src_h * target_w / target_h)
    crop_h = src_h
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(src_w * target_h / target_w)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    if crop_w < 2 or crop_h < 2:
        return []

    raw_centers = []
    valid_ts = []

    focus_map = {round(float(ts), 4): box for ts, box in (focus_boxes or [])}
    for ts, dets in samples:
        if focus_map:
            box = focus_map.get(round(float(ts), 4))
            if box is None:
                continue
            x, y, w, h = box
            cx, cy = x + w / 2.0, y + h / 2.0
            raw_centers.append((cx, cy))
            valid_ts.append(ts)
            continue

        persons = [d for d in dets if d["cls"] == 0]
        if persons:
            pcenters = _person_centers(dets)
            # Focus on largest / most confident person
            pcenters.sort(key=lambda p: p[2], reverse=True)
            cx, cy, _, _ = pcenters[0]
        else:
            objs = [d for d in dets if d["cls"] != 0]
            if not objs:
                continue
            largest = max(objs, key=lambda d: d["box"][2] * d["box"][3])
            x, y, w, h = largest["box"]
            cx, cy = x + w // 2, y + h // 2

        raw_centers.append((cx, cy))
        valid_ts.append(ts)

    if not raw_centers:
        return []

    # Apply Deadband + Exponential Moving Average
    smoothed = []
    curr_cx, curr_cy = float(raw_centers[0][0]), float(raw_centers[0][1])
    deadband_px = src_w * deadband_pct

    for cx, cy in raw_centers:
        # If movement within deadband, keep current smoothed center
        if abs(cx - curr_cx) > deadband_px:
            curr_cx = curr_cx + ema_alpha * (cx - curr_cx)
        if abs(cy - curr_cy) > deadband_px:
            curr_cy = curr_cy + ema_alpha * (cy - curr_cy)
        smoothed.append((int(curr_cx), int(curr_cy)))

    trajectory = []
    for i, ts in enumerate(valid_ts):
        cx, cy = smoothed[i]
        crop_x = max(0, min(int(cx - crop_w // 2), src_w - crop_w))
        crop_y = max(0, min(int(cy - crop_h // 2), src_h - crop_h))
        trajectory.append((ts, crop_x, crop_y, crop_w, crop_h))

    return trajectory


# Alias for compatibility
compute_yolo_trajectory = compute_smooth_yolo_trajectory


def build_split_screen_filter(
    box_top: Tuple[int, int, int, int],
    box_bottom: Tuple[int, int, int, int],
    src_w: int,
    src_h: int,
    target_w: int = 1080,
    target_h: int = 1920
) -> str:
    """
    Build FFmpeg complex filter for 9:16 Podcast Split-Screen layout.
    Top speaker: 1080x960
    Bottom speaker: 1080x960
    Combined via vstack with smooth divider.
    """
    half_h = target_h // 2  # 960

    # Calculate crop around speaker 1 (top)
    x1, w1 = box_top[0], box_top[2]  # the crop spans the full source height
    cx1 = x1 + w1 // 2
    # 9:8 aspect ratio crop for half frame (1080x960 = 9:8)
    crop1_w = int(src_h * 9 / 8)
    crop1_w = min(crop1_w, src_w)
    crop1_x = max(0, min(int(cx1 - crop1_w // 2), src_w - crop1_w))

    # Calculate crop around speaker 2 (bottom)
    x2, w2 = box_bottom[0], box_bottom[2]
    cx2 = x2 + w2 // 2
    crop2_w = int(src_h * 9 / 8)
    crop2_w = min(crop2_w, src_w)
    crop2_x = max(0, min(int(cx2 - crop2_w // 2), src_w - crop2_w))

    filtergraph = (
        f"[0:v]split=2[in1][in2];"
        f"[in1]crop={crop1_w}:{src_h}:{crop1_x}:0,scale={target_w}:{half_h}[top];"
        f"[in2]crop={crop2_w}:{src_h}:{crop2_x}:0,scale={target_w}:{half_h}[bottom];"
        f"[top][bottom]vstack=inputs=2[stacked];"
        f"[stacked]drawbox=x=0:y={half_h-2}:w={target_w}:h=4:color=0x1E1E2E@0.9:t=fill[outv]"
    )
    return filtergraph


def build_blurred_fit_filter(
    src_w: int,
    src_h: int,
    target_w: int = 1080,
    target_h: int = 1920
) -> str:
    """Build FFmpeg complex filter for Blurred Background Fit layout."""
    # [0:v] is consumed twice, so it must be split explicitly — reusing one
    # label as two inputs makes the whole graph fail to initialise.
    #
    # The background is blurred at half resolution and then upscaled: boxblur
    # cost is proportional to pixel count and a backdrop this soft loses
    # nothing visible. Measured on a Ryzen 7 5700X (28 s of 1080x1920, same
    # graph otherwise): 24.2 s -> 6.4 s, side-by-side frames indistinguishable.
    # Radius/passes are tuned for a 1080x1920 target; the halved radius keeps
    # the apparent blur equal at half resolution.
    bg_w, bg_h = target_w // 2, target_h // 2
    return (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,"
        f"crop={bg_w}:{bg_h},boxblur=16:2,scale={target_w}:{target_h}[bg];"
        f"[fgsrc]scale={target_w}:-2[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[outv]"
    )


def build_face_crop_filter(
    samples: list,
    src_w: int,
    src_h: int,
    target_w: int = 1080,
    target_h: int = 1920,
    focus_boxes: Optional[list] = None
) -> str:
    """Produce the ffmpeg crop filter for the YOLO trajectory."""
    traj = compute_smooth_yolo_trajectory(samples, src_w, src_h, target_w, target_h, focus_boxes=focus_boxes)
    if not traj:
        return ""
    from services.vertical import _build_timed_crop
    return _build_timed_crop(traj)


# ===========================================================================
# Content safety: object-tracking smart blur
# ===========================================================================

def unsafe_class_ids() -> frozenset:
    """Resolve the set of COCO class ids to blur.

    Priority: SMART_BLUR_CLASSES env var (comma-separated ids) > default
    conservative set. Any valid COCO id is accepted so a user can blur classes
    that are not hardcoded here; class 0 (person) is always refused.
    """
    raw = os.environ.get("SMART_BLUR_CLASSES", "").strip()
    if raw:
        ids = set()
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                cls = int(part)
            except ValueError:
                continue
            if 0 < cls < 80:
                ids.add(cls)
        if ids:
            return frozenset(ids)
    return SMART_BLUR_DEFAULT_CLASSES


def detect_unsafe_objects_in_frame(
    frame: np.ndarray,
    classes: Optional[frozenset] = None,
    conf: float = 0.25,
) -> list:
    """Return [{'cls','conf','box':(x,y,w,h)}] for unsafe-class detections.

    Uses the same YOLO model as focus tracking, filtered to the unsafe class
    set. Never blurs people (class 0) even when a caller passes a set that
    nominally includes it.
    """
    if classes is None:
        classes = unsafe_class_ids()
    if not classes:
        return []
    try:
        model = _get_yolo()
        res = model.predict(frame, conf=conf, verbose=False, device="cpu")
        if not res or not res[0].boxes:
            return []
        boxes = res[0].boxes
        out = []
        for b in boxes:
            cls = int(b.cls)
            if cls == 0 or cls not in classes:
                continue
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            out.append({
                "cls": cls,
                "name": UNSAFE_CLASSES.get(cls, f"class_{cls}"),
                "conf": float(b.conf),
                "box": (int(x1), int(y1), int(x2 - x1), int(y2 - y1)),
            })
        return out
    except Exception:
        return []


def sample_unsafe_frames(
    video_path: str,
    classes: Optional[frozenset] = None,
    sample_interval: float = 0.5,
    conf: float = 0.25,
) -> list:
    """Sample frames and return [(timestamp, [unsafe detections...]), ...]."""
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0:
        fps = 30.0
    frame_interval = max(1, int(fps * sample_interval))
    results = []
    count = 0
    try:
        while True:
            if count % frame_interval == 0:
                ok, frame = cap.read()
                if not ok:
                    break
                dets = detect_unsafe_objects_in_frame(frame, classes=classes, conf=conf)
                if dets:
                    results.append((count / fps, dets))
            else:
                if not cap.grab():
                    break
            count += 1
    finally:
        cap.release()
    return results


def _smooth_box_trajectory(unsafe_samples: list, pad_pct: float = 0.25) -> list:
    """Merge per-sample unsafe detections into a single per-timestamp blur box.

    When multiple unsafe objects appear in one frame we blur their union
    (min/max of the boxes), padded by ``pad_pct`` of the box width/height.
    Returns [(ts, x, y, w, h), ...] clamped to frame bounds (assumed known by
    the caller via src_w/src_h on the first box).
    """
    keyframes = []
    for ts, dets in unsafe_samples:
        if not dets:
            continue
        x0 = min(d["box"][0] for d in dets)
        y0 = min(d["box"][1] for d in dets)
        x1 = max(d["box"][0] + d["box"][2] for d in dets)
        y1 = max(d["box"][1] + d["box"][3] for d in dets)
        w = x1 - x0
        h = y1 - y0
        pad_x = int(w * pad_pct)
        pad_y = int(h * pad_pct)
        keyframes.append((ts, x0 - pad_x, y0 - pad_y, w + 2 * pad_x, h + 2 * pad_y))
    return keyframes


def _build_blur_box_overlay(trajectory: list, src_w: int, src_h: int) -> str:
    """Build the ffmpeg filter that blurs a tracked (possibly moving) region.

    Uses the same piecewise-linear expression technique as the vertical crop
    tracker (_build_timed_crop) but applied to a boxblur clip + overlay mask.
    Falls back to a static black box (no blur) when the trajectory is empty.
    """
    if not trajectory:
        return ""

    # Normalize dict/tuple entries (same contract as _build_timed_crop)
    points = []
    for entry in trajectory:
        if isinstance(entry, dict):
            ts = entry.get("t", entry.get("ts", entry.get("time", 0)))
            x = entry.get("x", 0)
            y = entry.get("y", 0)
            w = entry.get("w", entry.get("box_w", 0))
            h = entry.get("h", entry.get("box_h", 0))
        else:
            ts, x, y, w, h = entry[:5]
        points.append((float(ts), float(x), float(y), float(w), float(h)))
    points.sort(key=lambda p: p[0])

    bw = max(2, int(points[0][3]))
    bh = max(2, int(points[0][4]))

    # crop aborts the whole filtergraph if the box exceeds the frame, and the
    # 25% padding can push a box off-screen, so clamp box and origin here.
    bw = max(2, min(bw, int(src_w)))
    bh = max(2, min(bh, int(src_h)))
    max_x = max(0, int(src_w) - bw)
    max_y = max(0, int(src_h) - bh)
    points = [
        (ts, min(max(int(x), 0), max_x), min(max(int(y), 0), max_y), bw, bh)
        for ts, x, y, _w, _h in points
    ]

    from services.vertical import _piecewise_expr
    x_expr = _piecewise_expr(points, 1)
    y_expr = _piecewise_expr(points, 2)

    # ffmpeg syntax: crop=x:y:w:h, boxblur, then overlay with enable between
    # the first and last keyframe (track the box while any unsafe object is
    # present; a static box would smear the blur across the whole clip).
    # The expressions are wrapped in '...' so the graph parser treats their
    # commas as literal — adding backslashes here would corrupt them instead.
    t_start = points[0][0]
    t_end = points[-1][0]
    blur_chain = (
        f"[0:v]split=2[blursrc][blurfg];"
        f"[blursrc]crop={bw}:{bh}:x='{x_expr}':y='{y_expr}',"
        f"boxblur=20:2[blur];"
        f"[blurfg][blur]overlay=x='{x_expr}':y='{y_expr}'"
        f":enable='between(t,{t_start:.3f},{t_end:.3f})'[outv]"
    )
    return blur_chain


def build_smart_blur_filter(
    trajectory: list,
    src_w: int,
    src_h: int,
    target_w: int = 1080,
    target_h: int = 1920,
) -> str:
    """Build an ffmpeg filter_complex fragment that blurs a tracked region.

    Parameters mirror build_face_crop_filter: the caller provides a sampled
    unsafe detection timeline and source dimensions. The returned string is a
    full filtergraph ending in [outv]:
        [0:v]crop=...,boxblur=...[blur];[0:v][blur]overlay=...[outv]

    When no unsafe detections are present an empty string is returned so the
    caller can skip the blur stage entirely.
    """
    if not trajectory:
        return ""
    keyframes = _smooth_box_trajectory(trajectory, pad_pct=0.25)
    if not keyframes:
        return ""
    return _build_blur_box_overlay(keyframes, src_w, src_h)


def get_smart_blur_filter_for_video(
    video_path: str,
    classes: Optional[frozenset] = None,
    sample_interval: float = 0.5,
    conf: float = 0.25,
) -> str:
    """One-shot convenience: sample a video for unsafe objects and return the
    ready-to-compose smart-blur ffmpeg fragment (or '' when nothing unsafe)."""
    if not video_path or not os.path.exists(video_path):
        return ""
    samples = sample_unsafe_frames(video_path, classes=classes, sample_interval=sample_interval, conf=conf)
    if not samples:
        return ""
    # determine source size
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return ""
    src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if src_w <= 0 or src_h <= 0:
        return ""
    return build_smart_blur_filter(samples, src_w, src_h)
