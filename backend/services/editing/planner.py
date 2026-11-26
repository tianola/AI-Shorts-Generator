"""Decide which detected events become edits.

Deterministic: the same inputs always give the same timeline. Events are taken in priority order
(hook first, ordinary emphasis last); each wanted effect must pass the profile's allow-list, the
tone gate, the effect budget and the collision rules, or it is recorded as suppressed with the
reason. Detecting an event never forces an effect, and the planner is free to do nothing.
"""

from typing import Dict, List, Optional, Sequence

from services.editing.models import (
    AudioTrackItem, Event, Suppressed, Timeline, VisualTrackItem,
)
from services.editing.profiles import EditingProfile, profile_for_tone
from services.editing.signals import detect_events

# What each kind of event asks for, before the profile and the collision rules have their say.
WANTS = {
    "hook": {"visual": ["zoom_punch"], "audio": ["whoosh"]},
    "emotional_peak": {"visual": ["zoom_punch", "tint_red"], "audio": ["impact"]},
    "statement": {"visual": ["zoom_punch"], "audio": []},
    "audio_peak": {"visual": ["shake"], "audio": ["impact"]},
    "scene_cut": {"visual": ["shake"], "audio": ["whoosh"]},
    "popup": {"visual": ["flash"], "audio": ["pop"]},
    "question": {"visual": [], "audio": []},
}

DURATION_S = {"zoom_punch": 0.7, "shake": 0.35, "flash": 0.08, "tint_red": 0.6}
SHAKE_CUT_S = 0.15
CAMERA_EFFECTS = ("zoom_punch", "shake")
TINT_SPACING_S = 3.0
MAX_TINTS = 2
MAX_SOUNDS = 8
SOUND_REPEAT_S = 3.0           # the same sound category is not repeated closer than this
HEAVY_SOUND_INTENSITY = 0.9    # an emotional peak this strong gets a sub boom when the profile allows


def _near(start: float, others: Sequence[float], gap: float) -> bool:
    return any(abs(start - other) < gap for other in others)


def plan_timeline(
    events: List[Event],
    clip_duration: float,
    profile: EditingProfile,
    tone: str = "",
) -> Timeline:
    suppressed: List[Suppressed] = []
    visual: List[VisualTrackItem] = []
    audio: List[AudioTrackItem] = []
    kept_visual: Dict[str, List[str]] = {e.id: [] for e in events}
    kept_audio: Dict[str, List[str]] = {e.id: [] for e in events}

    def refuse(event: Event, action: str, reason: str) -> None:
        suppressed.append(Suppressed(event_id=event.id, action=action, reason=reason))

    ordered = sorted(events, key=lambda e: (-e.priority, e.start))
    for event in ordered:
        wants = WANTS.get(event.kind, {"visual": [], "audio": []})
        strength = min(event.intensity, profile.max_intensity)

        for action in wants["visual"]:
            # Loud passages alternate between a shake and a zoom, so a clip full of them does not
            # shake six times in a row.
            if (event.kind == "audio_peak" and action == "shake" and "zoom_punch" in profile.allowed_visual):
                last_camera = next((t for t in reversed(visual) if t.kind in CAMERA_EFFECTS), None)
                if last_camera is not None and last_camera.kind == "shake":
                    action = "zoom_punch"
            if action not in profile.allowed_visual:
                refuse(event, action, f"not allowed in the '{profile.id}' profile")
                continue
            if action == "tint_red" and event.intensity < profile.tint_min_intensity:
                refuse(event, action, f"intensity {event.intensity:.2f} is below the tint threshold "
                                      f"{profile.tint_min_intensity:.2f}")
                continue
            if len(visual) >= profile.max_events:
                refuse(event, action, f"effect budget of {profile.max_events} is used")
                continue
            if action in CAMERA_EFFECTS:
                clash = next((t for t in visual if t.kind in CAMERA_EFFECTS
                              and abs(t.start - event.start) < profile.min_spacing_s), None)
                if clash:
                    refuse(event, action, f"within {profile.min_spacing_s:g}s of {clash.kind} from {clash.event_id}")
                    continue
            elif action == "flash":
                if _near(event.start, [t.start for t in visual if t.kind == "flash"], profile.flash_spacing_s):
                    refuse(event, action, f"another flash within {profile.flash_spacing_s:g}s")
                    continue
            elif action == "tint_red":
                tints = [t for t in visual if t.kind == "tint_red"]
                if len(tints) >= MAX_TINTS or _near(event.start, [t.start for t in tints], TINT_SPACING_S):
                    refuse(event, action, "red tint already used nearby")
                    continue

            length = SHAKE_CUT_S if (action == "shake" and event.kind == "scene_cut") else DURATION_S[action]
            end = min(clip_duration, event.start + length)
            if end <= event.start:
                refuse(event, action, "no room before the end of the clip")
                continue
            visual.append(VisualTrackItem(kind=action, start=event.start, end=round(end, 3),
                                          intensity=round(strength, 3), event_id=event.id))
            kept_visual[event.id].append(action)

        # Sound goes with an event that was edited, or with a cut or popup, never on its own.
        wanted_sounds = list(wants["audio"])
        if (event.kind == "emotional_peak" and event.intensity >= HEAVY_SOUND_INTENSITY
                and "sub_boom" in profile.allowed_audio):
            wanted_sounds = ["sub_boom"]
        for action in wanted_sounds:
            if action not in profile.allowed_audio:
                refuse(event, action, f"not allowed in the '{profile.id}' profile")
                continue
            if not kept_visual[event.id] and event.kind not in ("scene_cut", "popup"):
                refuse(event, action, "no edit on this event, so no sound")
                continue
            if len(audio) >= MAX_SOUNDS:
                refuse(event, action, f"sound budget of {MAX_SOUNDS} is used")
                continue
            if _near(event.start, [a.start for a in audio if a.kind == action], SOUND_REPEAT_S):
                refuse(event, action, f"{action} already played within {SOUND_REPEAT_S:g}s")
                continue
            audio.append(AudioTrackItem(kind=action, start=event.start, gain_db=profile.sfx_gain_db,
                                        event_id=event.id))
            kept_audio[event.id].append(action)

    result_events = [
        e.model_copy(update={"visual_actions": kept_visual[e.id], "audio_actions": kept_audio[e.id]})
        for e in sorted(events, key=lambda e: e.start)
    ]
    return Timeline(
        clip_duration=round(clip_duration, 3),
        profile=profile.id,
        tone=tone,
        events=result_events,
        visual_tracks=sorted(visual, key=lambda t: (t.start, t.kind)),
        audio_tracks=sorted(audio, key=lambda a: (a.start, a.kind)),
        suppressed=suppressed,
        metadata={"planner": "deterministic", "event_count": len(events)},
    )


def build_timeline(
    words: Sequence[Dict],
    clip_duration: float,
    tone: str = "",
    profile_override: Optional[str] = None,
    energy: Optional[Sequence[float]] = None,
    energy_step_s: float = 0.25,
    scene_cuts: Optional[Sequence[float]] = None,
    popup_starts: Optional[Sequence[float]] = None,
    emotional_intensity: Optional[float] = None,
) -> Timeline:
    """Detect the events of a clip and plan its edits."""
    profile = profile_for_tone(tone, profile_override)
    events = detect_events(
        words, clip_duration, tone=tone, energy=energy, energy_step_s=energy_step_s,
        scene_cuts=scene_cuts, popup_starts=popup_starts, emotional_intensity=emotional_intensity,
    )
    return plan_timeline(events, clip_duration, profile, tone=tone)
