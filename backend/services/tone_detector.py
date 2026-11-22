"""
Tone & Context Gating Service for Shorts Video Generation.
Detects narrative tone, prevents tonal dissonance on serious/tragic content,
and determines eligibility and visual style profile for image pop-ups.

The tone also decides how aggressive the editing may be (see services/editing), so the
safety gate has to work for every language the app handles. Greek is matched on text that
is lower-cased and stripped of accents, using word stems because Greek inflects.
"""
import re
import unicodedata
from typing import Dict, Any
from loguru import logger

# High-severity negative / somber keyword regex patterns (English)
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

# Greek stems, written without accents. Matched at the start of a word on accent-stripped
# text, so "θάνατος", "θανάτου" and "θανάτων" all hit "θανατ".
SOMBER_GREEK_STEMS = [
    "θανατ", "πεθαν", "πεθανε", "σκοτωθ", "σκοτωσ", "δολοφον", "αυτοκτον", "κηδει", "κηδευ",
    "πενθ", "βιασμ", "τρομοκρατ", "ομηρι", "βασανιστ", "καρκιν", "εγκεφαλικ", "εμφραγμ",
    "υπερδοσ", "σεισμ", "τσουναμ", "γενοκτον", "σφαγ", "νεκρ", "τραγωδι", "εκτελεσ",
    "παιδικη κακοποι", "ενδοοικογενειακ", "πολεμικο εγκλημ", "αεροπορικο δυστυχημ",
]

_SAFETY_REGEX = re.compile("|".join(SOMBER_CRITICAL_TERMS), re.IGNORECASE)
_GREEK_SAFETY_REGEX = re.compile(r"\b(?:" + "|".join(re.escape(s) for s in SOMBER_GREEK_STEMS) + r")")

# Category definitions and profiles. classify_content_tone() reads its values from here,
# so this table is the one place to change what a tone allows.
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

# Keyword lists per tone. English words are matched as whole words (so "ai" no longer
# fires inside "said"); Greek stems are matched at the start of an accent-free word.
_HUMOR_EN = ["funny", "hilarious", "laugh", "joke", "meme", "crazy", "insane", "roast", "lol", "lmao", "comedy"]
_HUMOR_EL = ["αστει", "γελι", "γελα", "χαχα", "μεμε", "τρελ", "ξεκαρδ", "καψουρ", "ρομποτ"]
_TECH_EN = ["ai", "model", "code", "software", "tech", "computer", "algorithm", "python", "tutorial", "how to", "developer", "system", "app"]
_TECH_EL = ["λογισμικ", "υπολογιστ", "αλγοριθμ", "προγραμματισμ", "εφαρμογ", "τεχνολογι", "κωδικα"]
_BUSINESS_EN = ["revenue", "money", "market", "startup", "invest", "stock", "growth", "strategy", "sales", "business", "dollar", "crypto", "bitcoin"]
_BUSINESS_EL = ["εσοδ", "χρηματ", "αγορα", "επενδυ", "μετοχ", "στρατηγικ", "πωλησ", "επιχειρ", "λεφτα", "κερδ"]
_DOC_EN = ["documentary", "historical", "parliament", "congress", "geopolitics", "investigation", "archaeology", "century"]
_DOC_EL = ["ντοκιμαντερ", "ιστορικ", "βουλη", "γεωπολιτικ", "ερευν", "αρχαιολογ", "αιωνα", "πολιτικ"]


def _fold(text: str) -> str:
    """Lower-case and accent-free (Greek tonos, final sigma), keeping letters and spaces."""
    decomposed = unicodedata.normalize("NFD", text or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


def _has_word(text: str, words) -> bool:
    return any(re.search(r"\b" + re.escape(w) + r"\b", text) for w in words)


def _has_stem(text: str, stems) -> bool:
    return any(re.search(r"\b" + re.escape(s), text) for s in stems)


def is_transcript_somber(transcript_text: str) -> bool:
    """
    Stage 0 deterministic filter:
    Returns True if the transcript matches critical somber, tragic, or safety-blocked terms
    in English or Greek.
    """
    if not transcript_text:
        return False
    if _SAFETY_REGEX.search(transcript_text):
        return True
    return bool(_GREEK_SAFETY_REGEX.search(_fold(transcript_text)))


def _result(category: str, reason: str) -> Dict[str, Any]:
    profile = TONE_TAXONOMY[category]
    return {
        "tone_category": category,
        "popup_eligible": profile["popup_eligible"],
        "style_profile": profile["style_profile"],
        "max_popups": profile["max_popups"],
        "reason": reason,
    }


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
        result = _result("casual_podcast", "Empty transcript default fallback")
        result.update({"style_profile": "stock_minimal", "max_popups": 1})
        return result

    # Stage 0: Deterministic safety blocklist
    if is_transcript_somber(transcript_text):
        logger.info("Tone Gating: Transcript blocked due to somber/sensitive keywords.")
        return _result("somber_tragic", "Deterministic safety blocklist match (somber/tragic content)")

    lower_text = transcript_text.lower()
    folded = _fold(transcript_text)
    story_lower = (story_type or "").lower()

    if _has_word(lower_text, _HUMOR_EN) or _has_stem(folded, _HUMOR_EL) or story_lower in ["reaction", "reveal"]:
        return _result("humorous_entertainment", "Humorous/entertainment keyword signals")

    if _has_word(lower_text, _TECH_EN) or _has_stem(folded, _TECH_EL):
        return _result("tech_educational", "Technical/educational keyword signals")

    if _has_word(lower_text, _BUSINESS_EN) or _has_stem(folded, _BUSINESS_EL):
        return _result("business_strategy", "Business/strategy keyword signals")

    if _has_word(lower_text, _DOC_EN) or _has_stem(folded, _DOC_EL):
        return _result("documentary_serious", "Serious documentary/political narrative")

    return _result("casual_podcast", "General conversational podcast")
