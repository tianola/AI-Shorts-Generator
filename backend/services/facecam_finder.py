"""Find the streamer's webcam in a gaming stream: a person who stays in one corner.

The face detector cannot find a streamer's webcam reliably: the face is small, dim, often in
a headset, and OpenCV's Haar cascade hits a static doll on a shelf instead. A person detector
(the YOLO model the project already uses) finds the whole person in the webcam, and a doll is
not a person. Gameplay also has people (NPCs), but they stand in the middle of the frame, not in
a corner or on an edge, so only corner and edge spots count. A webcam shows its person on
nearly every sampled frame; a passing NPC does not.
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np

REGIONS = {                         # where a webcam can sit, as (x0, y0, x1, y1) shares of the frame
    "top-left": (0.0, 0.0, 0.42, 0.50),
    "top-right": (0.58, 0.0, 1.0, 0.50),
    "bottom-left": (0.0, 0.50, 0.42, 1.0),
    "bottom-right": (0.58, 0.50, 1.0, 1.0),
    "left-edge": (0.0, 0.22, 0.22, 0.78),
    "right-edge": (0.78, 0.22, 1.0, 0.78),
}
SAME_SPOT = 0.03                    # two detections closer than this share of the frame are one spot
MIN_HIT_RATE = 0.5                  # the webcam person is found on at least this share of the frames
MAX_WIDTH = 0.22                    # a webcam person is narrower than this share of the frame; a player's body is not
MAX_HEIGHT = 0.5                    # ...and shorter than this share of the frame height
DETECT_CONF = 0.25
DETECT_SIZE = 960                   # the inference size; a webcam person is small in a 1280-wide frame


@dataclass
class Webcam:
    box: Tuple[int, int, int, int]  # the head and shoulders box in source pixels (x, y, w, h)
    region: str
    hit_rate: float
    src_w: int
    src_h: int


def _sample_frames(video_path: str, samples: int):
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [], 0, 0
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if total <= 0 or w <= 0 or h <= 0:
            return [], 0, 0
        frames = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * (i + 0.5) / samples))
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
        return frames, w, h
    finally:
        cap.release()


def _people(frames: List[np.ndarray]) -> List[List[Tuple[int, int, int, int]]]:
    """Person boxes (x, y, w, h) per frame, from the project's YOLO model. Empty when YOLO is unavailable."""
    from services.focus_tracking import _get_yolo

    try:
        model = _get_yolo()
        results = model.predict(frames, classes=[0], conf=DETECT_CONF, imgsz=DETECT_SIZE, verbose=False)
    except Exception:
        return [[] for _ in frames]
    out = []
    for result in results:
        boxes = []
        for x1, y1, x2, y2 in result.boxes.xyxy.tolist():
            boxes.append((int(x1), int(y1), int(x2 - x1), int(y2 - y1)))
        out.append(boxes)
    return out


def _region_of(cx: float, cy: float) -> Optional[str]:
    for name, (x0, y0, x1, y1) in REGIONS.items():
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return name
    return None


def _head(person: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
    """The head and shoulders of a person box: the part a facecam panel should centre on."""
    x, y, w, h = person
    return x + w // 4, y, max(1, w // 2), max(1, int(h * 0.35))


def find_webcams(video_path: str, samples: int = 24) -> List[Webcam]:
    """Every person spot in a corner or on an edge that is seen on enough frames, most-seen first."""
    frames, w, h = _sample_frames(video_path, samples)
    if not frames:
        return []
    spots: List[dict] = []
    for index, people in enumerate(_people(frames)):
        seen = set()
        for person in people:
            x, y, bw, bh = person
            if bw > w * MAX_WIDTH or bh > h * MAX_HEIGHT:
                continue
            cx, cy = (x + bw / 2) / w, (y + bh / 2) / h
            region = _region_of(cx, cy)
            if region is None or region in seen:
                continue
            seen.add(region)
            for spot in spots:
                if abs(spot["cx"] - cx) < SAME_SPOT and abs(spot["cy"] - cy) < SAME_SPOT:
                    spot["frames"].add(index)
                    spot["boxes"].append(person)
                    break
            else:
                spots.append({"cx": cx, "cy": cy, "frames": {index}, "boxes": [person], "region": region})
    result = []
    for spot in sorted(spots, key=lambda s: -len(s["frames"])):
        hit_rate = len(spot["frames"]) / len(frames)
        if hit_rate < MIN_HIT_RATE:
            continue
        person = tuple(int(np.median([b[i] for b in spot["boxes"]])) for i in range(4))
        result.append(Webcam(_head(person), spot["region"], round(hit_rate, 2), w, h))
    return result


def find_webcam(video_path: str, samples: int = 24) -> Optional[Webcam]:
    """The most-seen person spot, or None when the clip shows no webcam."""
    cams = find_webcams(video_path, samples)
    return cams[0] if cams else None
