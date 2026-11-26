"""Turn the analysis the pipeline already has into events.

Nothing here measures anything new: it reads word times, the loudness curve, scene cuts, popup
cues and the clip's tone and emotional intensity, and says what happens when. Detecting an event
is not a decision to edit it; that is the planner's job.
"""

import re
import statistics
from typing import Dict, List, Optional, Sequence

from services.editing.models import Event

PRIORITY = {
    "hook": 100,
    "emotional_peak": 90,
    "statement": 80,
    "scene_cut": 65,
    "audio_peak": 55,
    "popup": 50,
    "question": 40,
}

SENTENCE_GAP_S = 0.7
HOOK_SECONDS = 1.2
EDGE_MARGIN_S = 0.5          # no cut or peak events this close to the ends of the clip
PEAK_SIGMA = 1.0
PEAK_PERCENTILE = 0.85
MIN_RANGE = 0.1              # the loudest sample must stand at least this far above the mean
MAX_PEAKS = 6
PEAK_SEPARATION_S = 3.0
MIN_PEAK_S = 0.2


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _word_text(word: Dict) -> str:
    return str(word.get("word", word.get("text", "")) or "").strip()


def _word_time(word: Dict, key: str, alt: str, default: float = 0.0) -> float:
    try:
        return float(word.get(key, word.get(alt, default)))
    except (TypeError, ValueError):
        return default


def split_sentences(words: Sequence[Dict]) -> List[Dict]:
    """Sentences as {start, end, text, exclaim, caps, question}, split on .!? and on long pauses."""
    sentences: List[Dict] = []
    current: List[Dict] = []

    def flush():
        if not current:
            return
        texts = [_word_text(w) for w in current]
        joined = " ".join(t for t in texts if t)
        letters = [t for t in texts if len(re.sub(r"[^\w]", "", t)) >= 3]
        shouted = [t for t in letters if re.sub(r"[^\w]", "", t).isupper() and re.sub(r"[^\w]", "", t).isalpha()]
        sentences.append({
            "start": _word_time(current[0], "start", "start_time"),
            "end": _word_time(current[-1], "end", "end_time"),
            "text": joined,
            "exclaim": "!" in joined,
            "question": joined.rstrip().endswith("?"),
            # One shouted word of four or more letters is enough ("He NEVER saw it"); shorter
            # ones are mostly acronyms (AI, US, TV). Otherwise a third of the words must be.
            "caps": any(len(re.sub(r"[^\w]", "", t)) >= 4 for t in shouted)
                    or (len(shouted) >= 1 and len(shouted) / max(1, len(letters)) >= 0.34),
        })
        current.clear()

    previous_end: Optional[float] = None
    for word in words:
        start = _word_time(word, "start", "start_time")
        if previous_end is not None and start - previous_end > SENTENCE_GAP_S:
            flush()
        current.append(word)
        previous_end = _word_time(word, "end", "end_time", start)
        if _word_text(word).endswith((".", "!", "?")):
            flush()
    flush()
    return sentences


def audio_peaks(energy: Sequence[float], step_s: float) -> List[Dict]:
    """Runs where loudness is more than PEAK_SIGMA above the clip's mean: {start, end, level, strength}."""
    if len(energy) < 4:
        return []
    mean = statistics.fmean(energy)
    spread = statistics.pstdev(energy)
    if spread < 0.02 or (max(energy) - mean) < MIN_RANGE:   # a flat curve has no peaks, only noise
        return []
    # Delivered clips are loudness-normalised, so their curves are compressed (measured on real
    # clips: spread about 0.12, never 2 sigma above the mean). The bar is therefore the higher of
    # one sigma above the mean and the clip's own 85th percentile, and only the strongest few count.
    ranked = sorted(energy)
    percentile = ranked[min(len(ranked) - 1, int(len(ranked) * PEAK_PERCENTILE))]
    threshold = max(mean + PEAK_SIGMA * spread, percentile)
    peaks: List[Dict] = []
    run_start: Optional[int] = None
    for i, value in enumerate(list(energy) + [-1.0]):
        if value >= threshold and run_start is None:
            run_start = i
        elif value < threshold and run_start is not None:
            level = max(energy[run_start:i])
            if (i - run_start) * step_s >= MIN_PEAK_S - 1e-9:
                peaks.append({
                    "start": run_start * step_s,
                    "end": i * step_s,
                    "level": level,
                    "strength": _clip01(0.6 + 0.4 * (level - threshold) / max(1e-6, 1.0 - threshold)),
                })
            run_start = None
    # Strongest first, but no two within PEAK_SEPARATION_S, so the picks spread over the clip
    # instead of crowding the one loud passage.
    chosen: List[Dict] = []
    for p in sorted(peaks, key=lambda p: -p["level"]):
        if len(chosen) >= MAX_PEAKS:
            break
        if all(abs(p["start"] - c["start"]) >= PEAK_SEPARATION_S for c in chosen):
            chosen.append(p)
    return sorted(chosen, key=lambda p: p["start"])


def detect_events(
    words: Sequence[Dict],
    clip_duration: float,
    tone: str = "",
    energy: Optional[Sequence[float]] = None,
    energy_step_s: float = 0.25,
    scene_cuts: Optional[Sequence[float]] = None,
    popup_starts: Optional[Sequence[float]] = None,
    emotional_intensity: Optional[float] = None,
) -> List[Event]:
    """All the moments worth considering, in time order, with ids event_001, event_002..."""
    raw: List[Dict] = []
    sentences = split_sentences(words)

    first = sentences[0] if sentences else None
    hook_strength = 0.6 + (0.2 if first and (first["exclaim"] or first["question"] or first["caps"]) else 0.0)
    raw.append({"kind": "hook", "start": 0.0, "end": min(HOOK_SECONDS, clip_duration),
                "intensity": hook_strength, "source": "words", "reason": "opening of the clip"})

    stressed = []
    for s in sentences:
        if s["start"] < HOOK_SECONDS and s is first:
            continue                         # the hook already covers the opening sentence
        if s["exclaim"] or s["caps"]:
            strength = _clip01(0.55 + 0.2 * s["exclaim"] + 0.2 * s["caps"])
            raw.append({"kind": "statement", "start": s["start"], "end": s["end"], "intensity": strength,
                        "source": "words", "reason": "stressed sentence: " + s["text"][:60]})
            stressed.append(raw[-1])
        elif s["question"]:
            raw.append({"kind": "question", "start": s["start"], "end": s["end"], "intensity": 0.4,
                        "source": "words", "reason": "question: " + s["text"][:60]})

    peaks = audio_peaks(energy, energy_step_s) if energy is not None and len(energy) > 0 else []
    for p in peaks:
        if EDGE_MARGIN_S <= p["start"] <= clip_duration - EDGE_MARGIN_S:
            raw.append({"kind": "audio_peak", "start": p["start"], "end": p["end"], "intensity": p["strength"],
                        "source": "audio_energy", "reason": f"loudness {p['level']:.2f} well above the clip's level"})

    for t in scene_cuts or []:
        if EDGE_MARGIN_S <= t <= clip_duration - EDGE_MARGIN_S:
            raw.append({"kind": "scene_cut", "start": float(t), "end": float(t) + 0.12, "intensity": 0.6,
                        "source": "scene_detection", "reason": "visual cut in the source"})

    for t in popup_starts or []:
        if 0.0 <= t < clip_duration:
            raw.append({"kind": "popup", "start": float(t), "end": float(t) + 0.5, "intensity": 0.5,
                        "source": "popup", "reason": "popup card appears"})

    if emotional_intensity is not None and emotional_intensity >= 0.8:
        if peaks:
            at = max(peaks, key=lambda p: p["level"])
            start, end = at["start"], at["end"]
        elif stressed:
            at = max(stressed, key=lambda e: e["intensity"])
            start, end = at["start"], at["end"]
        else:
            start, end = clip_duration * 0.4, clip_duration * 0.4 + 0.8
        raw.append({"kind": "emotional_peak", "start": start, "end": end, "intensity": _clip01(emotional_intensity),
                    "source": "clip_metadata", "reason": f"emotional intensity {emotional_intensity:.2f}"})

    raw.sort(key=lambda e: (e["start"], -PRIORITY[e["kind"]]))
    events = []
    for n, item in enumerate(raw, start=1):
        events.append(Event(
            id=f"event_{n:03d}", kind=item["kind"], start=round(item["start"], 3), end=round(item["end"], 3),
            intensity=round(item["intensity"], 3), priority=PRIORITY[item["kind"]], source=item["source"],
            tone=tone, reason=item["reason"],
        ))
    return events
