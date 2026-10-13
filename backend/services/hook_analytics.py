"""What the hook experiments actually returned.

A zero-view diagnostic job ships each clip with a different hook arm, and
services.youtube_stats writes the platform's numbers back onto those clips. This
is the other half: grouping the two so one style can be compared with another.

Nothing here guesses. A clip whose views were never synced is counted in
"clips" but not in "with_stats", and its number does not quietly become a zero -
averages over a half-synced table would rank the arms by how recently the sync
ran rather than by what audiences did.
"""
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from database.models import Clip

# Clips with no arm recorded shipped before the experiment was wired in, or came
# from a job that was not a diagnostic one. They are reported as their own group,
# because mixing them into an arm would credit that arm with views it never earned.
UNASSIGNED = "(none)"


def _median(values: List[int]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def summarize_hook_styles(db: Session, *, job_id: Optional[int] = None) -> Dict[str, dict]:
    """Per hook arm: how many clips shipped, how many published, and their views.

    Keyed by arm name, with "(none)" for clips that carry no arm. mean_views and
    median_views cover the synced clips only, so an arm whose videos have not
    been read back yet reports None rather than a flattering zero.
    """
    query = db.query(Clip)
    if job_id is not None:
        query = query.filter(Clip.job_id == job_id)

    groups: Dict[str, dict] = {}
    samples: Dict[str, List[int]] = {}
    for clip in query.all():
        arm = clip.hook_style or UNASSIGNED
        group = groups.setdefault(arm, {
            "clips": 0,
            "uploaded": 0,
            "with_stats": 0,
            "total_views": 0,
            "mean_views": None,
            "median_views": None,
            "top_clip_id": None,
            "top_views": None,
            "last_synced_at": None,
        })
        samples.setdefault(arm, [])
        group["clips"] += 1
        if clip.uploaded:
            group["uploaded"] += 1
        synced = clip.stats_synced_at
        if synced is not None and (group["last_synced_at"] is None or synced > group["last_synced_at"]):
            group["last_synced_at"] = synced
        if clip.view_count is None:
            continue
        group["with_stats"] += 1
        group["total_views"] += clip.view_count
        samples[arm].append(clip.view_count)
        if group["top_views"] is None or clip.view_count > group["top_views"]:
            group["top_views"] = clip.view_count
            group["top_clip_id"] = clip.id

    for arm, group in groups.items():
        views = samples[arm]
        if views:
            group["mean_views"] = round(sum(views) / len(views), 1)
            group["median_views"] = _median(views)

    # Best-performing arm first; arms with nothing measured yet go last, ordered
    # by name so the output is stable between runs.
    return dict(sorted(
        groups.items(),
        key=lambda kv: (kv[1]["mean_views"] is None, -(kv[1]["mean_views"] or 0.0), kv[0]),
    ))
