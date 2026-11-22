"""Shared data contracts for the editing timeline.

Everything is clip-relative time in seconds (0 is the first frame of the cut clip, before any
speed transform). The timeline is plain JSON on disk (edit_timeline.json), so effects, captions,
sound and the preview all read the same record, and it can be inspected after the fact.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

TIMELINE_VERSION = 1

# What a moment of the clip is, before any decision about editing it.
EVENT_KINDS = (
    "hook",             # the opening of the clip
    "emotional_peak",   # the strongest moment of an emotionally intense clip
    "statement",        # a sentence the speaker stresses (!, CAPS)
    "question",         # a sentence that asks something
    "audio_peak",       # loudness well above the clip's normal level
    "scene_cut",        # a visual cut in the source
    "popup",            # the contextual popup card appears
)

# Effects the visual engine can apply, and sound categories the sound engine can place.
VISUAL_ACTIONS = ("zoom_punch", "shake", "flash", "tint_red")
AUDIO_ACTIONS = ("whoosh", "impact", "pop", "riser", "sub_boom")


class Event(BaseModel):
    id: str
    kind: str
    start: float
    end: float
    intensity: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    priority: int
    source: str                      # words | audio_energy | scene_detection | popup | clip_metadata
    tone: str = ""
    visual_actions: List[str] = Field(default_factory=list)
    audio_actions: List[str] = Field(default_factory=list)
    reason: str = ""


class VisualTrackItem(BaseModel):
    kind: str                        # one of VISUAL_ACTIONS
    start: float
    end: float
    intensity: float = Field(ge=0.0, le=1.0)
    event_id: str


class AudioTrackItem(BaseModel):
    kind: str                        # one of AUDIO_ACTIONS
    start: float
    gain_db: float
    event_id: str


class Suppressed(BaseModel):
    event_id: str
    action: str
    reason: str


class Timeline(BaseModel):
    version: int = TIMELINE_VERSION
    clip_duration: float
    profile: str
    tone: str = ""
    events: List[Event] = Field(default_factory=list)
    visual_tracks: List[VisualTrackItem] = Field(default_factory=list)
    audio_tracks: List[AudioTrackItem] = Field(default_factory=list)
    suppressed: List[Suppressed] = Field(default_factory=list)
    metadata: Dict[str, object] = Field(default_factory=dict)


def timeline_to_json(timeline: Timeline) -> str:
    return timeline.model_dump_json(indent=2)


def timeline_from_json(text: str) -> Optional[Timeline]:
    """The timeline in a JSON string, or None when it is not a timeline this code understands."""
    try:
        timeline = Timeline.model_validate_json(text)
    except ValueError:
        return None
    return timeline if timeline.version == TIMELINE_VERSION else None
