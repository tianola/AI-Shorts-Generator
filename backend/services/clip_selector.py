"""
Service for selecting the best clips from a video using AI.
OpusClip-grade virality scoring, curiosity-gap hooks, and layout recommendations.
Research-backed signals: hook strength, emotional intensity, audio dynamics, visual events, pacing, narrative completeness.
"""
import json
import re
import string
from loguru import logger
from config import settings
from services.llm_router import call_llm_json, salvage_incomplete_json
from services.metadata import derive_title_from_text
from services.story_analysis import analyze_story_segments
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


def _composite_score(clip: dict) -> float:
    """Legacy weighted multi-signal virality score (kept for backward compat).

    Weights: LLM 40% | Audio Energy 25% | Keyword 20% | Density 10% | Structural 5%
    """
    llm_score     = float(clip.get("virality_score", 0.75))
    audio_energy  = float(clip.get("audio_energy_score", 0.5))
    keyword_score = float(clip.get("keyword_score", 0.5))
    density_score = float(clip.get("semantic_density", 0.5))
    structural    = float(clip.get("structural_score", 0.5))

    composite = (
        llm_score     * 0.40 +
        audio_energy  * 0.25 +
        keyword_score * 0.20 +
        density_score * 0.10 +
        structural    * 0.05
    )
    return round(min(1.0, composite), 4)


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


async def select_clips(segments_file: str, num_clips: int = 3, video_title: str = "", temporal_events: list = None) -> list:
    """
    Select the best clips from a video using AI.

    Args:
        segments_file: Path to segments.json
        num_clips: Number of clips to select
        video_title: Optional source video title for contextual hook framing
        temporal_events: Optional visual event timeline from temporal_localization

    Returns:
        List of selected clips
    """
    with open(segments_file, "r", encoding="utf-8") as f:
        segments = json.load(f)

    # Create transcript for prompt
    transcript_text = ""
    for seg in segments:
        transcript_text += f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}\n"

    story_plan = await analyze_story_segments(segments, video_title=video_title)

    title_context = f"\n    - Source Video Title / Main Theme: \"{video_title}\"" if video_title else ""

    visual_events_block = ""
    if temporal_events:
        from services.temporal_localization import format_events_for_prompt
        visual_events_block = format_events_for_prompt(temporal_events)

    visual_events_section = f"""
    {visual_events_block}

    Use the visual events to identify moments where something interesting HAPPENS on screen
    (new objects appear, people enter, scene changes), not just where the transcript is engaging.
    Clips that combine compelling speech WITH visual events score higher.
""" if visual_events_block else ""

    prompt = f"""
    You are a world-class TikTok/Shorts viral video producer (OpusClip AI Director).
    Analyze the following transcript and extract the top {num_clips} highest-converting viral moments.

    Story Plan:{title_context}
    - Summary: {story_plan.get('story_summary', '')}
    - Overall Hook: {story_plan.get('hook', '')}
    - Story Type: {story_plan.get('story_type', 'reaction')}
    - Recommended Layout: {story_plan.get('recommended_layout', 'single_speaker')}
    - CTA: {story_plan.get('cta', 'Follow for more.')}

    Rules for Viral Title & Hook Selection:
    - TITLE MUST BE THE ULTIMATE HIGH-STAKES VIRAL HOOK (High-CTR Magnet):
      * NEVER use passive, bland, or low-stakes descriptions (e.g. NEVER write "comforting her stalker" or "discussing something").
      * ALWAYS frame the title around the HIGHEST STAKES, emotional tension, shocking revelation, confrontation, or danger (e.g. "SHE CAUGHT HER STALKER INSIDE", "HE BROKE INTO HER HOUSE", "TRAPPED WITH HER STALKER", "THE MOMENT SHE REALIZED", "CAUGHT RED-HANDED").
      * The title must trigger primal curiosity, suspense, or anxiety so the viewer MUST stop scrolling.
      * Keep titles punchy, 3-6 words, uppercase (e.g. "CAUGHT INSIDE HER HOUSE", "HE DID THE UNTHINKABLE").
    - First 3 seconds MUST have an irresistible HOOK or open loop (Curiosity Gap).
    - Favor moments with revelations, contrarian statements, emotional peaks, or rapid explanations.
    - Clip length must be between {settings.MIN_CLIP_SECONDS} and {settings.MAX_CLIP_SECONDS} seconds.
    - Suggest suitable layout: 'single_speaker' (solo person), 'split_screen' (conversation/podcast), or 'fit_blur'.
    - Suggest subtitle style preset: 'hormozi', 'capcut_aqua', 'mrbeast', 'cyber_neon', or 'clean_box'.
    - Tone and Visual Popups:
      * tone_category: 'humorous_entertainment', 'casual_podcast', 'tech_educational', 'business_strategy', 'documentary_serious', or 'somber_tragic'.
      * popup_eligible: false for serious/somber/tragic content, true for energetic/humorous/tech/business.
      * popup_cues: 1-2 high-impact image/meme queries (e.g. 'bitcoin logo', 'mind blown meme', 'robot ai'). start_offset between 2.0 and duration-2.5s.

    Transcript:
    {transcript_text}
    {visual_events_section}
    Return ONLY valid JSON matching this schema:
    {{
        "clips": [
            {{
                "start": float,
                "end": float,
                "title": "string",
                "hook": "string",
                "virality_score": float between 0.0 and 1.0,
                "hook_score": float,
                "coherence_score": float,
                "connection_score": float,
                "trend_score": float,
                "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
                "layout": "single_speaker|split_screen|fit_blur",
                "caption_style": "hormozi|capcut_aqua|mrbeast|cyber_neon|clean_box",
                "tone_category": "humorous_entertainment|casual_podcast|tech_educational|business_strategy|documentary_serious|somber_tragic",
                "popup_eligible": boolean,
                "style_profile": "meme_bouncy|stock_minimal|none",
                "popup_cues": [
                    {{
                        "query": "string (1-3 keywords)",
                        "start_offset": float,
                        "duration": float,
                        "position": "upper_third|upper_right"
                    }}
                ],
                "cta": "string"
            }}
        ]
    }}
    """

    try:
        response_text = await call_llm_json(
            prompt=prompt,
            system="You are an expert viral Shorts director. Extract top-performing clips.",
            model=settings.OLLAMA_MODEL if settings.AI_PROVIDER == "ollama" else settings.OMNIROUTE_MODEL,
            temperature=0.35,
            max_tokens=max(2000, num_clips * CLIP_OUTPUT_TOKENS),
        )

        response_text = response_text.replace("```json", "").replace("```", "").strip()
        data = salvage_incomplete_json(response_text)
        clips = data.get("clips", [])

        valid_clips = []
        for clip in clips:
            if clip.get("start") is None or clip.get("end") is None:
                continue
            if float(clip["start"]) < 0 or float(clip["end"]) > segments[-1]["end"]:
                continue
            if (float(clip["end"]) - float(clip["start"])) < (settings.MIN_CLIP_SECONDS * 0.8):
                continue
            # The LLM is not trusted to respect the range; the code enforces it.
            # A 45-second clip from a 10-minute source is what the old pipeline shipped;
            # the new one trims it to MAX_CLIP_SECONDS so completion rate stays high.
            if (float(clip["end"]) - float(clip["start"])) > settings.MAX_CLIP_SECONDS:
                clip["end"] = float(clip["start"]) + settings.MAX_CLIP_SECONDS

            clip["virality_score"] = _normalise_llm_score(clip.get("virality_score"))

            clip.setdefault("story_type", story_plan.get("story_type", "reaction"))
            clip.setdefault("layout", "single_speaker")
            clip.setdefault("caption_style", "mrbeast")
            clip.setdefault("cta", story_plan.get("cta", "Follow for more."))
            clip.setdefault("hook_score", clip.get("virality_score", 0.88))
            clip.setdefault("coherence_score", clip.get("virality_score", 0.85))
            clip.setdefault("connection_score", clip.get("virality_score", 0.84))
            clip.setdefault("trend_score", clip.get("virality_score", 0.82))

            # Compute all signals from overlapping transcript segments
            clip_start = float(clip["start"])
            clip_end = float(clip["end"])
            clip_text_parts = []
            segments_in_clip = []
            for seg in segments:
                if seg['end'] < clip_start or seg['start'] > clip_end:
                    continue
                clip_text_parts.append(seg.get('text', ''))
                segments_in_clip.append(seg)
            clip_text = " ".join(clip_text_parts)
            clip_segment = {
                'start': clip_start,
                'end': clip_end,
                'text': clip_text
            }

            # Legacy signals (kept for backward compat)
            clip["semantic_density"] = _semantic_density_score(clip_segment)
            clip["keyword_score"] = _keyword_score(clip_text)
            clip["structural_score"] = _structural_completeness(segments_in_clip)
            clip.setdefault("audio_energy_score", 0.5)

            # If the audio pre-pass enriched the segments, use the real energy average
            if segments_in_clip and any("audio_energy" in s for s in segments_in_clip):
                energies = [float(s["audio_energy"]) for s in segments_in_clip if "audio_energy" in s]
                if energies:
                    clip["audio_energy_score"] = round(sum(energies) / len(energies), 4)

            # NEW: Research-backed viral signals (v2)
            # Hook strength: first 3 seconds of the clip
            clip["hook_strength"] = _hook_strength_score(clip_text)

            # Emotional intensity: high-arousal emotion density
            clip["emotional_intensity"] = _emotional_intensity_score(clip_text)

            # Audio dynamics: energy variation (dB spikes), not just average
            if segments_in_clip and any("audio_energy" in s for s in segments_in_clip):
                energies = [float(s["audio_energy"]) for s in segments_in_clip if "audio_energy" in s]
                clip["audio_dynamics"] = _audio_dynamics_score(energies) if energies else 0.5
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

            # Narrative completeness: stands alone without context
            clip["narrative_completeness"] = _narrative_completeness_score(clip_text)

            # Composite Virality Score v2 — research-backed weights.
            # Keep the raw LLM signal so the UI can show a truthful breakdown.
            clip["llm_score_raw"] = float(clip.get("virality_score", 0.75))
            clip["virality_score"] = _composite_score_v2({**clip})

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

        valid_clips.sort(key=lambda x: x.get("virality_score", 0.0), reverse=True)
        if len(valid_clips) >= num_clips:
            logger.info(f"Clip selection path=ai: {num_clips} of {len(valid_clips)} scored AI windows")
            return valid_clips[:num_clips]

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
            })

        # The AI windows are the answer; heuristics only fill the shortfall. Sorting
        # both lists together used to evict them: a heuristic candidate is generated
        # for every transcript segment, and its score is not comparable to the LLM's,
        # so 120 fillers outranked 7 real picks and left the top-10 slice with none.
        selected = list(valid_clips[:num_clips])
        taken = [(float(c["start"]), float(c["end"])) for c in selected]
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
            f"Clip selection path=ai+heuristic: {len(valid_clips)} scored AI windows kept, "
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
            words = len(window_text.split())

            # Compute new viral signals for fallback clips too
            f_hook = _hook_strength_score(window_text)
            f_emotion = _emotional_intensity_score(window_text)
            f_narrative = _narrative_completeness_score(window_text)
            f_pacing = 0.7  # assume decent pacing for fallback

            f_composite = _composite_score_v2({
                "virality_score": round(0.45 + min(0.25, words / 60.0), 2),
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
            })
        return fallback_clips


# Backward-compatibility alias — old callers used select_best_clips
select_best_clips = select_clips
