"""Correct misheard names in a clip's words before they are burned in as captions.

Whisper hears names it has no prior for as ordinary words. Job 112 (a history of
Fairchild Semiconductor) captioned "the Traitorous Eight" as "THE TRADERS ATE",
"Kleiner" as "CLINER" and "Fairchild" as "FAIR CHILD"; whisper small and medium
made the same mistakes on that audio, so a bigger model is not the fix. A
language model that reads the words in context, with the video's title and tags,
knows the names.

The correction is deliberately narrow: the model returns replacements by word
index, never a rewritten sentence, so every word keeps its own timing. Merging a
name split in two ("fair child") is the one structural change, done by giving
the merged spelling to the first word and an empty string to the rest. Anything
malformed or too sweeping is ignored and the words stay as Whisper wrote them.
"""

import json
import os
import re
from difflib import SequenceMatcher
from typing import Dict, List, Optional, Tuple

from loguru import logger

# A model that wants to change more than this share of the words is rewriting,
# not correcting names; keep Whisper's text in that case.
MAX_EDIT_SHARE = 0.25
MAX_WORD_CHARS = 32
MIN_SIMILARITY = 0.5

_SYSTEM = (
    "You fix speech-recognition errors in subtitles. You only correct words that "
    "were misheard: names of people, companies, places and products, technical "
    "terms, and numbers. You never rephrase, never fix grammar or style, never "
    "translate. Reply with JSON only."
)

_PROMPT = """A speech recognizer transcribed this excerpt of a video.
Video title: {title}
Video tags: {tags}

Words, numbered:
{numbered}

First work out what the video is about from the title, the tags and the words. The
recognizer does not know the people, groups, companies and events of that topic, so it
writes their names as ordinary words that sound alike - a famous group's nickname
heard as everyday words, a surname heard as a common word or split in two. Where the
context makes the real name clear, correct it.

Return {{"fixes": [{{"i": <number>, "word": "<corrected word>"}}, ...]}} for misheard words only.
Rules:
- One entry replaces exactly one numbered word. Keep that word's punctuation.
- A name split across words (e.g. "fair" "child" for Fairchild): put the joined name on the first number and "" on the others.
- A name heard as several words that should stay several words: fix each word on its own number.
- If nothing is misheard, return {{"fixes": []}}."""


def _word_text(word: dict) -> str:
    return str(word.get("word", word.get("text", "")) or "")


def _numbered(words: List[dict]) -> str:
    return " ".join(f"[{i}]{_word_text(w).strip()}" for i, w in enumerate(words))


def _letters(text: str) -> str:
    return re.sub(r"[^\w]", "", text.lower())


def _consonants(text: str) -> str:
    """A rough sound key: silent 'gh', common spellings folded, vowels dropped."""
    s = _letters(text)
    for a, b in (("gh", ""), ("ph", "f"), ("ck", "k"), ("c", "k"), ("q", "k"), ("z", "s"), ("y", "")):
        s = s.replace(a, b)
    s = re.sub(r"[aeiou]", "", s)
    return re.sub(r"(.)\1+", r"\1", s)


def sounds_like_a_mishearing(old: str, new: str) -> bool:
    """A correction must still look like what was heard.

    "traders"->"traitorous", "ate"->"eight", "Cliner"->"Kleiner" keep most of
    their letters; free/best also offered "token"->"tiny" on job 112, which is a
    different word, not a fix. An empty `new` (the tail of a merged name) and a
    case or punctuation change are always allowed.
    """
    a, b = _letters(old), _letters(new)
    if not b or a == b:
        return True
    if not a:
        return False
    if SequenceMatcher(None, a, b).ratio() >= MIN_SIMILARITY or b.startswith(a) or a in b:
        return True
    # Homophones spelled apart ("ate"/"eight") share their consonants.
    key_a, key_b = _consonants(a), _consonants(b)
    return bool(key_a) and key_a == key_b


def parse_fixes(raw: str, n_words: int) -> Dict[int, str]:
    """Valid {index: replacement} pairs from a model reply; {} when unusable."""
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    items = data.get("fixes") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}
    fixes: Dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        word = item.get("word")
        if not isinstance(word, str) or not (0 <= index < n_words):
            continue
        word = word.strip()
        if len(word) > MAX_WORD_CHARS or re.search(r"\s", word):
            continue
        fixes[index] = word
    return fixes


def apply_fixes(words: List[dict], fixes: Dict[int, str]) -> Tuple[List[dict], int]:
    """Apply index replacements; an empty replacement folds that word into the previous one.

    Returns the new word list and how many words changed.
    """
    fixes = {i: new for i, new in (fixes or {}).items()
             if sounds_like_a_mishearing(_word_text(words[i]), new)}
    if not fixes:
        return words, 0
    changed = sum(1 for i, new in fixes.items() if new != _word_text(words[i]).strip())
    if changed == 0 or changed > max(3, int(len(words) * MAX_EDIT_SHARE)):
        return words, 0
    out: List[dict] = []
    for i, word in enumerate(words):
        new = fixes.get(i)
        if new is None:
            out.append(word)
            continue
        if new == "":
            if out:
                # The joined name already sits on the previous word; it now ends here.
                prev = dict(out[-1])
                for key in ("end", "end_time"):
                    if key in word:
                        prev[key] = word[key]
                out[-1] = prev
                continue
            out.append(word)
            continue
        fixed = dict(word)
        key = "word" if "word" in word else ("text" if "text" in word else "word")
        original = _word_text(word)
        # Keep the original's leading space, which some transcripts carry, and its
        # closing punctuation: free/best returned "plan" for "plane." and the
        # caption grouping lost the sentence end.
        lead = original[: len(original) - len(original.lstrip())]
        trail = re.search(r"[.,!?;:]+$", original.strip())
        if trail and not re.search(r"[.,!?;:]$", new):
            new = new + trail.group(0)
        fixed[key] = lead + new
        out.append(fixed)
    return out, changed


async def fix_clip_words(words_file: str, title: str = "", tags: Optional[List[str]] = None,
                         model: Optional[str] = None) -> int:
    """Correct misheard names in ``words_file`` in place. Returns words changed (0 on any failure)."""
    try:
        with open(words_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning(f"caption fix: cannot read {words_file}: {exc}")
        return 0
    wrapped = isinstance(data, dict)
    words = data.get("words", []) if wrapped else data
    if not isinstance(words, list) or not words:
        return 0

    from services.llm_router import call_llm_json

    prompt = _PROMPT.format(
        title=title or "unknown",
        tags=", ".join((tags or [])[:30]) or "none",
        numbered=_numbered(words),
    )
    if model is None:
        from config import settings
        model = getattr(settings, "CAPTION_FIX_MODEL", "") or None
    try:
        raw = await call_llm_json(prompt, _SYSTEM, model=model, temperature=0.0, max_tokens=12000)
    except Exception as exc:
        logger.warning(f"caption fix: model call failed: {exc}")
        return 0
    new_words, changed = apply_fixes(words, parse_fixes(raw, len(words)))
    if not changed:
        return 0
    try:
        # Other stages read this file while the call runs; swap it in whole.
        tmp = words_file + ".fix.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**data, "words": new_words} if wrapped else new_words, f, ensure_ascii=False, indent=2)
        os.replace(tmp, words_file)
    except Exception as exc:
        logger.warning(f"caption fix: cannot write {words_file}: {exc}")
        return 0
    return changed
