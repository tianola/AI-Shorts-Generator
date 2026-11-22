"""A/B check: did each planned visual effect actually show up in the shipped file?

The worker can keep a plain render of the same clip (same cut, layout, captions and title, but no
effects, no sound design and no emoji stickers) next to the finished one. For every visual effect
on the timeline this compares the plain frame with the finished frame at the same moment.

The shipped file also carries the colour shift and the small speed change of the anti-fingerprint
step, so two frames are never identical. The effect is therefore judged against a control: a moment
with no effect, compared the same way. An effect is `visible` when its frame differs from plain
clearly more than the control does, in structure (SSIM) or in pixel values (PSNR, which a
colour tint moves while SSIM barely does).
"""

import json
import os
from typing import Dict, List, Optional

from services import visual_regression as vr

AB_FILE = "ab_report.json"
SSIM_MARGIN = 0.005            # SSIM below the control by at least this much, or
PSNR_MARGIN_DB = 1.5           # PSNR below the control by at least this much (catches colour-only effects)
MAX_ITEMS = 12


def _control_time(duration: float, tracks: List[Dict]) -> Optional[float]:
    """The moment furthest from every visual effect, or None when the clip is too short."""
    if duration < 2.0:
        return None
    best, best_gap = None, -1.0
    for t in (duration * f / 20.0 for f in range(1, 20)):
        gap = min((max(0.0, tr["start"] - t, t - tr["end"]) for tr in tracks), default=duration)
        if gap > best_gap:
            best, best_gap = t, gap
    return best if best_gap >= 0.5 else None


def ab_report(plain: str, final: str, tracks: List[Dict], duration: float, speed: float,
              work_dir: str) -> Dict:
    """{control, speed, items}; each item has the effect, its clip and final time, ssim and visible."""
    speed = speed if speed and speed > 0 else 1.0
    items_src = sorted(tracks, key=lambda t: t["start"])[:MAX_ITEMS]
    times = [round((t["start"] + t["end"]) / 2.0, 3) for t in items_src]
    control_t = _control_time(duration, items_src)
    pairs = [(t, t / speed) for t in times] + ([(control_t, control_t / speed)] if control_t is not None else [])
    rows = vr.compare_video_frames(plain, final, pairs, work_dir)
    control = rows[-1] if control_t is not None else None
    control_ssim = control["ssim"] if control else None
    control_psnr = control["psnr"] if control else None

    items = []
    for track, row in zip(items_src, rows):
        ssim = row["ssim"]
        psnr = row["psnr"]
        visible = None
        if ssim is not None and psnr is not None and control_ssim is not None and control_psnr is not None:
            visible = ssim < control_ssim - SSIM_MARGIN or psnr < control_psnr - PSNR_MARGIN_DB
        items.append({"event_id": track.get("event_id"), "kind": track["kind"],
                      "time_clip": row["time_a"], "time_final": row["time_b"],
                      "ssim": ssim, "psnr": psnr, "visible": visible})
    return {"control": ({"time_clip": control["time_a"], "ssim": control_ssim, "psnr": control_psnr} if control else None),
            "speed": speed, "items": items}


def write_report(clip_dir: str, report: Dict) -> None:
    with open(os.path.join(clip_dir, AB_FILE), "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
