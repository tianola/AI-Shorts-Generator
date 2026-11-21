"""A manual trim is a derived artifact, not an edit to the render.

Why this exists
---------------
``POST /api/clips/{id}/trim`` used to cut the requested window and then
``os.replace`` the result over ``clip_dir/final_video.mp4``. That destroyed the
only copy of the render, made every later trim cut an already-trimmed file (the
windows compound: trim 5-20 s, then trim 0-10 s, and the clip is 5-15 s of the
original), and left ``Clip.trim_start``/``Clip.trim_end`` describing something no
file on disk matches - neither ``worker.process_clip``'s ``cut_clip`` nor
``api/clips.py``'s re-render reads those columns, so the next resume served the
untrimmed short and published what the operator had rejected.

The rule enforced here
----------------------
``final_video.mp4`` is the pristine render and stays that way. The trimmed cut is
``final_video.trimmed.mp4`` beside it, and like every other derived artifact it
is registered in the manifest ``utils/derived_cache.py`` keeps per directory,
keyed by everything it was built from:

    kind   = "clip_final_trimmed"
    inputs = {"clip_window": [3175.0, 3202.0],   # the render's own window
              "trim": [5.0, 20.0],               # where inside it the cut starts/ends
              "base": {"name": "final_video.mp4", "size": ..., "mtime_ns": ...}}

So a re-render that writes a new ``final_video.mp4`` invalidates the trim by
changing ``base``, and a resume whose selection moved the window invalidates it
by changing ``clip_window`` - the trimmed file stops being current instead of
quietly riding a render it was never cut from.

``trim_start``/``trim_end`` are **seconds into the rendered clip**, which is what
the trim request carries and what the Studio's inputs are measured against; the
render is a self-contained video that starts at 0, so the source-absolute window
is ``clip.start + trim_start``.
"""

from __future__ import annotations

import os
from typing import Any, Optional, Sequence

from utils import derived_cache

BASE_RENDER_NAME = "final_video.mp4"
TRIMMED_ARTIFACT_NAME = "final_video.trimmed.mp4"
TRIM_KIND = "clip_final_trimmed"


def base_render_path(clip_dir: Any) -> str:
    return os.path.join(str(clip_dir), BASE_RENDER_NAME)


def trimmed_path(clip_dir: Any) -> str:
    return os.path.join(str(clip_dir), TRIMMED_ARTIFACT_NAME)


def trim_inputs(base_render: str, clip_window: Sequence[float],
                start: float, end: float) -> dict:
    """The cache key: the render, the window it covers, and the cut inside it."""
    return {
        "clip_window": [float(clip_window[0]), float(clip_window[1])],
        "trim": [float(start), float(end)],
        "base": derived_cache.input_fingerprint(base_render),
    }


def is_trim_current(clip_dir: Any, clip_window: Sequence[float],
                    start: float, end: float) -> bool:
    return derived_cache.is_current(
        trimmed_path(clip_dir), TRIM_KIND,
        trim_inputs(base_render_path(clip_dir), clip_window, start, end))


def apply_trim(clip_dir: Any, clip_window: Sequence[float], start: float, end: float,
               *, force_reencode: bool = False) -> dict:
    """Cut ``[start, end)`` out of the pristine render into its own artifact.

    Never writes to ``final_video.mp4``. Returns the artifact path plus the trim
    service's report; ``method`` is ``"cached"`` when the file already comes from
    exactly this render and this window, which is the point of the key.

    Raises ``services.trim.TrimValidationError`` for a window the render cannot
    satisfy (inverted, negative, past its end) - before the service touches a
    file, which is that module's own guarantee.
    """
    from services.trim import trim_clip  # late: the API layer stubs this in tests

    base = base_render_path(clip_dir)
    out = trimmed_path(clip_dir)
    inputs = trim_inputs(base, clip_window, start, end)

    if derived_cache.is_current(out, TRIM_KIND, inputs):
        return {"path": out, "duration": float(end) - float(start),
                "method": "cached", "used_fallback": False}

    # Drop the claim before the build: a trim killed half-way must not still
    # look like this window's output.
    derived_cache.forget(out)
    result = trim_clip(base, start, end, out, force_reencode=force_reencode)
    derived_cache.record(out, TRIM_KIND, inputs)
    return {"path": out, **result}


def sync_stored_trim(clip_dir: Any, clip_window: Sequence[float],
                     trim_start: Optional[float],
                     trim_end: Optional[float]) -> Optional[str]:
    """Re-apply the operator's recorded trim to whatever is on disk now.

    Called after anything that rewrites ``final_video.mp4`` (a re-render, a smart
    blur), so the short that gets published is the one that was approved. Returns
    the artifact path, or None when there is no trim to apply. Lets
    ``TrimValidationError`` through when the recorded window does not fit the new
    render - silently serving the untrimmed cut is the failure being fixed.
    """
    if trim_start is None or trim_end is None:
        return None
    if not os.path.exists(base_render_path(clip_dir)):
        return None
    return apply_trim(clip_dir, clip_window, trim_start, trim_end)["path"]


def current_trimmed_relative(clip: Any, base_relative: Optional[str]) -> Optional[str]:
    """``base_relative``'s still-current trimmed sibling, relative to STORAGE_ROOT.

    This is what a worker run should record instead of the untrimmed render when
    the row carries a trim: the answer is only Yes when the manifest says the
    artifact was cut from this exact render and this exact window, so a row can
    never point at a trim of a render that no longer exists.
    """
    from config import settings

    if not base_relative or getattr(clip, "trim_start", None) is None \
            or getattr(clip, "trim_end", None) is None:
        return None
    base_absolute = base_relative if os.path.isabs(base_relative) \
        else os.path.join(settings.STORAGE_ROOT, base_relative)
    clip_dir = os.path.dirname(base_absolute)
    if os.path.basename(base_absolute) != BASE_RENDER_NAME:
        return None
    if not is_trim_current(clip_dir, (clip.start, clip.end),
                           clip.trim_start, clip.trim_end):
        return None
    return os.path.relpath(trimmed_path(clip_dir), settings.STORAGE_ROOT)
