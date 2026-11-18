"""Gaming layout: the gameplay on top, the streamer's facecam in its own panel below.

Streamers keep a small webcam in a corner of the capture. A face-follow crop either
loses the game or loses the face, and frame-by-frame switching jitters. Here the
facecam is found once per clip (a face that sits in the same corner on most sampled
frames), and the frame is composed from two stable panels. The idea follows Clipify AI
(github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE, CC BY-NC 4.0,
non-commercial); the implementation is our own.
"""

from typing import List, Optional, Tuple

import cv2

PANEL_SPLIT = 0.65          # share of the output height given to the gameplay
CORNER_FRACTION = 0.38      # a facecam's centre lies in this outer share of both axes
MAX_CAM_WIDTH = 0.34        # ...and it is narrower than this share of the frame
MIN_HIT_RATE = 0.5          # faces must be seen in the same corner on this share of samples
PAD = 0.45                  # grow the face box by this much of its size for hair/shoulders

Box = Tuple[int, int, int, int]


def corner_of(box: Box, src_w: int, src_h: int) -> Optional[str]:
    """"top-left" ... "bottom-right" when the box looks like a corner facecam, else None."""
    x, y, w, h = box
    if src_w <= 0 or src_h <= 0 or w <= 0 or h <= 0 or w > src_w * MAX_CAM_WIDTH:
        return None
    cx, cy = (x + w / 2) / src_w, (y + h / 2) / src_h
    horizontal = "left" if cx <= CORNER_FRACTION else "right" if cx >= 1 - CORNER_FRACTION else None
    vertical = "top" if cy <= CORNER_FRACTION else "bottom" if cy >= 1 - CORNER_FRACTION else None
    return f"{vertical}-{horizontal}" if horizontal and vertical else None


def pick_facecam(samples: List[List[Box]], src_w: int, src_h: int) -> Optional[Box]:
    """The facecam rectangle from per-frame face boxes, or None when there is none.

    `samples` has one list of detected faces per sampled frame. The corner that holds a
    face on at least MIN_HIT_RATE of the frames wins; the rectangle is the median of
    those boxes grown by PAD and clamped to the frame.
    """
    if not samples:
        return None
    by_corner = {}
    for faces in samples:
        seen = set()
        for box in faces:
            corner = corner_of(box, src_w, src_h)
            if corner and corner not in seen:
                seen.add(corner)
                by_corner.setdefault(corner, []).append(box)
    if not by_corner:
        return None
    _corner, boxes = max(by_corner.items(), key=lambda item: len(item[1]))
    if len(boxes) / len(samples) < MIN_HIT_RATE:
        return None

    def median(values):
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    x = median(b[0] for b in boxes)
    y = median(b[1] for b in boxes)
    w = median(b[2] for b in boxes)
    h = median(b[3] for b in boxes)
    grow_x, grow_y = int(w * PAD), int(h * PAD)
    left, top = max(0, x - grow_x), max(0, y - grow_y)
    right, bottom = min(src_w, x + w + grow_x), min(src_h, y + h + grow_y)
    return left, top, right - left, bottom - top


def detect_facecam(video_path: str, samples: int = 10) -> Optional[Box]:
    """Look for a corner facecam in evenly spaced frames of the clip."""
    from services.vertical import detect_faces_in_frame

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    try:
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
        src_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        src_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if frames <= 0 or src_w <= 0 or src_h <= 0:
            return None
        found = []
        for i in range(samples):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(frames * (i + 0.5) / samples))
            ok, frame = cap.read()
            if ok:
                found.append(detect_faces_in_frame(frame))
        return pick_facecam(found, src_w, src_h)
    finally:
        cap.release()


def build_gaming_filter(src_w: int, src_h: int, cam: Box,
                        target_w: int = 1080, target_h: int = 1920) -> str:
    """filter_complex for gameplay over facecam, ending in [outv].

    The gameplay panel keeps the whole frame visible (HUD, minimap) on a blurred
    copy of itself; the facecam panel is a cover-crop of the facecam rectangle.
    """
    game_h = int(target_h * PANEL_SPLIT) // 2 * 2
    cam_h = target_h - game_h
    cx, cy, cw, ch = cam
    cw, ch = max(16, cw // 2 * 2), max(16, ch // 2 * 2)
    bg_w, bg_h = target_w // 2, game_h // 2
    return (
        f"[0:v]split=3[gbg][gfg][cam];"
        f"[gbg]scale={bg_w}:{bg_h}:force_original_aspect_ratio=increase,crop={bg_w}:{bg_h},"
        f"boxblur=16:2,scale={target_w}:{game_h}[gb];"
        f"[gfg]scale={target_w}:-2[gf];"
        f"[gb][gf]overlay=(W-w)/2:(H-h)/2[game];"
        f"[cam]crop={cw}:{ch}:{cx}:{cy},scale={target_w}:{cam_h}:force_original_aspect_ratio=increase,"
        f"crop={target_w}:{cam_h}[camp];"
        f"[game][camp]vstack=inputs=2[outv]"
    )
