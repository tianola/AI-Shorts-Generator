"""
Service for selecting the best clips from a video using AI.
OpusClip-grade virality scoring, curiosity-gap hooks, and layout recommendations.
Research-backed signals: hook strength, emotional intensity, audio dynamics, visual events, pacing, narrative completeness.
"""
import json
import os
import re
import string
from typing import Optional
from loguru import logger
from config import settings
from services.claude_client import SHORTS_SYSTEM as _SELECTION_SYSTEM
from services.clip_bounds import load_words, snap_to_sentences
from services.clip_judging import (
    AUTO, JUDGE_SCHEMA, apply_penalties, dimension_score, fit_to_topic, infer_mode, judge_instructions,
    mode_guidance, normalise_mode, parse_judgement, topic_boundaries,
)
from services.llm_router import call_llm_json, pick_model, salvage_incomplete_json
from services.metadata import derive_title_from_text
from services.title_style import fix_title
from services.story_analysis import analyze_story_segments, transcript_block
from services.tone_detector import is_transcript_somber, classify_content_tone

# Power words for engagement keyword detection (case-insensitive).
POWER_WORDS = [
    # Curiosity / Open loops
    "secret", "never told", "nobody knows", "hidden", "actually",
    "wait", "but wait", "here's the thing", "plot twist",
    # Urgency / Stakes
    "never again", "last time", "breaking", "urgent", "critical",
    # Controversy / Emotion
    "wrong", "lied", "shocked", "unbelievable", "insane", "crazy",
    "caught", "exposed", "truth", "proof",
    # Instructional (high retention)
    "how to", "the reason", "this is why", "what nobody", "you need to",
    # Numbers (specificity = trust)
    "$", "%", "million", "billion", "number one", "#1",
]

# Measured 2026-11-12: a clip in the selection reply costs ~285 output tokens
# (6869 chars / 7 clips at a 2000-token cap). A flat budget therefore truncates
# any ask above 7 clips; scale it with num_clips and keep a floor for small runs.
CLIP_OUTPUT_TOKENS = 350

# Automatic count (num_clips <= 0): the transcript is read in parts of about this many seconds,
# each part asked for a few candidates, so no single reply has to carry 60 clips.
AUTO_CHUNK_SECONDS = 900
AUTO_CANDIDATES_PER_CHUNK = 8
AUTO_JUDGE_BATCH = 20

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "title": {"type": "string"},
                    "hook": {"type": "string"},
                    "why": {"type": "string"},
                    "virality_score": {"type": "number"},
                    "story_type": {
                        "type": "string",
                        "enum": ["question_answer", "reveal", "challenge", "emotion", "lesson", "reaction"],
                    },
                    "layout": {"type": "string", "enum": ["single_speaker", "split_screen", "fit_blur"]},
                    "tone_category": {
                        "type": "string",
                        "enum": ["humorous_entertainment", "casual_podcast", "tech_educational",
                                 "business_strategy", "documentary_serious", "somber_tragic"],
                    },
                    "popup_eligible": {"type": "boolean"},
                    "popup_cues": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "start_offset": {"type": "number"},
                                "duration": {"type": "number"},
                                "position": {"type": "string", "enum": ["upper_third", "upper_right"]},
                            },
                            "required": ["query", "start_offset", "duration", "position"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["start", "end", "title", "hook", "why", "virality_score", "story_type",
                             "layout", "tone_category", "popup_eligible", "popup_cues"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["clips"],
    "additionalProperties": False,
}

# Where a clip came from. Only a window a model actually chose may publish on its
# own; the transcript-heuristic filler and the time grid are there so a job still
# renders something to review, not so it reaches the channel unattended.
MODEL_SELECTION_PATHS = ("claude", "ai")

# Adaptive thinking is billed inside max_tokens on Claude, so the reply budget the
# OmniRoute chain needs is not enough headroom there.
_CLAUDE_THINKING_HEADROOM = 16000

# The free reasoning members of the OmniRoute chain (nemotron-3-ultra) think inside
# max_tokens too: at 3500 they returned no JSON on job 111's prompt, at 12000 all
# ten windows. Models that do not think stop when their answer ends, so the floor
# costs them nothing.
_OMNIROUTE_MIN_TOKENS = 12000


async def _selection_model_json(prompt: str, schema: dict, max_tokens: int, *,
                                context: str = "", effort: str = "high",
                                visual: Optional[list] = None) -> tuple:
    """Ask Claude when a key is configured, else (or on failure) the OmniRoute chain.

    `context` (the transcript) is sent to Claude as a separate cached block so the
    story pass and the candidate pass share it; the OmniRoute chain has no cache
    and gets it inline ahead of the task. `effort` is Claude's thinking depth:
    high for picking candidates, medium for the judge, which reads far less.
    `visual` (keyframe image blocks) goes to Claude only; the free chain is text.

    Returns the reply text and the path it came from ("claude" or "ai").
    """
    from services.claude_client import call_claude_json, claude_available

    if claude_available():
        try:
            return await call_claude_json(
                prompt=prompt,
                system=_SELECTION_SYSTEM,
                schema=schema,
                max_tokens=max_tokens + _CLAUDE_THINKING_HEADROOM,
                effort=effort,
                context=context,
                visual=visual,
            ), "claude"
        except Exception as exc:
            logger.warning(f"Claude clip-selection call failed ({type(exc).__name__}: {exc}); trying OmniRoute")

    text = await call_llm_json(
        prompt=f"{context}\n{prompt}" if context else prompt,
        system=_SELECTION_SYSTEM,
        model=pick_model(),
        temperature=0.35,
        max_tokens=max(max_tokens, _OMNIROUTE_MIN_TOKENS),
    )
    return text, "ai"


def _dedupe_overlapping_clips(clips: list, overlap_ratio: float = 0.5) -> list:
    """Keep the stronger window when two picks substantially cover the same moment."""
    kept = []
    for clip in sorted(clips, key=lambda item: item.get("virality_score", 0.0), reverse=True):
        start, end = float(clip["start"]), float(clip["end"])
        duplicate = False
        for prior in kept:
            overlap = max(0.0, min(end, float(prior["end"])) - max(start, float(prior["start"])))
            shorter = min(end - start, float(prior["end"]) - float(prior["start"]))
            if shorter > 0 and overlap / shorter >= overlap_ratio:
                duplicate = True
                break
        if not duplicate:
            kept.append(clip)
    return kept


def _candidates_wanted(num_clips: int) -> int:
    """How many candidates to ask for: about twice the clips that will ship.

    Asking for exactly num_clips meant every answer was published and the scoring
    only reordered them. The surplus is what gives the second pass something to
    reject. Capped so a 20-clip job does not ask for a reply nobody can finish.
    """
    return min(30, max(num_clips + 4, num_clips * 2))


def _fit_window(start: float, end: float, segments: list, tolerance: float = 1.0):
    """Place a model-proposed window on transcript boundaries, inside the length rule.

    Returns (start, end), or None when the window cannot be made valid. The model's
    timestamps are approximate, so an edge within `tolerance` of a segment edge moves
    onto it - that is where a sentence starts or ends, and a cut anywhere else lands
    mid-word. A window that is too long is ended at the last segment end that still
    fits rather than at start+MAX, because the tail is where the payoff is and a
    blind cut removes it mid-sentence. One that is too short is extended to the next
    segment end when that fits, and dropped otherwise.
    """
    if not segments:
        return None
    total = float(segments[-1]["end"])
    if start < 0 or start >= total or end <= start:
        return None
    end = min(end, total)

    min_len = settings.MIN_CLIP_SECONDS * 0.8
    max_len = float(settings.MAX_CLIP_SECONDS)
    starts = [float(seg["start"]) for seg in segments]
    ends = [float(seg["end"]) for seg in segments]

    nearest_start = min(starts, key=lambda value: abs(value - start))
    if abs(nearest_start - start) <= tolerance:
        start = nearest_start
    nearest_end = min(ends, key=lambda value: abs(value - end))
    if abs(nearest_end - end) <= tolerance and nearest_end > start:
        end = nearest_end

    if end - start > max_len:
        fitting = [value for value in ends if start + min_len <= value <= start + max_len]
        end = max(fitting) if fitting else start + max_len
    elif end - start < min_len:
        later = [value for value in ends if value > end and start + min_len <= value <= start + max_len]
        if not later:
            return None
        end = min(later)

    return round(start, 2), round(end, 2)


def _place_window(start: float, end: float, segments: list, words: list):
    """Sentence edges from word timing when there is any, else segment edges."""
    if words:
        window = snap_to_sentences(start, end, words,
                                   settings.MIN_CLIP_SECONDS * 0.8,
                                   float(settings.MAX_CLIP_SECONDS))
        if window is not None:
            return window
    return _fit_window(start, end, segments)


def _snap_filler(clips: list, segments: list, words: list) -> list:
    """Heuristic and grid windows get the same sentence edges as model picks."""
    for clip in clips:
        window = _place_window(float(clip["start"]), float(clip["end"]), segments, words)
        if window is not None:
            clip["start"], clip["end"] = window
    return clips


def _segments_in_window(segments: list, start: float, end: float) -> list:
    """Segments that really overlap the window.

    A segment that only touches the window's edge is not in it. The old `<`/`>`
    test let the sentence before the clip in, so a clip that began on a segment
    boundary - the normal case - had its hook scored on the previous sentence.
    """
    return [
        seg for seg in segments
        if min(float(seg["end"]), end) - max(float(seg["start"]), start) > 0.05
    ]


def _spoken_language_hint(segments: list) -> str:
    """"Greek" when the transcript is mostly Greek script, else "" (let the model match)."""
    sample = " ".join(str(seg.get("text") or "") for seg in segments[:300])
    greek = sum(1 for ch in sample if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿")
    latin = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    return "Greek" if greek > latin else ""


def _candidate_prompt(segments: list, story_plan: dict, wanted: int, num_clips: int,
                      video_title: str, temporal_events, mode: str = "viral",
                      topic_breaks=None, channel_examples: str = "") -> str:
    key_moments = ""
    for moment in (story_plan.get("key_moments") or [])[:8]:
        if isinstance(moment, dict) and moment.get("start") is not None:
            key_moments += f"\n- around {moment.get('start')}s: {moment.get('reason', '')}"
    key_moments_block = (
        f"\nA first read of the video flagged these moments (hints, not a list to copy):{key_moments}\n"
        if key_moments else ""
    )

    visual_events_block = ""
    if temporal_events:
        from services.temporal_localization import format_events_for_prompt
        visual_events = format_events_for_prompt(temporal_events)
        if visual_events:
            visual_events_block = (
                f"\n{visual_events}\n\n"
                "Use the visual events to prefer moments where something also HAPPENS on screen "
                "(a person enters, an object appears, the scene changes), not only where the talk is good.\n"
            )

    examples_block = f"\n{channel_examples}\n" if channel_examples else ""

    language = _spoken_language_hint(segments)
    language_note = f" ({language})" if language else ""
    title_line = f'\nSource video title: "{video_title}"' if video_title else ""
    from services.title_style import brief as _title_brief
    mode_block = f"\n{mode_guidance(mode)}\n"
    if topic_breaks:
        stamps = ", ".join(f"{t:.0f}s" for t in topic_breaks[:80])
        mode_block += (
            f"\nThe subject of the talk changes at about: {stamps}. Prefer a moment that "
            "stays inside one subject, from where it opens to where it closes, rather than "
            "one that straddles a change.\n"
        )

    return f"""
The transcript above is the whole video, with timestamps.
You are choosing moments from that video to publish as standalone vertical shorts
(YouTube Shorts / TikTok). A viewer decides in the first 1-3 seconds whether to swipe
away, and most of them never saw the full video, so every pick has to work for a
stranger with zero context.
{title_line}
What the video is about: {story_plan.get('story_summary', '')}
{mode_block}{key_moments_block}{examples_block}
Find the {wanted} strongest candidate moments. That is more than will be published on
purpose: a second pass ranks them and keeps the best {num_clips}, so include a real
range rather than padding with near-duplicates. Candidates must not overlap.

What makes a moment worth picking:
- It opens on its strongest line. The first sentence spoken already creates tension or
  curiosity: a bold claim, a surprising number, a confession, a question the viewer
  wants answered, a conflict already in motion. Start the clip exactly on that
  sentence - not on the setup before it (greetings, "so", "anyway", the interviewer
  winding up to the question).
- It pays off. The clip ends right after the answer, punchline, reveal or reaction
  lands. A complete thought, never cut mid-sentence, never ending on the setup.
- It stands alone. No "as I said before", no unexplained "he" or "that thing".
- Something is at stake: emotion, disagreement, money, risk, a strong opinion, a funny
  or shocking turn. A calm explanation with no turn in it is a weak pick.

Length: between {settings.MIN_CLIP_SECONDS} and {settings.MAX_CLIP_SECONDS} seconds, and
aim for 35-55. A clip must cover the WHOLE topic the speaker is on: the setup, the
point, and the follow-through or conclusion of that same thread. Do not stop at the
first sentence that sounds like an answer if the speaker keeps developing the same
point in the next lines; keep going until they move to a different subject. A 20-25
second clip that leaves the topic unfinished is worse than a 50 second one that closes
it. Never shorten a moment by cutting the last sentence: "end" is the moment the
speaker finishes the sentence where the payoff lands, not where a line of the
transcript happens to stop (a line can break mid-sentence).
Use the transcript's own timestamps.

For each candidate:
- title: the headline burned onto the video and used as the published title. Its job is
  to make someone need to watch, not to summarise.
{_title_brief()}
- hook: the first sentence spoken in the clip, copied word for word from the transcript.
- why: one sentence on why a stranger keeps watching to the end.
- virality_score: 0.0 to 1.0. Be honest and spread the scores out; most moments in any
  video are not a 0.9.
- story_type, layout ('split_screen' for a two-person conversation, 'single_speaker'
  for one person talking, 'fit_blur' when the whole frame matters), tone_category.
- popup_eligible: false for serious, somber or tragic content. popup_cues: zero to two
  image queries of 1-3 keywords for something concrete that is mentioned, with
  start_offset in seconds from the clip start (at least 2.0) and duration 2-3 seconds.

Language: write title and why in the language spoken in the transcript{language_note}.
Never switch to another language or script.
{visual_events_block}
Return ONLY valid JSON in this shape:
{{
  "clips": [
    {{
      "start": float, "end": float,
      "title": "string", "hook": "string", "why": "string",
      "virality_score": float,
      "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
      "layout": "single_speaker|split_screen|fit_blur",
      "tone_category": "humorous_entertainment|casual_podcast|tech_educational|business_strategy|documentary_serious|somber_tragic",
      "popup_eligible": boolean,
      "popup_cues": [{{"query": "string", "start_offset": float, "duration": float, "position": "upper_third|upper_right"}}]
    }}
  ]
}}
"""


async def _judge_candidates(clips: list, segments: list, num_clips: int, mode: str = "viral",
                            video_path: Optional[str] = None, channel_examples: str = "") -> bool:
    """Second pass: an editor's verdict on what the viewer will actually hear.

    The first pass rates its own picks while reading the whole transcript. Here the
    model sees only the words inside each final window, opening line first - the
    same information a stranger in the feed gets - and scores eleven dimensions plus
    a keep/reject decision (rubric in services.clip_judging). Writes judge_dims,
    judge_decision, judge_score (0-1) and judge_reason onto the clips it rated.
    Returns False, leaving the clips as they were, when no row is usable: the first
    pass's own scores still rank them.
    """
    blocks = []
    for index, clip in enumerate(clips):
        inside = _segments_in_window(segments, float(clip["start"]), float(clip["end"]))
        opening = str(inside[0].get("text") or "").strip() if inside else ""
        rest = " ".join(str(seg.get("text") or "").strip() for seg in inside[1:])
        blocks.append(
            f"Candidate c{index} ({float(clip['end']) - float(clip['start']):.0f}s) - "
            f"title: {clip.get('title', '')}\n"
            f"OPENING LINE: {opening}\n"
            f"THEN: {rest}"
        )
    candidates_text = "\n\n".join(blocks)

    prompt = f"""
Below are {len(clips)} candidate shorts cut from one long video. For each you see exactly
what the viewer hears, from the first word to the last. Judge each as a stranger scrolling
a feed who has never seen the video. A weak opening caps the overall impression, because
viewers leave before the rest matters.
{judge_instructions(num_clips, mode)}
{channel_examples + chr(10) if channel_examples else ""}
{candidates_text}

Return ONLY valid JSON:
{{"ranked": [{{"candidate_id": "c0", "decision": "keep", "hook": 0-10, "curiosity": 0-10, "emotion": 0-10, "surprise": 0-10, "story": 0-10, "payoff": 0-10, "pacing": 0-10, "standalone": 0-10, "rewatch": 0-10, "conflict": 0-10, "coherence": 0-10, "reason": "under 18 words"}}]}}
"""

    try:
        visual = None
        from services.claude_client import claude_available
        if claude_available():
            from services.judge_frames import judge_frame_blocks
            visual = await judge_frame_blocks(video_path, clips, int(settings.JUDGE_FRAMES_PER_CLIP)) or None
        text, _ = await _selection_model_json(
            prompt, JUDGE_SCHEMA, max_tokens=max(3000, len(clips) * 260), effort="medium",
            visual=visual,
        )
        data = salvage_incomplete_json(text.replace("```json", "").replace("```", "").strip())
        ranked = data.get("ranked")
        if not isinstance(ranked, list):
            return False
        rated = 0
        for row in ranked:
            judgement = parse_judgement(row)
            if judgement is None:
                continue
            try:
                index = int(str(row.get("candidate_id", "")).strip().lstrip("cC"))
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(clips) or "judge_dims" in clips[index]:
                continue
            clips[index]["judge_dims"] = judgement["dims"]
            clips[index]["judge_decision"] = judgement["decision"]
            clips[index]["judge_score"] = round(dimension_score(judgement["dims"]), 4)
            clips[index]["judge_reason"] = judgement["reason"]
            rated += 1
        return rated > 0
    except Exception as exc:
        logger.warning(f"Clip ranking pass failed ({type(exc).__name__}: {exc}); keeping first-pass scores")
        return False


def _selection_score(clip: dict) -> float:
    """Rank a model-chosen clip: mostly the model's judgement, plus what was measured.

    The English keyword, hook-pattern and emotion-word scores are left out of the
    ranking on purpose. They carried 45% of the old composite and read nothing in a
    Greek transcript, so on Greek video the order was noise. What remains beside the
    model is language-independent: loudness variation, speech coverage, on-screen
    events.
    """
    model = float(clip.get("llm_score_raw", 0.75))
    judge = clip.get("judge_score")
    if judge is not None:
        model = 0.3 * model + 0.7 * float(judge)
    score = (
        model * 0.75 +
        float(clip.get("audio_dynamics", 0.5)) * 0.10 +
        float(clip.get("pacing_score", 0.5)) * 0.10 +
        float(clip.get("visual_event_density", 0.5)) * 0.05
    )
    if clip.get("judge_dims"):
        score = apply_penalties(score, clip["judge_dims"], clip.get("judge_decision", "keep"),
                                float(clip["end"]) - float(clip["start"]))
    return round(min(1.0, score), 4)


# High-arousal emotion words (drive sharing per Journal of Consumer Psychology 2024)
HIGH_AROUSAL_EMOTIONS = [
    # Awe/Wonder
    "amazing", "incredible", "unbelievable", "mind-blowing", "astonishing",
    "stunning", "breathtaking", "wow", "omg", "holy", "insane", "crazy",
    # Humor/Amusement
    "hilarious", "funny", "lol", "lmao", "laughing", "cracked up",
    # Surprise/Shock
    "shocked", "surprised", "unexpected", "didn't expect", "plot twist",
    "caught", "exposed", "revealed",
    # Anger/Outrage (drives shares but use carefully)
    "outraged", "furious", "angry", "disgusted", "unfair", "wrong",
    # Inspiration/Joy
    "inspiring", "beautiful", "love", "amazing", "wonderful", "fantastic",
]

# Hook patterns (first 3 seconds) that predict high retention
HOOK_PATTERNS = [
    # Curiosity gap
    r"\b(secret|never|nobody|hidden|actually|wait)\b",
    # Concrete claim with numbers
    r"\b\d+\s*(percent|million|billion|times|ways)\b",
    # Question that demands answer
    r"^(what|why|how|when|where)\b.*\?",
    # Contrarian statement
    r"\b(everyone|nobody|always|never)\b.*\b(wrong|right|lies|truth)\b",
    # Start mid-action (no "so" or "well" opening)
    r"^(?!so\b|well\b|okay\b|alright\b)",
]


def _keyword_score(text: str) -> float:
    """Score text 0-1 based on power-word matches."""
    if not text:
        return 0.0
    lower = text.lower()
    matches = sum(1 for word in POWER_WORDS if word in lower)
    return min(1.0, matches * 0.25)


def _hook_strength_score(text: str) -> float:
    """Score 0-1 based on hook quality in first 3 seconds of transcript.

    Research (MLLM-VAU framework, arXiv 2602.22299): first 3 seconds are decisive.
    Strong hooks: curiosity gap, concrete claim, question, contrarian, mid-action start.
    Weak hooks: "so today", "well", generic openings.
    """
    if not text:
        return 0.0

    # Take first ~50 chars (roughly 3 seconds of speech)
    hook_text = text[:50].lower().strip()
    if not hook_text:
        return 0.0

    score = 0.0

    # Check for hook patterns
    for pattern in HOOK_PATTERNS:
        if re.search(pattern, hook_text, re.IGNORECASE):
            score += 0.2

    # Penalize weak openings
    weak_openings = ("so ", "well ", "okay ", "alright ", "um ", "like ")
    if any(hook_text.startswith(w) for w in weak_openings):
        score -= 0.3

    # Bonus for questions (curiosity gap)
    if hook_text.endswith("?") or "?" in hook_text[:30]:
        score += 0.15

    # Bonus for concrete numbers (specificity = trust)
    if re.search(r"\d+", hook_text):
        score += 0.1

    return round(max(0.0, min(1.0, score)), 3)


def _emotional_intensity_score(text: str) -> float:
    """Score 0-1 based on high-arousal emotion density.

    Research (Journal of Consumer Psychology 2024): 72% of shares come from emotional reactions.
    High-arousal emotions (awe, humor, surprise, anger) drive 30% more shares than neutral.
    """
    if not text:
        return 0.0

    lower = text.lower()
    # Strip punctuation for word matching
    cleaned = lower.translate(str.maketrans('', '', string.punctuation))
    words = cleaned.split()
    if not words:
        return 0.0

    emotion_matches = 0
    for word in words:
        if any(emotion in word for emotion in HIGH_AROUSAL_EMOTIONS):
            emotion_matches += 1

    # Also check for multi-word phrases in the full text
    for emotion in HIGH_AROUSAL_EMOTIONS:
        if " " in emotion and emotion in lower:
            emotion_matches += 1

    # Normalize: 1+ emotion per 10 words = high intensity
    density = emotion_matches / max(1, len(words) / 10.0)
    return round(min(1.0, density * 0.5), 3)


def _audio_dynamics_score(energies: list) -> float:
    """Score 0-1 based on audio energy variation (dB spikes), not just average.

    Research: audio-only pseudo-highlights beat visual-only (Islam et al., WACV 2025).
    Key signal: variance in energy, not mean. A spike indicates an exciting moment.
    """
    if not energies or len(energies) < 2:
        return 0.5

    # Calculate coefficient of variation (std/mean) — higher = more dynamic
    mean_e = sum(energies) / len(energies)
    if mean_e <= 0:
        return 0.0

    variance = sum((e - mean_e) ** 2 for e in energies) / len(energies)
    std_e = variance ** 0.5
    cv = std_e / mean_e  # coefficient of variation

    # CV of 0.3+ indicates good dynamics; 0.5+ is excellent
    score = min(1.0, cv * 2.0)
    return round(score, 3)


def _visual_event_density_score(event_count: int, duration: float) -> float:
    """Score 0-1 based on visual events per second.

    Research: scene changes and object appearances predict engagement.
    Sweet spot: 1 event per 5-10 seconds (0.1-0.2 events/sec).
    """
    if duration <= 0:
        return 0.0

    density = event_count / duration
    # Sweet spot: 0.1-0.2 events/sec; too dense (>0.5) is chaotic, too sparse (<0.05) is boring
    if density < 0.05:
        return round(density * 10.0, 3)  # linear ramp up
    elif density <= 0.2:
        return 0.5 + (density - 0.05) * 3.33  # peak at 0.2
    else:
        return round(max(0.3, 1.0 - (density - 0.2) * 2.0), 3)  # decay after peak


def _pacing_score(segments: list, clip_start: float, clip_end: float) -> float:
    """Score 0-1 based on coverage — penalizes dead air/gaps.

    Research: dead air kills retention. Tight pacing = high completion rate.
    Measures what fraction of the clip has speech vs. silence.
    """
    clip_duration = clip_end - clip_start
    if clip_duration <= 0:
        return 0.0

    # Calculate total time covered by segments
    covered = 0.0
    for seg in segments:
        seg_start = max(seg["start"], clip_start)
        seg_end = min(seg["end"], clip_end)
        if seg_end > seg_start:
            covered += seg_end - seg_start

    coverage = covered / clip_duration
    # 90%+ coverage = excellent, 70-90% = good, <70% = has dead air
    return round(min(1.0, coverage * 1.1), 3)  # scale so 90% coverage = ~1.0


def _narrative_completeness_score(text: str) -> float:
    """Score 0-1 based on whether the clip stands alone without context.

    Research: viral clips are self-contained. Incomplete thoughts ("and then...") kill shares.
    Checks: has subject, has conclusion (ends with punctuation), doesn't start with conjunction.
    """
    if not text:
        return 0.0

    text = text.strip()
    words = text.split()
    if len(words) < 3:
        return 0.2  # too short to be complete

    score = 0.5

    # Penalize starting with conjunction (mid-thought)
    bad_starts = ("and ", "but ", "or ", "so ", "because ", "that ", "which ", "then ")
    if text.lower().startswith(bad_starts):
        score -= 0.3

    # Reward ending with proper punctuation (conclusion)
    if text[-1] in ".!?":
        score += 0.2

    # Reward having a subject (pronoun or noun near start)
    subject_markers = ("i ", "we ", "he ", "she ", "they ", "it ", "the ", "this ", "that ")
    if any(text.lower().startswith(m) or f" {m}" in text.lower()[:30] for m in subject_markers):
        score += 0.15

    # Reward question format (self-contained curiosity)
    if "?" in text:
        score += 0.15

    return round(max(0.0, min(1.0, score)), 3)


def _structural_completeness(segments_in_clip: list) -> float:
    """Score 0-1 based on whether the clip starts and ends cleanly."""
    if not segments_in_clip:
        return 0.5
    first_text = (segments_in_clip[0].get('text') or '').strip()
    last_text = (segments_in_clip[-1].get('text') or '').strip()
    score = 0.5
    bad_starts = ('and ', 'but ', 'or ', 'so ', 'because ', 'that ', 'which ')
    if not first_text.lower().startswith(bad_starts):
        score += 0.25
    if last_text.endswith(('.', '!', '?', '...')):
        score += 0.25
    return score


def _semantic_density_score(seg: dict) -> float:
    """Score 0-1 based on words-per-second. Sweet spot: 2.5-4.5 wps."""
    duration = seg['end'] - seg['start']
    if duration <= 0:
        return 0.5
    words = len((seg.get('text') or '').split())
    wps = words / duration
    if wps < 1.0:
        return round(wps / 2.0, 3)
    elif wps <= 4.5:
        return round(min(1.0, wps / 3.5), 3)
    else:
        return round(max(0.5, 1.0 - (wps - 4.5) * 0.1), 3)


def _normalise_llm_score(value) -> float:
    """Map the model's own virality self-rating onto 0-1.

    The prompt asks for a float and names no range, so the model answers on the
    scale it likes - measured 0-10 on job 110's source (9.4, 8.9, 9.6). At the v2
    composite's 25% weight, a raw 9.6 is 2.4 on its own, so min(1.0, composite)
    pinned every AI clip to 1.0 and the ranking collapsed to emission order.
    """
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.75
    if score > 1.0:
        score /= 10.0
    return round(max(0.0, min(1.0, score)), 4)


def _composite_score_v2(clip: dict) -> float:
    """Research-backed weighted virality score (v2).

    Based on OpusClip, Munch, and academic research (WACV 2025, CVPR 2026, arXiv 2602.22299).

    Weights:
    - LLM raw score: 25% (down from 40% — LLM alone is not enough)
    - Hook strength: 20% (first 3 seconds are decisive)
    - Emotional intensity: 15% (high-arousal emotions drive 72% of shares)
    - Audio dynamics: 10% (dB spikes, not just average)
    - Keyword score: 10% (power words still matter)
    - Visual event density: 8% (something happening on screen)
    - Pacing: 7% (no dead air)
    - Narrative completeness: 5% (stands alone)
    """
    llm_score           = float(clip.get("virality_score", 0.75))
    hook_strength       = float(clip.get("hook_strength", 0.5))
    emotional_intensity = float(clip.get("emotional_intensity", 0.5))
    audio_dynamics      = float(clip.get("audio_dynamics", 0.5))
    keyword_score       = float(clip.get("keyword_score", 0.5))
    visual_density      = float(clip.get("visual_event_density", 0.5))
    pacing              = float(clip.get("pacing_score", 0.5))
    narrative           = float(clip.get("narrative_completeness", 0.5))

    composite = (
        llm_score           * 0.25 +
        hook_strength       * 0.20 +
        emotional_intensity * 0.15 +
        audio_dynamics      * 0.10 +
        keyword_score       * 0.10 +
        visual_density      * 0.08 +
        pacing              * 0.07 +
        narrative           * 0.05
    )
    return round(min(1.0, composite), 4)


def _time_chunks(segments: list, seconds: int = AUTO_CHUNK_SECONDS) -> list:
    """The transcript in consecutive parts of about `seconds` each, in order. Empty parts are left out."""
    parts, current, start = [], [], None
    for seg in segments:
        if start is None:
            start = float(seg.get("start", 0.0))
        current.append(seg)
        if float(seg.get("end", start)) - start >= seconds:
            parts.append(current)
            current, start = [], None
    if current:
        parts.append(current)
    return [part for part in parts if sum(len(str(s.get("text") or "").split()) for s in part) >= 30]


async def _judge_in_batches(clips: list, segments: list, ceiling: int, mode: str,
                            video_path: Optional[str] = None, channel_examples: str = "") -> bool:
    """Judge a long list in batches, so one judge reply never has to carry 60 verdicts."""
    judged = False
    for start in range(0, len(clips), AUTO_JUDGE_BATCH):
        batch = clips[start:start + AUTO_JUDGE_BATCH]
        if await _judge_candidates(batch, segments, ceiling, mode, video_path=video_path,
                                   channel_examples=channel_examples):
            judged = True
    return judged


async def select_clips(segments_file: str, num_clips: int = 3, video_title: str = "", temporal_events: list = None,
                       mode: str = "viral", video_path: Optional[str] = None,
                       channel_examples: str = "") -> list:
    """Select clips (see _select_clips). mode="auto" reads the video and picks the mode;
    every clip carries the mode that was used as "edit_mode"."""
    mode = normalise_mode(mode)
    if mode == AUTO:
        try:
            with open(segments_file, "r", encoding="utf-8") as fh:
                mode = await infer_mode(video_title, json.load(fh))
        except (OSError, ValueError):
            mode = "viral"
        logger.info(f"Editing mode picked for this video: {mode}")
    clips = await _select_clips(segments_file, num_clips, video_title, temporal_events, mode,
                                video_path=video_path, channel_examples=channel_examples)
    for clip in clips:
        clip["edit_mode"] = mode
    return clips


async def _select_clips(segments_file: str, num_clips: int = 3, video_title: str = "", temporal_events: list = None,
                        mode: str = "viral", video_path: Optional[str] = None,
                        channel_examples: str = "") -> list:
    """
    Select the best clips from a video using AI, in two passes.

    Pass 1 asks for about twice as many candidate moments as will ship. Each is put
    on transcript boundaries and inside the length rule by code. Pass 2 shows the
    model only the words inside each final window and has it score them the way a
    stranger in the feed would. The best non-overlapping num_clips are returned.

    Args:
        segments_file: Path to segments.json
        num_clips: Number of clips to select
        video_title: Optional source video title for contextual hook framing
        temporal_events: Optional visual event timeline from temporal_localization
        channel_examples: Optional text from services.channel_examples, the channel's
            most and least watched past clips, shown to both passes

    Returns:
        List of selected clips; each carries "selection_path" naming where it came
        from ("claude"/"ai" for a model pick, "heuristic" or "grid" otherwise).
    """
    with open(segments_file, "r", encoding="utf-8") as f:
        segments = json.load(f)
    words = load_words(os.path.join(os.path.dirname(segments_file), "words.json"))

    # num_clips <= 0 means automatic: the selector keeps every moment its judge passes, up to
    # MAX_AUTO_CLIPS. num_clips is then only the fallback count if the model gives nothing usable.
    auto = num_clips <= 0
    ceiling = int(settings.MAX_AUTO_CLIPS) if auto else num_clips
    if auto:
        num_clips = max(1, int(settings.NUM_CLIPS))

    story_plan = await analyze_story_segments(segments, video_title=video_title)
    topic_breaks = topic_boundaries(segments)

    if auto:
        requests = [(part, AUTO_CANDIDATES_PER_CHUNK) for part in _time_chunks(segments)]
    else:
        requests = [(segments, _candidates_wanted(num_clips))]
    source = "ai"

    try:
        clips = []
        for part, wanted in requests:
            prompt = _candidate_prompt(part, story_plan, wanted, wanted if auto else num_clips,
                                       video_title, temporal_events, mode=mode, topic_breaks=topic_breaks,
                                       channel_examples=channel_examples)
            try:
                response_text, source = await _selection_model_json(
                    prompt, _CANDIDATE_SCHEMA, max_tokens=max(2000, wanted * CLIP_OUTPUT_TOKENS),
                    context=transcript_block(part),
                )
                response_text = response_text.replace("```json", "").replace("```", "").strip()
                data = salvage_incomplete_json(response_text)
                clips.extend(data.get("clips", []))
            except Exception as exc:
                if not auto:
                    raise
                # One part of a long video failing must not throw away the other parts.
                logger.warning(f"Auto selection: a part of the video gave no candidates ({type(exc).__name__}: {exc})")

        valid_clips = []
        for clip in clips:
            if not isinstance(clip, dict):
                continue
            try:
                raw_start, raw_end = float(clip.get("start")), float(clip.get("end"))
            except (TypeError, ValueError):
                continue
            # The LLM is not trusted to respect the range or to land on a sentence
            # edge; the code places the window.
            # Finish the subject the clip is on before putting it on sentence edges.
            raw_start, raw_end = fit_to_topic(raw_start, raw_end, topic_breaks,
                                              float(settings.MAX_CLIP_SECONDS))
            window = _place_window(raw_start, raw_end, segments, words)
            if window is None:
                continue
            clip["start"], clip["end"] = window
            clip["selection_path"] = source
            # The title is checked in code: hype and extra emoji are removed, the length is cut,
            # and a title that does not match what the clip says is replaced by the clip's own words.
            window_text = " ".join(str(seg.get("text") or "") for seg in _segments_in_window(
                segments, float(clip["start"]), float(clip["end"])))
            clip["title"], title_fixed = fix_title(clip.get("title"), window_text)
            if title_fixed:
                clip["title_fixed"] = True

            clip["virality_score"] = _normalise_llm_score(clip.get("virality_score"))

            clip.setdefault("story_type", story_plan.get("story_type", "reaction"))
            clip.setdefault("layout", "single_speaker")
            clip.setdefault("caption_style", "mrbeast")
            clip.setdefault("cta", story_plan.get("cta", "Follow for more."))
            clip.setdefault("hook_score", clip.get("virality_score", 0.88))
            clip.setdefault("coherence_score", clip.get("virality_score", 0.85))
            clip.setdefault("connection_score", clip.get("virality_score", 0.84))
            clip.setdefault("trend_score", clip.get("virality_score", 0.82))

            # Compute all signals from the transcript segments inside the window
            clip_start = float(clip["start"])
            clip_end = float(clip["end"])
            segments_in_clip = _segments_in_window(segments, clip_start, clip_end)
            clip_text = " ".join(seg.get('text', '') for seg in segments_in_clip)
            clip_segment = {
                'start': clip_start,
                'end': clip_end,
                'text': clip_text
            }

            # Informational signals: shown in the breakdown, not used to rank
            # (they are English-only, see _selection_score).
            clip["semantic_density"] = _semantic_density_score(clip_segment)
            clip["keyword_score"] = _keyword_score(clip_text)
            clip["structural_score"] = _structural_completeness(segments_in_clip)
            clip["hook_strength"] = _hook_strength_score(clip_text)
            clip["emotional_intensity"] = _emotional_intensity_score(clip_text)
            clip["narrative_completeness"] = _narrative_completeness_score(clip_text)

            # Audio: average energy and its variation, when the pre-pass enriched the segments
            clip.setdefault("audio_energy_score", 0.5)
            energies = [float(s["audio_energy"]) for s in segments_in_clip if "audio_energy" in s]
            if energies:
                clip["audio_energy_score"] = round(sum(energies) / len(energies), 4)
                clip["audio_dynamics"] = _audio_dynamics_score(energies)
            else:
                clip["audio_dynamics"] = 0.5

            # Visual event density: count temporal events in clip window
            visual_event_count = 0
            if temporal_events:
                from services.temporal_localization import events_for_clip_window
                visual_event_count = len(
                    events_for_clip_window(temporal_events, clip_start, clip_end)
                )
            clip["visual_event_density"] = _visual_event_density_score(
                visual_event_count, clip_end - clip_start
            )

            # Pacing: coverage of clip by speech segments (no dead air)
            clip["pacing_score"] = _pacing_score(segments_in_clip, clip_start, clip_end)

            # Keep the model's own rating so the UI can show a truthful breakdown.
            clip["llm_score_raw"] = float(clip.get("virality_score", 0.75))

            # Tone & Context Gating
            clip_txt = f"{clip.get('title', '')} {clip.get('hook', '')}"
            if is_transcript_somber(clip_txt):
                clip["popup_eligible"] = False
                clip["popup_cues"] = []
                clip["tone_category"] = "somber_tragic"
                clip["style_profile"] = "none"
            else:
                clip.setdefault("popup_eligible", True)
                clip.setdefault("tone_category", "casual_podcast")
                clip.setdefault("style_profile", "meme_bouncy")
                clip.setdefault("popup_cues", [])

            valid_clips.append(clip)

        candidate_count = len(valid_clips)
        if auto:
            judged = candidate_count > 1 and await _judge_in_batches(valid_clips, segments, ceiling, mode,
                                                                       video_path=video_path,
                                                                       channel_examples=channel_examples)
        else:
            judged = candidate_count > 1 and await _judge_candidates(valid_clips, segments, num_clips, mode,
                                                                        video_path=video_path,
                                                                        channel_examples=channel_examples)
        if judged:
            # A reject is final: a clip the editor refused is not brought back by its
            # first-pass score. Unrated candidates (no usable row) stay in.
            rejected = [c for c in valid_clips if c.get("judge_decision") == "reject"]
            valid_clips = [c for c in valid_clips if c.get("judge_decision") != "reject"]
            logger.info(f"Judge kept {len(valid_clips)} of {candidate_count} candidates, rejected {len(rejected)}")
            if not valid_clips:
                logger.warning("Every candidate was rejected by the judge: no Short for this video")
                return []
        for clip in valid_clips:
            clip["virality_score"] = _selection_score(clip)

        # Strongest first, and two picks of the same moment collapse into the better one.
        valid_clips = _dedupe_overlapping_clips(valid_clips)
        if auto and judged:
            # The judge's bar decides how many moments ship; the ceiling only caps it.
            logger.info(f"Automatic selection: {len(valid_clips)} clips passed the judge (ceiling {ceiling})")
            return valid_clips[:ceiling]
        if len(valid_clips) >= num_clips:
            logger.info(
                f"Clip selection path={source}: {num_clips} of {candidate_count} candidate windows "
                f"({'ranked by second pass' if judged else 'first-pass scores only'})"
            )
            return valid_clips[:num_clips]
        if judged:
            # Fewer good clips than asked for is the honest answer; do not pad with
            # transcript filler the judge never saw.
            return valid_clips

        # Fill with heuristic moments if LLM returned fewer clips than requested
        total_duration = segments[-1]["end"] if segments else settings.MIN_CLIP_SECONDS
        clip_length = min(settings.MAX_CLIP_SECONDS, max(settings.MIN_CLIP_SECONDS, total_duration / max(1, num_clips)))
        heuristic_clips = []

        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            strength = 0.5 + min(0.4, len(text.split()) / 15.0)
            if "?" in text or "!" in text:
                strength += 0.15

            start = max(0.0, seg["start"] - 0.5)
            end = min(total_duration, start + clip_length)
            title = derive_title_from_text(text)

            tone_info = classify_content_tone(text, story_type=story_plan.get("story_type", ""))

            # Compute heuristic signals (legacy)
            h_audio = float(seg.get("audio_energy", 0.5))
            h_kw = _keyword_score(text)
            h_density = _semantic_density_score({"start": start, "end": end, "text": text})
            h_struct = 0.75

            # NEW: Research-backed signals for heuristic clips too
            h_hook = _hook_strength_score(text)
            h_emotion = _emotional_intensity_score(text)
            h_dynamics = _audio_dynamics_score([h_audio])  # single segment, limited signal
            h_visual = 0.3  # heuristic clips don't have visual event data
            h_pacing = 0.7  # assume decent pacing for single segments
            h_narrative = _narrative_completeness_score(text)

            h_composite = _composite_score_v2({
                "virality_score": round(min(0.95, strength), 2),
                "hook_strength": h_hook,
                "emotional_intensity": h_emotion,
                "audio_dynamics": h_dynamics,
                "keyword_score": h_kw,
                "visual_event_density": h_visual,
                "pacing_score": h_pacing,
                "narrative_completeness": h_narrative,
            })

            heuristic_clips.append({
                "start": float(start),
                "end": float(end),
                "title": title[:45],
                "hook": " ".join(text.split()[:8])[:60],
                "virality_score": h_composite,
                "llm_score_raw": round(min(0.95, strength), 2),
                "audio_energy_score": h_audio,
                "keyword_score": h_kw,
                "semantic_density": h_density,
                "structural_score": h_struct,
                "hook_strength": h_hook,
                "emotional_intensity": h_emotion,
                "audio_dynamics": h_dynamics,
                "visual_event_density": h_visual,
                "pacing_score": h_pacing,
                "narrative_completeness": h_narrative,
                "story_type": story_plan.get("story_type", "reaction"),
                "layout": "single_speaker",
                "caption_style": "mrbeast",
                "cta": story_plan.get("cta", "Follow for more."),
                "tone_category": tone_info["tone_category"],
                "popup_eligible": tone_info["popup_eligible"],
                "style_profile": tone_info["style_profile"],
                "popup_cues": [],
                "selection_path": "heuristic",
            })

        # The AI windows are the answer; heuristics only fill the shortfall. Sorting
        # both lists together used to evict them: a heuristic candidate is generated
        # for every transcript segment, and its score is not comparable to the LLM's,
        # so 120 fillers outranked 7 real picks and left the top-10 slice with none.
        selected = list(valid_clips[:num_clips])
        taken = [(float(c["start"]), float(c["end"])) for c in selected]
        _snap_filler(heuristic_clips, segments, words)
        heuristic_clips.sort(key=lambda x: x.get("virality_score", 0.0), reverse=True)
        for clip in heuristic_clips:
            if len(selected) >= num_clips:
                break
            start, end = float(clip["start"]), float(clip["end"])
            if any(start < prev_end and prev_start < end for prev_start, prev_end in taken):
                continue
            selected.append(clip)
            taken.append((start, end))

        logger.info(
            f"Clip selection path={source}+heuristic: {len(valid_clips)} model windows kept, "
            f"{len(selected) - len(valid_clips)} filled from transcript heuristics"
        )
        return selected

    except Exception as e:
        logger.error(f"Clip selection failed: {e}")
        total_duration = segments[-1]["end"] if segments else settings.MIN_CLIP_SECONDS
        clip_length = min(settings.MAX_CLIP_SECONDS, max(settings.MIN_CLIP_SECONDS, total_duration / max(1, num_clips)))
        fallback_clips = []
        for i in range(num_clips):
            start = round(max(0.0, i * (total_duration / max(1, num_clips))))
            end = min(total_duration, start + clip_length)
            window_text = " ".join(
                (seg.get("text") or "").strip()
                for seg in segments
                if min(seg["end"], end) - max(seg["start"], start) >= 1.0
            ).strip()
            # A placeholder title used to be published verbatim ("VIRAL MOMENT 2"
            # on the channel). The spoken opening is the only honest description
            # left when the selector is gone, so take it from there - and emit no
            # clip at all for a window with nothing said in it.
            title = derive_title_from_text(window_text)
            if not title:
                continue
            word_count = len(window_text.split())

            # Compute new viral signals for fallback clips too
            f_hook = _hook_strength_score(window_text)
            f_emotion = _emotional_intensity_score(window_text)
            f_narrative = _narrative_completeness_score(window_text)
            f_pacing = 0.7  # assume decent pacing for fallback

            f_composite = _composite_score_v2({
                "virality_score": round(0.45 + min(0.25, word_count / 60.0), 2),
                "hook_strength": f_hook,
                "emotional_intensity": f_emotion,
                "audio_dynamics": 0.5,  # no audio data in fallback
                "keyword_score": _keyword_score(window_text),
                "visual_event_density": 0.3,  # no visual data in fallback
                "pacing_score": f_pacing,
                "narrative_completeness": f_narrative,
            })

            fallback_clips.append({
                "start": float(start),
                "end": float(end),
                "title": title,
                "hook": title,
                "virality_score": f_composite,
                "hook_strength": f_hook,
                "emotional_intensity": f_emotion,
                "audio_dynamics": 0.5,
                "keyword_score": _keyword_score(window_text),
                "visual_event_density": 0.3,
                "pacing_score": f_pacing,
                "narrative_completeness": f_narrative,
                "story_type": "reaction",
                "layout": "single_speaker",
                "caption_style": "mrbeast",
                "cta": "Follow for more.",
                "selection_path": "grid",
            })
        return _snap_filler(fallback_clips, segments, words)

