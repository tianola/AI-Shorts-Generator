"""Does the clip back up what the title says?

The rule-based checker in title_style.py catches length, hype and missing words. It cannot tell
that "He lost $10,000 in one hand" is false when the clip never mentions money. This module does
that in two steps:

1. Figures in the title that the clip never says are found in code and passed to the model as a hint.
2. One structured model call lists each claim the title makes and a verdict per claim, with the
   words from the clip that back it up. A "supported" verdict only counts when the quoted words
   really appear in the transcript; otherwise it is downgraded to "unclear".

A title with an unsupported claim is rejected, and the caller replaces it with the clip's own words.
If the model call fails, the title is kept: the code check still ran, and nothing was proven false.
"""

import json
import re
from typing import Dict, List, Optional, Tuple

from loguru import logger

from services.title_style import derive_from_transcript

CHECK_VERSION = "title-claims-1"
MIN_QUOTE_OVERLAP = 0.7

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 1000000, "billion": 1000000000}
_DIGITS = re.compile(r"\d[\d,.]*")
_YEAR = range(1900, 2101)


def _spoken_numbers(words: List[str]) -> set:
    """Numbers written as English words, runs combined: 'ten thousand' -> 10000, 'twenty five' -> 25."""
    found, total, current, active, numeric = set(), 0, 0, False, False
    for word in words + [""]:
        if word in _UNITS or word in _TENS or word in _SCALES:
            active = True
            if word in _UNITS:
                current += _UNITS[word]
            elif word in _TENS:
                current += _TENS[word]
            elif word == "hundred":
                current = max(current, 1) * 100
            else:
                total += max(current, 1) * _SCALES[word]
                current = 0
            if word in _UNITS or word in _TENS:
                numeric = True
        else:
            if active and numeric:
                found.add(float(total + current))     # "ten thousand" -> 10000; "million" alone is no figure
            total, current, active, numeric = 0, 0, False, False
    return found


def _figures(text: str) -> set:
    """The numbers a text mentions, as floats, whether written in digits or as English words."""
    found = set()
    text = re.sub(r"(\d)\s+([.,])(\d)", r"\1\2\3", text or "")      # word-by-word transcripts: "10 .8" -> "10.8"
    for raw in _DIGITS.findall(text):
        cleaned = raw.replace(",", "").rstrip(".")
        try:
            found.add(float(cleaned))
        except ValueError:
            continue
    return found | _spoken_numbers(re.findall(r"[a-z]+", (text or "").lower()))


def _mostly_latin(text: str) -> bool:
    letters = [c for c in text or "" if c.isalpha()]
    return bool(letters) and sum(1 for c in letters if c.isascii()) / len(letters) >= 0.7


def unsupported_numbers(title: str, transcript: str) -> List[float]:
    """Numbers in the title that the clip never says.

    Only judged when the clip is mostly English: number words in other languages are not parsed,
    and a year is read aloud in pieces ("twenty twenty four"), so both are left to the model check.
    0 and 1 are never judged.
    """
    if not _mostly_latin(transcript):
        return []
    spoken = _figures(transcript)
    # 0 and 1 are left out: titles use "1 trick" or "One ticket" where the clip says "a".
    return sorted(n for n in _figures(title) if n > 1 and n not in spoken and int(n) not in _YEAR)


def _words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def quote_in_transcript(quote: str, transcript: str) -> bool:
    """True when (nearly) all the words of the quote occur in the transcript, in order."""
    quote_words = _words(quote)
    if len(quote_words) < 2:
        return False
    spoken = _words(transcript)
    position, matched = 0, 0
    for word in quote_words:
        try:
            position = spoken.index(word, position) + 1
            matched += 1
        except ValueError:
            continue
    return matched / len(quote_words) >= MIN_QUOTE_OVERLAP


def _prompt(title: str, transcript: str, numbers=()) -> str:
    hint = (
        "These figures in the title are not spoken as written in the clip: "
        + ", ".join(f"{n:g}" for n in numbers)
        + ". Decide whether the clip supports each one in another form (a count of items, a different "
        "spelling); if it does not, the claim is unsupported.\n\n"
    ) if numbers else ""
    return hint + (
        f'Title: "{title}"\n\nWhat the clip says:\n"{transcript[:2500]}"\n\n'
        "List every factual claim or promise the title makes (an event, a number, a person, a "
        "result, an emotion it says someone felt). For each, decide:\n"
        '  "supported"   - the clip says it, and you can quote the words that show it\n'
        '  "unsupported" - the clip says something different, or nothing about it\n'
        '  "unclear"     - the clip hints at it but does not state it\n'
        "For \"supported\", copy the exact words from the clip into \"quote\". Judge only from the "
        "clip's words. A title that is a question or a tease is fine if what it points at is in the "
        'clip. Return JSON only: {"claims": [{"claim": "...", "verdict": "supported|unsupported|unclear", "quote": "..."}]}'
    )


def _parse_claims(raw: str, transcript: str) -> List[Dict]:
    data = json.loads(raw)
    claims = []
    for item in data.get("claims", []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).lower()
        if verdict not in ("supported", "unsupported", "unclear"):
            continue
        quote = str(item.get("quote") or "")
        if verdict == "supported" and not quote_in_transcript(quote, transcript):
            verdict, quote = "unclear", ""      # a verdict with no evidence in the clip does not stand
        claims.append({"claim": str(item.get("claim", ""))[:200], "verdict": verdict, "quote": quote[:300]})
    return claims


async def verify_title_claims(title: str, transcript: str, caller=None) -> Dict:
    """{accepted, checked, reasons, claims, version}. `caller` replaces the model call in tests."""
    result = {"accepted": True, "checked": False, "reasons": [], "claims": [], "version": CHECK_VERSION}
    if not title or not (transcript or "").strip():
        return result

    # A figure the clip never says is a hint for the model, not a verdict: titles count things
    # ("3 words", "4 pages") and write amounts in other forms ("$2.5M"), which code cannot tell apart.
    bad_numbers = unsupported_numbers(title, transcript)
    result["numbers_not_spoken"] = bad_numbers

    try:
        if caller is None:
            from services.llm_router import call_llm_json, pick_model

            async def caller(prompt):
                return await call_llm_json(
                    prompt=prompt, system="You check video titles against what the video says. JSON only.",
                    model=pick_model(), temperature=0.0, max_tokens=700)
        claims = _parse_claims(await caller(_prompt(title, transcript, bad_numbers)), transcript)
    except Exception as exc:
        logger.warning(f"Title claim check skipped: {type(exc).__name__}: {exc}")
        return result

    result["checked"] = True
    result["claims"] = claims
    for claim in claims:
        if claim["verdict"] == "unsupported":
            result["accepted"] = False
            result["reasons"].append(f'not backed by the clip: "{claim["claim"]}"')
    return result


async def resolve_title(title: str, transcript: str, topic: str = "", caller=None) -> Tuple[str, Optional[Dict]]:
    """The title to burn in, and the check's record. A rejected title becomes the clip's own words."""
    check = await verify_title_claims(title, transcript, caller=caller)
    if check["accepted"]:
        return title, check
    replacement = derive_from_transcript(transcript, topic)
    if not replacement:
        return title, check              # nothing true to replace it with; the record still says why
    check["replaced"] = True
    check["original_title"] = title
    check["final_title"] = replacement
    return replacement, check
