"""Glue between the worker and the editing timeline.

The worker calls plan_clip_edits() once per clip, after the popup is prepared and before the
render. It gathers what the pipeline already knows, plans the edits, saves edit_timeline.json
next to the clip, and returns the timeline. Any failure returns None: the clip then renders
exactly as it did before the timeline existed.
"""

import json
import os
from typing import Dict, List, Optional, Sequence

from loguru import logger

from services.editing.describe import explain
from services.editing.models import Timeline, timeline_to_json
from services.editing.planner import build_timeline

TIMELINE_FILE = "edit_timeline.json"
ENERGY_STEP_S = 0.25


def _load_words(words_file: str) -> List[Dict]:
    try:
        with open(words_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("words", [])
    return [w for w in data if isinstance(w, dict)] if isinstance(data, list) else []


def scene_cuts_in_window(events_file: Optional[str], clip_start: float, clip_end: float) -> List[float]:
    """Scene-cut times from the job's temporal_events.json, converted to clip-relative seconds."""
    if not events_file or not os.path.isfile(events_file):
        return []
    try:
        with open(events_file, "r", encoding="utf-8") as handle:
            events = json.load(handle)
    except (OSError, ValueError):
        return []
    cuts = []
    for item in events if isinstance(events, list) else []:
        if not isinstance(item, dict) or "scene" not in str(item.get("type", "")):
            continue
        try:
            t = float(item["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if clip_start <= t <= clip_end:
            cuts.append(round(t - clip_start, 3))
    return sorted(set(cuts))


def loudness_curve(media_path: str) -> Optional[List[float]]:
    """A 0.25 s loudness curve of the cut clip, or None when it cannot be measured."""
    try:
        from services.audio_energy_scorer import measure_energy
        return measure_energy(media_path, segment_duration=ENERGY_STEP_S)
    except Exception as exc:  # the curve only sharpens the plan; its absence must not stop a render
        logger.info(f"Editing timeline: no loudness curve ({exc})")
        return None


def plan_clip_edits(
    clip_dir: str,
    words_file: str,
    clip_duration: float,
    tone: str = "",
    clip_media: Optional[str] = None,
    popup_starts: Optional[Sequence[float]] = None,
    temporal_events_file: Optional[str] = None,
    clip_start: float = 0.0,
    emotional_intensity: Optional[float] = None,
    profile_override: Optional[str] = None,
    log=None,
) -> Optional[Timeline]:
    """Plan the clip's edits and save them. None when anything goes wrong."""
    try:
        # No words file means the clip's captions step did not run: there is nothing to plan from.
        if not os.path.isfile(words_file):
            logger.info(f"Editing timeline skipped: no words file at {words_file}")
            return None
        words = _load_words(words_file)
        timeline = build_timeline(
            words,
            clip_duration,
            tone=tone,
            profile_override=profile_override,
            energy=loudness_curve(clip_media) if clip_media else None,
            energy_step_s=ENERGY_STEP_S,
            scene_cuts=scene_cuts_in_window(temporal_events_file, clip_start, clip_start + clip_duration),
            popup_starts=[p for p in (popup_starts or []) if p is not None],
            emotional_intensity=emotional_intensity,
        )
        with open(os.path.join(clip_dir, TIMELINE_FILE), "w", encoding="utf-8") as handle:
            handle.write(timeline_to_json(timeline))
        if log:
            for line in explain(timeline):
                log(line)
        return timeline
    except Exception as exc:
        logger.warning(f"Editing timeline skipped: {exc}")
        return None
