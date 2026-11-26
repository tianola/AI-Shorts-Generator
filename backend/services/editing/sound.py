"""From the timeline's sound tracks to the sound effects the renderer mixes.

The planner decides which categories of sound a clip earns and when (services/editing/planner).
This module turns each one into a file and a start time: it picks a file from the category in
a fixed rotation, and moves the sound into a gap between spoken words when there is one close
by. The mix then ducks every effect under the voice (sidechain compression in the renderer),
so a sound that cannot find a gap is still never louder than the speech.
"""

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from services.editing.models import Timeline

# Files by category, in rotation order. Names are looked up by services.sfx._find_sfx_file in
# the assets/sfx folders; a name with no file on disk is skipped.
SOUND_LIBRARY: Dict[str, List[str]] = {
    "whoosh": ["whoosh_1", "whoosh_2", "swoosh", "swish"],
    "impact": ["impact_1", "impact_2", "thump"],
    "pop": ["pop_1", "pop_2"],
    "riser": ["riser_1", "riser_2", "buildup"],
    "sub_boom": ["sub_boom_1", "sub_boom_2"],
}

MIN_GAP_S = 0.25            # a gap between words shorter than this is not a gap
SHIFT_WINDOW_S = 0.5        # a sound may move this far to reach a gap
IN_GAP_MARGIN_S = 0.1       # a sound starting this close to the end of a gap is not in it
NO_GAP_REDUCTION_DB = 6.0   # a light sound with no gap is turned down by this much
SKIPPED_WITHOUT_GAP = ("impact", "sub_boom", "riser")   # strong sounds are not laid over speech


def speech_gaps(words: Sequence[Dict], clip_duration: float, min_gap: float = MIN_GAP_S) -> List[Tuple[float, float]]:
    """The silent stretches of a clip as (start, end): before the first word, between words, after the last."""
    spans = []
    for w in words:
        try:
            spans.append((float(w.get("start", w.get("start_time", 0.0))), float(w.get("end", w.get("end_time", 0.0)))))
        except (TypeError, ValueError):
            continue
    spans.sort()
    gaps: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in spans:
        if start - cursor >= min_gap:
            gaps.append((cursor, start))
        cursor = max(cursor, end)
    if clip_duration - cursor >= min_gap:
        gaps.append((cursor, clip_duration))
    return gaps


def place_in_gap(start: float, gaps: Sequence[Tuple[float, float]],
                 window: float = SHIFT_WINDOW_S) -> Tuple[float, bool]:
    """(start, in_gap): the start if it already falls in a gap, else the start of the nearest gap
    within the window, else the start unchanged and in_gap False."""
    for g_start, g_end in gaps:
        if g_start <= start <= g_end - IN_GAP_MARGIN_S:
            return start, True
    nearest = None
    for g_start, g_end in gaps:
        distance = abs(g_start - start)
        if distance <= window and (nearest is None or distance < nearest[0]):
            nearest = (distance, g_start)
    if nearest:
        return nearest[1], True
    return start, False


def plan_sound_events(
    timeline: Timeline,
    words: Sequence[Dict],
    find_file: Callable[[str], Optional[str]],
) -> List[Dict]:
    """The sound effects to mix: [{path, start, gain_db, kind, event_id, note}] in time order.

    `note` says what was done beyond the plan ('moved into a gap', 'turned down: no gap'), so the
    choice can be explained later. Sounds with no file on disk, or a strong sound with no gap,
    are dropped.
    """
    gaps = speech_gaps(words, timeline.clip_duration)
    rotation: Dict[str, int] = {}
    last_file: Optional[str] = None
    events: List[Dict] = []
    for item in sorted(timeline.audio_tracks, key=lambda a: a.start):
        names = SOUND_LIBRARY.get(item.kind, [])
        path = None
        for step in range(len(names)):
            candidate = find_file(names[(rotation.get(item.kind, 0) + step) % len(names)])
            if candidate and candidate != last_file:
                path = candidate
                rotation[item.kind] = (rotation.get(item.kind, 0) + step + 1) % len(names)
                break
        if not path:
            continue
        start, in_gap = place_in_gap(item.start, gaps) if gaps else (item.start, False)
        gain = item.gain_db
        note = "moved into a gap" if in_gap and start != item.start else "in a gap" if in_gap else ""
        if not in_gap:
            if item.kind in SKIPPED_WITHOUT_GAP:
                continue
            gain -= NO_GAP_REDUCTION_DB
            note = "turned down: no gap"
        events.append({"path": path, "start": round(start, 3), "gain_db": round(gain, 1),
                       "kind": item.kind, "event_id": item.event_id, "note": note})
        last_file = path
    return sorted(events, key=lambda e: e["start"])
