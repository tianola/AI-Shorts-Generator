"""
Service for selecting the best clips from a video using AI.
OpusClip-grade virality scoring, curiosity-gap hooks, and layout recommendations.
"""
import json
from loguru import logger
from config import settings
from services.llm_router import call_llm_json
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


def _keyword_score(text: str) -> float:
    """Score text 0-1 based on power-word matches."""
    if not text:
        return 0.0
    lower = text.lower()
    matches = sum(1 for word in POWER_WORDS if word in lower)
    return min(1.0, matches * 0.25)


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
    """Weighted multi-signal virality score.

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
                "virality_score": float,
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
        )

        response_text = response_text.replace("```json", "").replace("```", "").strip()
        data = json.loads(response_text)
        clips = data.get("clips", [])

        valid_clips = []
        for clip in clips:
            if clip.get("start") is None or clip.get("end") is None:
                continue
            if float(clip["start"]) < 0 or float(clip["end"]) > segments[-1]["end"]:
                continue
            if (float(clip["end"]) - float(clip["start"])) < (settings.MIN_CLIP_SECONDS * 0.6):
                continue

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
            clip["semantic_density"] = _semantic_density_score(clip_segment)
            clip["keyword_score"] = _keyword_score(clip_text)
            clip["structural_score"] = _structural_completeness(segments_in_clip)
            clip.setdefault("audio_energy_score", 0.5)

            # If the audio pre-pass enriched the segments, use the real energy average
            if segments_in_clip and any("audio_energy" in s for s in segments_in_clip):
                energies = [float(s["audio_energy"]) for s in segments_in_clip if "audio_energy" in s]
                if energies:
                    clip["audio_energy_score"] = round(sum(energies) / len(energies), 4)

            # Composite Virality Score — replaces raw LLM virality_score.
            # Keep the raw signal so the UI can show a truthful breakdown.
            clip["llm_score_raw"] = float(clip.get("virality_score", 0.75))
            clip["virality_score"] = _composite_score({**clip})

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
            title = " ".join(text.split()[:4]).upper() or f"VIRAL MOMENT {i + 1}"

            tone_info = classify_content_tone(text, story_type=story_plan.get("story_type", ""))
            
            # Compute heuristic signals
            h_audio = float(seg.get("audio_energy", 0.5))
            h_kw = _keyword_score(text)
            h_density = _semantic_density_score({"start": start, "end": end, "text": text})
            h_struct = 0.75
            h_composite = _composite_score({
                "virality_score": round(min(0.95, strength), 2),
                "audio_energy_score": h_audio,
                "keyword_score": h_kw,
                "semantic_density": h_density,
                "structural_score": h_struct,
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
                "story_type": story_plan.get("story_type", "reaction"),
                "layout": "single_speaker",
                "caption_style": "mrbeast",
                "cta": story_plan.get("cta", "Follow for more."),
                "tone_category": tone_info["tone_category"],
                "popup_eligible": tone_info["popup_eligible"],
                "style_profile": tone_info["style_profile"],
                "popup_cues": [],
            })

        seen = set()
        merged = []
        for clip in valid_clips + heuristic_clips:
            key = round(float(clip["start"]), 1), round(float(clip["end"]), 1)
            if key in seen:
                continue
            seen.add(key)
            merged.append(clip)

        merged.sort(key=lambda x: x.get("virality_score", 0.0), reverse=True)
        return merged[:num_clips]

    except Exception as e:
        logger.error(f"Clip selection failed: {e}")
        total_duration = segments[-1]["end"] if segments else settings.MIN_CLIP_SECONDS
        clip_length = min(settings.MAX_CLIP_SECONDS, max(settings.MIN_CLIP_SECONDS, total_duration / max(1, num_clips)))
        fallback_clips = []
        for i in range(num_clips):
            start = round(max(0.0, i * (total_duration / max(1, num_clips))))  # fixed: no trailing comma
            end = min(total_duration, start + clip_length)
            fallback_clips.append({
                "start": float(start),
                "end": float(end),
                "title": f"VIRAL MOMENT {i + 1}",
                "hook": "",
                "virality_score": 0.85 - (i * 0.05),
                "story_type": "reaction",
                "layout": "single_speaker",
                "caption_style": "mrbeast",
                "cta": "Follow for more.",
            })
        return fallback_clips


# Backward-compatibility alias — old callers used select_best_clips
select_best_clips = select_clips
