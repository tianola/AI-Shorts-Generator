"""Where the clip title card goes: high on the frame, lower only when the high strip is busy.

The title used to sit at one third from the top, which lands on a presenter's forehead or on
the middle of a screen recording. It now defaults to TITLE_CENTER_FRAC (high on the frame). A few
frames of the clip, already cropped to 9:16, are sampled and each candidate strip gets a
busyness score (mean edge strength). The title leaves the default strip only when that strip is
clearly busy and a fallback strip is clearly emptier, so a screen recording that is text all
over keeps its title high instead of hopping around.
"""
import subprocess
from typing import Dict, List, Optional, Sequence

import numpy as np
from loguru import logger

SAMPLE_W = 270
SAMPLE_H = 480
# Below this mean edge strength a strip is plain enough to write on (flat colour, soft blur).
BUSY_FLOOR = 6.0
# A fallback has to be at most this share of the default strip's busyness to win.
CLEARLY_EMPTIER = 0.6


def _frag_for_sampling(vertical_frag: str) -> str:
    """The vertical-crop fragment rewired to end in [tp_v], splitting [0:v] when used twice."""
    text = vertical_frag.strip()
    refs = text.count("[0:v]")
    prefix = ""
    if refs > 1:
        pads = "".join(f"[tpsplit{i}]" for i in range(refs))
        prefix = f"[0:v]split={refs}{pads};"
        for i in range(refs):
            text = text.replace("[0:v]", f"[tpsplit{i}]", 1)
    if text.endswith("]"):
        text = text[:text.rfind("[")]
    return prefix + text + "[tp_v]"


def sample_frames(input_video: str, start: float, end: float, vertical_frag: str,
                  count: int = 4, timeout: int = 120) -> List[np.ndarray]:
    """A few greyscale frames of the 9:16 picture the viewer will see, SAMPLE_W x SAMPLE_H."""
    duration = max(float(end) - float(start), 0.5)
    rate = count / duration
    graph = (_frag_for_sampling(vertical_frag)
             + f";[tp_v]fps={rate:.5f},scale={SAMPLE_W}:{SAMPLE_H},format=gray[tp_out]")
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-t", str(duration), "-i", input_video,
           "-filter_complex", graph, "-map", "[tp_out]", "-an",
           "-frames:v", str(count), "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=timeout, check=True).stdout
    size = SAMPLE_W * SAMPLE_H
    return [np.frombuffer(out[i:i + size], dtype=np.uint8).reshape(SAMPLE_H, SAMPLE_W)
            for i in range(0, len(out) - size + 1, size)]


def strip_busyness(frames: Sequence[np.ndarray], center_frac: float, height_frac: float) -> float:
    """Mean edge strength inside the strip the title card would cover, over all frames."""
    if not frames:
        return 0.0
    scores = []
    for frame in frames:
        h = frame.shape[0]
        top = max(0, int(round((center_frac - height_frac / 2) * h)))
        bottom = min(h, int(round((center_frac + height_frac / 2) * h)))
        if bottom - top < 2:
            continue
        band = frame[top:bottom].astype(np.int16)
        dx = np.abs(np.diff(band, axis=1)).mean()
        dy = np.abs(np.diff(band, axis=0)).mean()
        scores.append(float(dx + dy))
    return float(np.mean(scores)) if scores else 0.0


def choose_center(scores: Dict[float, float], default: float, fallbacks: Sequence[float]) -> float:
    """Keep the default strip unless it is busy and a fallback is clearly emptier."""
    busy = scores.get(default, 0.0)
    if busy < BUSY_FLOOR or not fallbacks:
        return default
    best = min(fallbacks, key=lambda f: scores.get(f, float("inf")))
    if scores.get(best, float("inf")) < busy * CLEARLY_EMPTIER:
        return best
    return default


def parse_fracs(text: str) -> List[float]:
    out = []
    for part in (text or "").split(","):
        try:
            value = float(part)
        except ValueError:
            continue
        if 0.05 <= value <= 0.8:
            out.append(value)
    return out


def pick_title_center(input_video: str, start: float, end: float, vertical_frag: str,
                      title_height_frac: float, default: float,
                      fallbacks: Sequence[float], enabled: bool = True) -> float:
    """The centre line for the title card. Any failure keeps the default."""
    fallbacks = [f for f in fallbacks if abs(f - default) > 1e-6]
    if not enabled or not fallbacks or not vertical_frag:
        return default
    try:
        frames = sample_frames(input_video, start, end, vertical_frag)
    except Exception as e:  # sampling is advisory; the render must not fail on it
        logger.warning(f"Title placement: could not sample frames ({e}); keeping the default")
        return default
    scores = {f: strip_busyness(frames, f, title_height_frac) for f in [default, *fallbacks]}
    chosen = choose_center(scores, default, fallbacks)
    logger.info("Title placement: centre {:.2f} (busyness {})".format(
        chosen, ", ".join(f"{f:.2f}={s:.1f}" for f, s in scores.items())))
    return chosen


def popup_top_below_title(title_center_frac: float, title_height_px: int,
                          default_top: int = 290, popup_height_px: int = 430,
                          frame_h: int = 1920, gap: int = 24) -> int:
    """Top edge for an upper-third popup card so it never sits on the title card."""
    title_top = title_center_frac * frame_h - title_height_px / 2
    title_bottom = title_center_frac * frame_h + title_height_px / 2
    if default_top + popup_height_px + gap <= title_top or default_top >= title_bottom + gap:
        return default_top
    # Just above the title when there is room under the platform's top bar, else below it.
    above = title_top - gap - popup_height_px
    if above >= 150:
        return int(round(above))
    return int(round(title_bottom + gap))


def title_center_for(mode: str, default: Optional[float] = None) -> float:
    """The fixed centre line per layout, before any busyness check."""
    if mode == "gaming":
        from services.gaming_layout import PANEL_SPLIT
        return 1.0 - PANEL_SPLIT
    if default is None:
        from config import settings
        default = float(settings.TITLE_CENTER_FRAC)
    return default
