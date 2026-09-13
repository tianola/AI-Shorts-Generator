"""
Modern YOLO-based focus tracking for vertical (9:16) conversion.

Replaces the simple MediaPipe-face center-tracker with a real object/person
focus system:

  - YOLOv8n detects PERSONS (primary) plus salient objects (tv, cell phone,
    laptop, sports ball, ...) as secondary focus anchors.
  - When two persons are present we alternate between the TOP person and
    BOTTOM person frames (the dynamic "one up / one down" edit).
  - One person -> smooth follow of their body/face center within a 9:16 crop.
  - No detection -> return [] so the caller falls back to static center crop.

The output is a list of (timestamp, crop_x, crop_y, crop_w, crop_h) that the
existing `_build_timed_crop` turns into a smooth ffmpeg crop trajectories.
"""
import os
import numpy as np
import cv2

# YOLO COCO classes we treat as "focus-worthy objects"
FOCUS_CLASSES = {
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
}

_model = None


def _get_yolo():
    """Lazy-load the YOLO model (yolov8n.pt). Raises if unavailable."""
    global _model
    if _model is None:
        from ultralytics import YOLO  # heavy import — do it lazily
        # Model path: CWD or the project utils dir. YOLO auto-downloads if missing.
        model_path = os.environ.get("YOLO_MODEL", "yolov8n.pt")
        _model = YOLO(model_path)
    return _model


def detect_objects_in_frame(frame, conf: float = 0.25) -> list:
    """Return [{'cls','conf','box':(x,y,w,h)} for focus-worthy detections]."""
    try:
        model = _get_yolo()
        res = model.predict(frame, conf=conf, verbose=False, device="cpu")
        if not res:
            return []
        boxes = res[0].boxes
        if boxes is None:
            return []
        out = []
        for b in boxes:
            cls = int(b.cls)
            if cls not in FOCUS_CLASSES:
                continue
            x1, y1, x2, y2 = [float(v) for v in b.xyxy[0]]
            w = x2 - x1
            h = y2 - y1
            out.append({"cls": cls, "name": FOCUS_CLASSES[cls],
                        "conf": float(b.conf), "box": (int(x1), int(y1), int(w), int(h))})
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
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        ts = count / fps
        if count % frame_interval == 0:
            dets = detect_objects_in_frame(frame, conf=0.25)
            # person detections come first so active-speaker logic sees them
            dets.sort(key=lambda d: (d["cls"] != 0, -d["conf"]))
            results.append((ts, dets))
        count += 1
    cap.release()
    return results


def _person_centers(dets):
    """[(cx, cy, area)] for person detections (class 0)."""
    centers = []
    for d in dets:
        if d["cls"] == 0:
            x, y, w, h = d["box"]
            centers.append((x + w // 2, y + h // 2, w * h))
    return centers


def compute_yolo_trajectory(samples, src_w, src_h,
                            target_w=1080, target_h=1920,
                            smooth=5) -> list:
    """
    Build a (timestamp, crop_x, crop_y, crop_w, crop_h) trajectory.

    Strategy:
      - No persons but objects: focus on the largest object's center.
      - One person: follow their center.
      - Two+ persons: alternate top/bottom (split-screen feel).
    Returns [] when nothing is detected (falls back to static crop).
    """
    # 9:16 crop window
    crop_w = int(src_h * target_w / target_h)
    crop_h = src_h
    if crop_w > src_w:
        crop_w = src_w
        crop_h = int(src_w * target_h / target_w)
    crop_w -= crop_w % 2
    crop_h -= crop_h % 2
    if crop_w < 2 or crop_h < 2:
        return []

    centers = []  # (cx, cy)
    valid_ts = []
    for ts, dets in samples:
        persons = [d for d in dets if d["cls"] == 0]
        if persons:
            pcenters = _person_centers(dets)
            if len(pcenters) == 1:
                cx, cy, _ = pcenters[0]
            else:
                # 2+ persons: alternate TOP (min y) then BOTTOM (max y)
                pcenters.sort(key=lambda p: p[1])  # by cy (y)
                top = pcenters[0]
                bottom = pcenters[-1]
                # pick the one closer to the frame's vertical center on even/odd sample
                # for a natural back-and-forth: alternate by index parity
                even = len(valid_ts) % 2 == 0
                cx, cy, _ = (top if even else bottom)
        else:
            # object focus: largest object
            objs = [d for d in dets if d["cls"] != 0]
            if not objs:
                continue  # no focus — skip this sample
            largest = max(objs, key=lambda d: d["box"][2] * d["box"][3])
            x, y, w, h = largest["box"]
            cx, cy = x + w // 2, y + h // 2
        centers.append((cx, cy))
        valid_ts.append(ts)

    if not centers:
        return []

    # Smooth with moving average
    centers = _moving_average(centers, smooth)

    trajectory = []
    for i, ts in enumerate(valid_ts):
        cx, cy = centers[i]
        crop_x = max(0, min(int(cx - crop_w // 2), src_w - crop_w))
        crop_y = max(0, min(int(cy - crop_h // 2), src_h - crop_h))
        trajectory.append((ts, crop_x, crop_y, crop_w, crop_h))
    return trajectory


def _moving_average(centers, window=5):
    arr = np.array(centers, dtype=np.float64)
    if len(arr) <= window:
        return arr.tolist()
    out = []
    for i in range(len(arr)):
        lo = max(0, i - window // 2)
        hi = min(len(arr), i + window // 2 + 1)
        out.append(arr[lo:hi].mean(axis=0).astype(int).tolist())
    return out


def build_face_crop_filter(samples, src_w, src_h,
                           target_w=1080, target_h=1920):
    """
    High-level helper: produce the ffmpeg ``crop`` filter for the YOLO
    trajectory, or "" if nothing detected (caller falls back to static).
    """
    traj = compute_yolo_trajectory(samples, src_w, src_h, target_w, target_h)
    if not traj:
        return ""
    from services.vertical import _build_timed_crop
    return _build_timed_crop(traj)