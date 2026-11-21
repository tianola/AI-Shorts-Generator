"""Put a clip window on sentence boundaries, using word timestamps.

Job 113 shipped half its clips ending mid-sentence ("...he'll have lost nearly")
because the window was snapped to Whisper *segment* edges, and a segment is a
chunk of audio, not a sentence, and the window was capped at 30 s. Here the end
is moved to where the sentence that is being spoken actually finishes, even when
that makes the clip longer, up to MAX_CLIP_SECONDS. Only when the sentence runs
past that limit does the clip end earlier, at the last sentence that fits.

A sentence ends at a word carrying terminal punctuation, or before a long pause
(transcripts without punctuation still have breaths between thoughts).
"""

import json
import os
import re
from typing import List, Optional, Tuple

_TERMINAL = re.compile(r"[.!?…;;]['\"”’)\]]*$")
# A breath this long between two words ends a thought even without punctuation.
PAUSE_ENDS_SENTENCE = 0.7
# The first word's onset and the last word's tail are clipped by Whisper's word
# timing; a little room either side stops the cut landing on the syllable.
HEAD_PAD = 0.12
TAIL_PAD = 0.35
# How far the start may move to reach the beginning of a sentence.
START_SEARCH_BEFORE = 4.0
START_SEARCH_AFTER = 2.5


def load_words(path: str) -> List[dict]:
    """Word list from words.json, or [] when missing or unreadable."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("words") or []
    words = []
    for w in data if isinstance(data, list) else []:
        try:
            words.append({"word": str(w.get("word") or ""), "start": float(w["start"]),
                          "end": float(w["end"])})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    words.sort(key=lambda w: w["start"])
    return words


def sentence_end_indices(words: List[dict], use_pauses: bool = True) -> List[int]:
    """Indices of words that close a sentence.

    With use_pauses False only punctuation counts: a narrator breathing after
    "markets and even politics," has not finished the sentence (job 113 clip 4).
    """
    ends = []
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1]["start"] - w["end"]) if not last else 0.0
        if last or _TERMINAL.search(w["word"].strip()) or (use_pauses and gap >= PAUSE_ENDS_SENTENCE):
            ends.append(i)
    return ends


def snap_to_sentences(start: float, end: float, words: List[dict],
                      min_len: float, max_len: float) -> Optional[Tuple[float, float]]:
    """(start, end) on sentence edges inside [min_len, max_len], or None.

    The end is the close of the sentence being spoken at the proposed end, which
    can be later than proposed: finishing the thought beats hitting a length.
    """
    if not words or end <= start:
        return None
    # Punctuated sentences first; pauses only for a transcript that has none
    # where this clip is.
    window = _snap(start, end, words, sentence_end_indices(words, use_pauses=False),
                   min_len, max_len)
    if window is None:
        window = _snap(start, end, words, sentence_end_indices(words), min_len, max_len)
    return window


def _snap(start, end, words, ends, min_len, max_len):
    if not ends:
        return None
    starts = sorted({0} | {i + 1 for i in ends if i + 1 < len(words)})

    near = [i for i in starts
            if start - START_SEARCH_BEFORE <= words[i]["start"] <= start + START_SEARCH_AFTER]
    if near:
        si = min(near, key=lambda i: abs(words[i]["start"] - start))
    else:
        later = [i for i, w in enumerate(words) if w["start"] >= start - 0.3]
        if not later:
            return None
        si = later[0]
    first = words[si]["start"]

    room = max_len - TAIL_PAD
    fitting = [i for i in ends if i >= si and words[i]["end"] - first <= room]
    if not fitting:
        return None
    long_enough = [i for i in fitting if words[i]["end"] - first >= min_len]
    if not long_enough:
        return None
    # The sentence in progress at the proposed end, if it fits.
    completing = [i for i in long_enough if words[i]["end"] >= end - 0.6]
    ei = completing[0] if completing else long_enough[-1]

    clip_start = max(0.0, first - HEAD_PAD)
    if si > 0:
        clip_start = max(clip_start, words[si - 1]["end"] + 0.02)
    clip_end = words[ei]["end"] + TAIL_PAD
    if ei + 1 < len(words):
        clip_end = min(clip_end, max(words[ei]["end"], words[ei + 1]["start"] - 0.05))
    return round(clip_start, 2), round(clip_end, 2)
