"""
Tone & Context Gating Service for Shorts Video Generation.
Detects narrative tone, prevents tonal dissonance on serious/tragic content,
and determines eligibility and visual style profile for image pop-ups.
"""
import re
from typing import Dict, Any
from loguru import logger

# High-severity negative / somber keyword regex patterns
SOMBER_CRITICAL_TERMS = [
    # Death, grief, and mourning
    r"\b(death|died|dying|killed|murder|murdered|suicide|funeral|cremation|coffin|obituary|passed away)\b",
    r"\b(grief|grieving|mourning|loss of a loved one|fatal|fatality|fatalities|massacre|genocide)\b",
    # Violence, assault, and war
    r"\b(assault|rape|raped|sexual abuse|domestic violence|terrorist|terrorism|hostage|torture)\b",
    r"\b(gunshot|shooting|shooter|homicide|execution|war crime|war crimes)\b",
    # Catastrophic illnesses, health emergencies, and disasters
    r"\b(cancer|terminal illness|chemotherapy|stroke|heart attack|icu|intensive care|overdose)\b",
    r"\b(plane crash|shipwreck|train crash|explosion|famine|starvation|earthquake|tsunami)\b",
    # Severe legal, trauma, or criminal proceedings
    r"\b(custody battle|child abuse|indictment|sentenced to prison|convicted of felony)\b",
]

_SAFETY_REGEX = re.compile("|".join(SOMBER_CRITICAL_TERMS), re.IGNORECASE)

# Category definitions and profiles
TONE_TAXONOMY = {
    "humorous_entertainment": {
        "popup_eligible": True,
        "style_profile": "meme_bouncy",
        "max_popups": 2,
    },
    "casual_podcast": {
        "popup_eligible": True,
        "style_profile": "meme_bouncy",
        "max_popups": 2,
    },
    "tech_educational": {
        "popup_eligible": True,
        "style_profile": "stock_minimal",
        "max_popups": 1,
    },
    "business_strategy": {
        "popup_eligible": True,
        "style_profile": "stock_minimal",
        "max_popups": 1,
    },
    "documentary_serious": {
        "popup_eligible": False,
        "style_profile": "none",
        "max_popups": 0,
    },
    "somber_tragic": {
        "popup_eligible": False,
        "style_profile": "none",
        "max_popups": 0,
    },
}


def is_transcript_somber(transcript_text: str) -> bool:
    """
    Stage 0 deterministic filter:
    Returns True if the transcript matches critical somber, tragic, or safety-blocked terms.
    """
    if not transcript_text:
        return False
    return bool(_SAFETY_REGEX.search(transcript_text))


def classify_content_tone(transcript_text: str, story_type: str = "") -> Dict[str, Any]:
    """
    Classify transcript tone and compute popup gating verdict.

    Returns:
        dict with:
          - tone_category: str
          - popup_eligible: bool
          - style_profile: str ('meme_bouncy' | 'stock_minimal' | 'none')
          - max_popups: int
          - reason: str
    """
    if not transcript_text:
        return {
            "tone_category": "casual_podcast",
            "popup_eligible": True,
            "style_profile": "stock_minimal",
            "max_popups": 1,
            "reason": "Empty transcript default fallback",
        }

    # Stage 0: Deterministic safety blocklist
    if is_transcript_somber(transcript_text):
        logger.info("Tone Gating: Transcript blocked due to somber/sensitive keywords.")
        return {
            "tone_category": "somber_tragic",
            "popup_eligible": False,
            "style_profile": "none",
            "max_popups": 0,
            "reason": "Deterministic safety blocklist match (somber/tragic content)",
        }

    lower_text = transcript_text.lower()
    story_lower = (story_type or "").lower()

    # Comedy / Entertainment heuristics
    if any(k in lower_text for k in ["funny", "hilarious", "laugh", "joke", "meme", "crazy", "insane", "roast", "lol", "lmao", "comedy"]) or story_lower in ["reaction", "reveal"]:
        return {
            "tone_category": "humorous_entertainment",
            "popup_eligible": True,
            "style_profile": "meme_bouncy",
            "max_popups": 2,
            "reason": "Humorous/entertainment keyword signals",
        }

    # Tech / Educational heuristics
    if any(k in lower_text for k in ["ai", "model", "code", "software", "tech", "computer", "algorithm", "python", "tutorial", "how to", "developer", "system", "app"]):
        return {
            "tone_category": "tech_educational",
            "popup_eligible": True,
            "style_profile": "stock_minimal",
            "max_popups": 1,
            "reason": "Technical/educational keyword signals",
        }

    # Business / Strategy heuristics
    if any(k in lower_text for k in ["revenue", "money", "market", "startup", "invest", "stock", "growth", "strategy", "sales", "business", "dollar", "crypto", "bitcoin"]):
        return {
            "tone_category": "business_strategy",
            "popup_eligible": True,
            "style_profile": "stock_minimal",
            "max_popups": 1,
            "reason": "Business/strategy keyword signals",
        }

    # Documentary / Serious politics heuristics
    if any(k in lower_text for k in ["documentary", "historical", "parliament", "congress", "geopolitics", "investigation", "archaeology", "century"]):
        return {
            "tone_category": "documentary_serious",
            "popup_eligible": False,
            "style_profile": "none",
            "max_popups": 0,
            "reason": "Serious documentary/political narrative",
        }

    # Default: Casual Podcast
    return {
        "tone_category": "casual_podcast",
        "popup_eligible": True,
        "style_profile": "meme_bouncy",
        "max_popups": 2,
        "reason": "General conversational podcast",
    }
