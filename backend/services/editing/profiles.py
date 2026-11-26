"""Editing profiles: how much editing a clip gets, in one table.

A profile decides which effects are allowed, how strong they may be, how many there can be, and
which sound categories go with them. The profile is chosen from the clip's tone
(services/tone_detector) and can be overridden by hand. Change a profile here and every stage
that reads the timeline follows; nothing else holds these numbers.
"""

from dataclasses import dataclass
from typing import Dict, FrozenSet, Optional, Tuple


@dataclass(frozen=True)
class EditingProfile:
    id: str
    allowed_visual: FrozenSet[str]
    allowed_audio: FrozenSet[str]
    max_intensity: float          # scales every effect's strength down to this ceiling
    max_events: int               # most visual effects on one clip
    min_spacing_s: float          # camera effects (zoom, shake) closer than this collide
    flash_spacing_s: float        # at most one flash per this many seconds
    tint_min_intensity: float     # the event must be at least this strong for a red tint
    sfx_gain_db: float            # level of sound effects before the sidechain duck
    caption_style: Optional[str]  # preferred caption preset; None keeps the job's choice


_ALL_AUDIO = frozenset({"whoosh", "impact", "pop", "riser", "sub_boom"})

PROFILES: Dict[str, EditingProfile] = {
    "dynamic": EditingProfile(
        id="dynamic",
        allowed_visual=frozenset({"zoom_punch", "shake", "flash"}),
        allowed_audio=frozenset({"whoosh", "impact", "pop"}),
        max_intensity=0.9, max_events=10, min_spacing_s=1.5, flash_spacing_s=1.0,
        tint_min_intensity=1.1, sfx_gain_db=-12.0, caption_style=None,
    ),
    "humorous": EditingProfile(
        id="humorous",
        allowed_visual=frozenset({"zoom_punch", "flash"}),
        allowed_audio=frozenset({"pop", "whoosh"}),
        max_intensity=0.8, max_events=8, min_spacing_s=1.5, flash_spacing_s=1.5,
        tint_min_intensity=1.1, sfx_gain_db=-12.0, caption_style=None,
    ),
    "dramatic": EditingProfile(
        id="dramatic",
        allowed_visual=frozenset({"zoom_punch", "shake", "tint_red"}),
        allowed_audio=frozenset({"impact", "riser", "sub_boom"}),
        max_intensity=0.8, max_events=6, min_spacing_s=2.0, flash_spacing_s=2.0,
        tint_min_intensity=0.8, sfx_gain_db=-14.0, caption_style=None,
    ),
    "educational": EditingProfile(
        id="educational",
        allowed_visual=frozenset({"zoom_punch"}),
        allowed_audio=frozenset({"pop", "whoosh"}),
        max_intensity=0.5, max_events=5, min_spacing_s=2.5, flash_spacing_s=3.0,
        tint_min_intensity=1.1, sfx_gain_db=-15.0, caption_style=None,
    ),
    "cinematic": EditingProfile(
        id="cinematic",
        allowed_visual=frozenset({"zoom_punch"}),
        allowed_audio=frozenset({"whoosh", "riser", "sub_boom"}),
        max_intensity=0.4, max_events=4, min_spacing_s=3.0, flash_spacing_s=3.0,
        tint_min_intensity=1.1, sfx_gain_db=-16.0, caption_style=None,
    ),
    # Serious or sensitive material: stable framing, no effects, no sound design.
    "documentary": EditingProfile(
        id="documentary",
        allowed_visual=frozenset(),
        allowed_audio=frozenset(),
        max_intensity=0.0, max_events=0, min_spacing_s=3.0, flash_spacing_s=3.0,
        tint_min_intensity=1.1, sfx_gain_db=-18.0, caption_style=None,
    ),
}

DEFAULT_PROFILE = "dynamic"

# How the caption words move for each profile (services/captions.CAPTION_MOTIONS). Fonts and
# colours stay the job's preset; this only picks the animation.
CAPTION_MOTION_BY_PROFILE: Dict[str, str] = {
    "dynamic": "kinetic",
    "humorous": "pop",
    "dramatic": "emphasis",
    "educational": "kinetic",
    "cinematic": "cinematic",
    "documentary": "cinematic",
}


def caption_motion_for(profile: EditingProfile) -> str:
    return CAPTION_MOTION_BY_PROFILE.get(profile.id, "kinetic")

# Tone (services/tone_detector.TONE_TAXONOMY) to profile.
TONE_TO_PROFILE: Dict[str, str] = {
    "humorous_entertainment": "humorous",
    "casual_podcast": "dynamic",
    "tech_educational": "educational",
    "business_strategy": "educational",
    "documentary_serious": "documentary",
    "somber_tragic": "documentary",
}


def profile_for_tone(tone: Optional[str], override: Optional[str] = None) -> EditingProfile:
    """The profile for a clip: a valid manual override wins, then the tone's, then the default."""
    if override and override in PROFILES:
        return PROFILES[override]
    return PROFILES[TONE_TO_PROFILE.get(tone or "", DEFAULT_PROFILE)]


def profile_ids() -> Tuple[str, ...]:
    return tuple(PROFILES)
