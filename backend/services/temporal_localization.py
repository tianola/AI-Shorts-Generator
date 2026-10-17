"""Temporal event localization for the full source video.

Builds a timeline of visually interesting moments by combining YOLO object
tracking, scene-change detection, and transcript alignment.  Runs between
audio-energy scoring and clip selection so the clip-picker knows *what
happens* when, not just *what is said* when.

No VLM required — uses the same YOLO detector the reframe pipeline already
depends on, plus ffmpeg scene-change filters.  When a VLM provider becomes
available the ``vlm_enrich`` tier can upgrade the timeline, but the base
tier is fully local.
"""
import json
import os
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

# ---------------------------------------------------------------------------
# Scene-change detection (ffmpeg, no model needed)
# ---------------------------------------------------------------------------

def detect_scene_changes(video_path: str, threshold: float = 0.3) -> List[float]:
    """Return timestamps where ffmpeg detects a scene cut.

    Uses the ``select`` filter with scene-score; any frame whose diff from
    the previous exceeds *threshold* is a cut.
    """
    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene\\,{threshold})',showinfo",
        "-f", "null", "-"
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        logger.warning("ffmpeg not found — scene-change detection skipped")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg scene detection timed out")
        return []

    timestamps: List[float] = []
    for line in result.stderr.splitlines():
        if "pts_time:" in line:
            try:
                pts = line.split("pts_time:")[1].split()[0]
                timestamps.append(float(pts))
            except (IndexError, ValueError):
                continue
    logger.info(f"Detected {len(timestamps)} scene changes (threshold={threshold})")
    return timestamps


# ---------------------------------------------------------------------------
# Visual event extraction from YOLO samples
# ---------------------------------------------------------------------------

def _track_objects(
    samples: List[Tuple[float, list]],
) -> List[Dict[str, Any]]:
    """Walk YOLO samples and emit events for object appearances/disappearances.

    An "event" is a timestamp where a named object class enters or leaves the
    frame for at least 2 consecutive samples (to suppress flicker).
    """
    MIN_PERSIST = 2
    events: List[Dict[str, Any]] = []

    confirmed_present: set = set()
    pending_appear: Dict[str, int] = {}
    pending_disappear: Dict[str, int] = {}

    for ts, dets in samples:
        current_names = set()
        for d in dets:
            name = d.get("name", "").lower().strip()
            if name:
                current_names.add(name)

        for name in current_names:
            if name not in confirmed_present:
                pending_appear[name] = pending_appear.get(name, 0) + 1
                if pending_appear[name] >= MIN_PERSIST:
                    events.append({
                        "t": ts,
                        "type": "object_appear",
                        "name": name,
                        "importance": 0.6,
                    })
                    confirmed_present.add(name)
                    pending_appear.pop(name, None)
            if name in pending_disappear:
                pending_disappear.pop(name)

        for name in list(confirmed_present):
            if name not in current_names:
                pending_disappear[name] = pending_disappear.get(name, 0) + 1
                if pending_disappear[name] >= MIN_PERSIST:
                    events.append({
                        "t": ts,
                        "type": "object_disappear",
                        "name": name,
                        "importance": 0.3,
                    })
                    confirmed_present.discard(name)
                    pending_disappear.pop(name, None)
            else:
                pending_disappear.pop(name, None)

        for name in list(pending_appear.keys()):
            if name not in current_names:
                pending_appear.pop(name)

    return events


def _track_person_count(
    samples: List[Tuple[float, list]],
) -> List[Dict[str, Any]]:
    """Emit events when the number of people on screen changes."""
    events: List[Dict[str, Any]] = []
    prev_count = -1

    for ts, dets in samples:
        count = sum(1 for d in dets if d.get("cls") == 0)
        if prev_count < 0:
            prev_count = count
            continue
        if count != prev_count and abs(count - prev_count) >= 1:
            if count > prev_count:
                events.append({
                    "t": ts,
                    "type": "person_enter",
                    "count": count,
                    "importance": 0.7,
                })
            elif count < prev_count and prev_count > 0:
                events.append({
                    "t": ts,
                    "type": "person_leave",
                    "count": count,
                    "importance": 0.4,
                })
        prev_count = count

    return events


# ---------------------------------------------------------------------------
# Transcript alignment — boost events near spoken words
# ---------------------------------------------------------------------------

def _align_with_transcript(
    events: List[Dict[str, Any]],
    segments: List[Dict[str, Any]],
    window: float = 3.0,
) -> List[Dict[str, Any]]:
    """Boost importance when an event coincides with transcript content.

    If a spoken word within *window* seconds mentions the event's object name,
    the importance goes up — the camera shows what the speaker talks about.
    """
    for ev in events:
        ev_t = ev["t"]
        ev_name = ev.get("name", "")
        for seg in segments:
            if abs(seg["start"] - ev_t) > window and abs(seg["end"] - ev_t) > window:
                continue
            text = seg.get("text", "").lower()
            if ev_name and ev_name in text:
                ev["importance"] = min(1.0, ev["importance"] + 0.25)
                ev["transcript_aligned"] = True
                break
    return events


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def localise_video(
    video_path: str,
    segments: List[Dict[str, Any]],
    sample_interval: float = 2.0,
) -> List[Dict[str, Any]]:
    """Build a full-video temporal event timeline.

    Combines YOLO object tracking, person-count changes, and scene cuts into
    a single sorted list of events with importance scores.  Transcript
    alignment boosts events where the speaker names what appears on screen.

    Args:
        video_path: Path to the full source video
        segments: Transcript segments (from segments.json)
        sample_interval: Seconds between YOLO samples (2.0 = every 2s)

    Returns:
        Sorted list of event dicts with ``t``, ``type``, ``importance``.
    """
    from services.focus_tracking import sample_frames

    logger.info(f"Temporal localization: sampling {video_path} every {sample_interval}s")

    try:
        samples = sample_frames(video_path, sample_interval=sample_interval)
    except Exception as e:
        logger.error(f"YOLO sampling failed: {e}")
        samples = []

    events: List[Dict[str, Any]] = []

    if samples:
        events.extend(_track_objects(samples))
        events.extend(_track_person_count(samples))

    scene_cuts = detect_scene_changes(video_path)
    for t in scene_cuts:
        events.append({"t": t, "type": "scene_change", "importance": 0.5})

    events = _align_with_transcript(events, segments)
    events.sort(key=lambda e: e["t"])

    logger.info(f"Temporal localization: {len(events)} events "
                f"({sum(1 for e in events if e['type'] == 'object_appear')} object appears, "
                f"{sum(1 for e in events if e['type'] == 'person_enter')} person enters, "
                f"{len(scene_cuts)} scene changes)")

    return events


def events_for_clip_window(
    events: List[Dict[str, Any]],
    start: float,
    end: float,
) -> List[Dict[str, Any]]:
    """Filter events to those within a clip's time window."""
    return [e for e in events if start <= e["t"] <= end]


def format_events_for_prompt(events: List[Dict[str, Any]]) -> str:
    """Format events as a text block for the clip-selection LLM prompt."""
    if not events:
        return ""

    lines = ["Visual Events Timeline (from video analysis):"]
    for ev in events:
        t = ev["t"]
        etype = ev["type"]
        imp = ev["importance"]
        if etype == "object_appear":
            lines.append(f"  [{t:.1f}s] '{ev.get('name', '?')}' appears on screen (importance: {imp:.2f})")
        elif etype == "object_disappear":
            lines.append(f"  [{t:.1f}s] '{ev.get('name', '?')}' leaves screen")
        elif etype == "person_enter":
            lines.append(f"  [{t:.1f}s] New person enters ({ev.get('count', '?')} people total, importance: {imp:.2f})")
        elif etype == "person_leave":
            lines.append(f"  [{t:.1f}s] Person leaves ({ev.get('count', '?')} people remaining)")
        elif etype == "scene_change":
            lines.append(f"  [{t:.1f}s] Scene change / cut")
    return "\n".join(lines)


def save_events(events: List[Dict[str, Any]], path: str) -> None:
    """Persist events to JSON for downstream stages."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)


def load_events(path: str) -> List[Dict[str, Any]]:
    """Load events from JSON."""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
