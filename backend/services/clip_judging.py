"""Editorial judging and topic segmentation for clip selection.

Ideas adapted from Clipify AI (github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE,
CC BY-NC 4.0; non-commercial use, credit given). The code here is our own:

- the judge scores every candidate on eleven dimensions and returns keep/reject;
- a code-side score weighs the dimensions and applies penalties for incoherent,
  context-dependent or payoff-less clips and a bonus for the preferred length;
- editing modes (viral, funny, ...) steer what both passes look for;
- topic boundaries come from lexical cohesion between neighbouring stretches of
  transcript, so a clip can be extended to the end of the subject it is on.
"""

import math
import re
from collections import Counter
from typing import Dict, List, Optional

from config import settings

DIMENSIONS = (
    "hook", "curiosity", "emotion", "surprise", "story", "payoff",
    "pacing", "standalone", "rewatch", "conflict", "coherence",
)

# Relative weight of each dimension in the overall score (sums to 1).
_WEIGHTS = {
    "hook": 0.15, "curiosity": 0.10, "emotion": 0.08, "surprise": 0.08,
    "story": 0.10, "payoff": 0.14, "pacing": 0.08, "standalone": 0.10,
    "rewatch": 0.04, "conflict": 0.05, "coherence": 0.08,
}

DEFAULT_MODE = "viral"

EDIT_MODES: Dict[str, str] = {
    "viral": (
        "Favour scroll-stopping hooks, curiosity, surprise, stakes and a satisfying payoff. "
        "In interviews, podcasts and talks a bold claim, a forward-looking prediction or a "
        "sharp insight is a hook; clear, engaging discussion is worth keeping."
    ),
    "funny": (
        "Favour real humour: setup, punchline and the reaction after it, awkwardness, comic "
        "timing, the unexpected. Words like 'crazy' or 'insane' alone do not make a moment funny."
    ),
    "dramatic": (
        "Favour tension, danger, suspense, emotional escalation, reveals and strong reactions."
    ),
    "educational": (
        "Favour a useful explanation, a surprising fact or a clear teaching moment that ends in "
        "a self-contained takeaway."
    ),
    "story": (
        "Favour a complete mini-story: setup, escalation, turning point and payoff."
    ),
    "meme": (
        "Favour absurd, chaotic, highly reactive, remixable moments with strong comic timing."
    ),
    "gaming": (
        "Favour clutch plays, reveals, accusations, fails, wins, losses and reactions that make "
        "sense next to gameplay footage, each with a clear setup and payoff."
    ),
}


def normalise_mode(value: Optional[str]) -> str:
    mode = str(value or "").strip().lower()
    return mode if mode in EDIT_MODES else DEFAULT_MODE


def mode_guidance(mode: Optional[str]) -> str:
    mode = normalise_mode(mode)
    return f"Editing mode: {mode}. {EDIT_MODES[mode]}"


JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "decision": {"type": "string", "enum": ["keep", "reject"]},
                    **{name: {"type": "number"} for name in DIMENSIONS},
                    "reason": {"type": "string"},
                },
                "required": ["candidate_id", "decision", *DIMENSIONS, "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["ranked"],
    "additionalProperties": False,
}


def judge_instructions(num_clips: int, mode: Optional[str]) -> str:
    """The rubric shown above the candidates."""
    dims = (
        "- hook: do the first 1-3 seconds stop the scroll?\n"
        "- curiosity: is there a reason to keep watching?\n"
        "- emotion: tension, humour, shock, awe, a strong human reaction\n"
        "- surprise: an unexpected reveal or outcome\n"
        "- story: setup, escalation, resolution\n"
        "- payoff: a satisfying answer, result or punchline\n"
        "- pacing: energetic, little dead time\n"
        "- standalone: understandable with no context from the rest of the video\n"
        "- rewatch: would reward a second viewing\n"
        "- conflict: stakes, challenge, disagreement, danger\n"
        "- coherence: one focused topic from the first word to the last"
    )
    return f"""
{mode_guidance(mode)}

Judge each candidate as an editor, using only the words shown for it. Score every
dimension from 0 to 10:
{dims}

decision "keep": a publishable, coherent moment with an identifiable hook and payoff. It
need not be a perfect viral hit; a self-contained, interesting thought is a keep.
decision "reject": only when the clip is genuinely incoherent, needs essential missing
context, has no meaningful moment or payoff, or stops before the thought resolves.
Penalise clips that jump between unrelated topics, start weakly or end abruptly. Do not
reward generic words such as 'crazy' or 'this is wild' by themselves. The decision must
agree with the scores. Use the full range and score every candidate. At most {num_clips}
will be published, so be honest about which are weaker.
"""


def _clamp(value, low=0.0, high=10.0, default=5.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


def parse_judgement(row: dict) -> Optional[dict]:
    """One reply row -> {"decision", "dims", "reason"}, or None when it is not usable.

    A row with a missing or unknown decision is not an approval: it is treated as
    unjudged so the caller can keep the first pass's score instead.
    """
    if not isinstance(row, dict):
        return None
    decision = str(row.get("decision") or "").strip().lower()
    if decision not in ("keep", "reject"):
        return None
    dims = {}
    for name in DIMENSIONS:
        if name not in row:
            return None
        dims[name] = _clamp(row.get(name))
    return {"decision": decision, "dims": dims, "reason": str(row.get("reason") or "")[:240]}


def dimension_score(dims: Dict[str, float]) -> float:
    """Weighted mean of the dimensions on 0-1."""
    total = sum(_WEIGHTS[name] * _clamp(dims.get(name)) for name in DIMENSIONS)
    return max(0.0, min(1.0, total / 10.0))


def apply_penalties(score: float, dims: Dict[str, float], decision: str, duration: float) -> float:
    """Pull down clips that cannot stand alone; nudge up the preferred length."""
    if dims.get("coherence", 10.0) < 4.5:
        score -= 0.28  # an unfocused clip is the worst failure, so it pays twice
    if dims.get("standalone", 10.0) < 4.0:
        score -= 0.10
    if dims.get("payoff", 10.0) < 4.0:
        score -= 0.10
    if decision == "reject":
        score -= 0.12
    lo, hi = float(settings.MIN_CLIP_SECONDS), float(settings.MAX_CLIP_SECONDS)
    if duration < lo or duration > hi:
        score -= 0.15
    elif 30.0 <= duration <= 55.0:
        score += 0.03
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------- topic segmentation

_TOKEN = re.compile(r"[^\W\d_]{4,}", re.UNICODE)


def _tokens(text: str) -> List[str]:
    return [t.lower() for t in _TOKEN.findall(text or "")]


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a.keys() & b.keys())
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def topic_boundaries(segments: List[dict], block_words: int = 40, window: int = 3) -> List[float]:
    """Times where the subject of the talk changes.

    Segments are grouped into blocks of about `block_words` words. For each gap
    between blocks the vocabulary of the `window` blocks before it is compared with
    the `window` blocks after it; a gap where that similarity dips clearly below its
    surroundings is a topic break. Works on any language that has spaces between
    words, because it compares word overlap and nothing else.
    """
    blocks: List[dict] = []
    cur_tokens: List[str] = []
    cur_start: Optional[float] = None
    cur_end = 0.0
    count = 0
    for seg in segments:
        text = str(seg.get("text") or "")
        if cur_start is None:
            cur_start = float(seg["start"])
        cur_tokens += _tokens(text)
        count += len(text.split())
        cur_end = float(seg["end"])
        if count >= block_words:
            blocks.append({"start": cur_start, "end": cur_end, "tokens": Counter(cur_tokens)})
            cur_tokens, cur_start, count = [], None, 0
    if cur_start is not None and count:
        blocks.append({"start": cur_start, "end": cur_end, "tokens": Counter(cur_tokens)})
    if len(blocks) < 2 * window + 1:
        return []

    sims = []
    for gap in range(1, len(blocks)):
        left = sum((b["tokens"] for b in blocks[max(0, gap - window):gap]), Counter())
        right = sum((b["tokens"] for b in blocks[gap:gap + window]), Counter())
        sims.append(_cosine(left, right))

    # Depth of each valley relative to the nearest peaks on both sides.
    depths = []
    for i, value in enumerate(sims):
        left_peak = value
        for j in range(i - 1, -1, -1):
            if sims[j] < left_peak:
                break
            left_peak = sims[j]
        right_peak = value
        for j in range(i + 1, len(sims)):
            if sims[j] < right_peak:
                break
            right_peak = sims[j]
        depths.append((left_peak - value) + (right_peak - value))

    mean = sum(depths) / len(depths)
    std = math.sqrt(sum((d - mean) ** 2 for d in depths) / len(depths))
    cutoff = mean + 0.5 * std
    found = []
    for i, depth in enumerate(depths):
        if depth > cutoff and depth > 0.05:
            found.append(blocks[i + 1]["start"])
    return found


def fit_to_topic(start: float, end: float, boundaries: List[float],
                 max_len: float, reach: float = 18.0) -> tuple:
    """Move a window onto the topic it sits in.

    If the window stops shortly before the next topic break it is extended to it;
    if it starts shortly after the previous break it is pulled back to it. Both are
    capped by `max_len` and by `reach` seconds, so a window never swallows a whole
    new subject. Returns (start, end).
    """
    if not boundaries:
        return start, end
    after = [b for b in boundaries if b >= end - 0.5]
    if after and 0.5 < after[0] - end <= reach and after[0] - start <= max_len:
        end = after[0]
    before = [b for b in boundaries if b <= start + 0.5]
    if before and 0.0 < start - before[-1] <= 6.0 and end - before[-1] <= max_len:
        start = before[-1]
    return start, end
