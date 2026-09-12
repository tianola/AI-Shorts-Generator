"""
Service for selecting the best clips from a video using AI.
Uses local Ollama API for clip selection.
"""
import json
from loguru import logger
from config import settings
from services.llm_router import call_llm_json
from services.story_analysis import analyze_story_segments

async def select_best_clips(segments_file: str, num_clips: int) -> list:
    """
    Select the best clips from a video using AI.

    Args:
        segments_file: Path to segments.json
        num_clips: Number of clips to select

    Returns:
        List of selected clips
    """
    with open(segments_file, "r", encoding="utf-8") as f:
        segments = json.load(f)

    # Create transcript for prompt
    transcript_text = ""
    for seg in segments:
        transcript_text += f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}\n"

    story_plan = await analyze_story_segments(segments)

    prompt = f"""
    You are an expert YouTube Shorts editor and growth strategist. Analyze the following transcript and select the {num_clips} most engaging, MrBeast-style viral clips.

    Story plan:
    - Summary: {story_plan.get('story_summary', '')}
    - Hook: {story_plan.get('hook', '')}
    - Story type: {story_plan.get('story_type', 'reaction')}
    - Recommended layout: {story_plan.get('recommended_layout', 'centered_face')}
    - CTA: {story_plan.get('cta', 'Follow for more.')}

    Requirements:
    - Each clip must have a clear HOOK within the first 3 seconds.
    - Prefer clips with strong emotional reactions, surprises, countdowns, challenges, or high-energy payoffs.
    - Clip length must be between {settings.MIN_CLIP_SECONDS} and {settings.MAX_CLIP_SECONDS} seconds.
    - Suggest attention-grabbing short titles and 1-line hook scripts suitable for TTS overlays.
    - Score clips by predicted virality (0.0-1.0).
    - If the story suggests a question-answer or reveal, prioritize that moment.
    - Recommended layout should be included per clip when possible.

    Transcript:
    {transcript_text}

    Return ONLY a JSON object with the following schema (valid JSON):
    {{
        "clips": [
            {{
                "start": float,
                "end": float,
                "title": "string",
                "hook": "string",
                "virality_score": float,
                "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
                "layout": "centered_face|split_screen|screen_share_face",
                "cta": "string"
            }}
        ]
    }}

    Notes: keep titles short (<= 60 chars), hooks under 20 words, prioritize clips that create curiosity and strong emotional reaction, and keep storytelling coherent.
    """

    try:
        response_text = await call_llm_json(
            prompt=prompt,
            system="You are an expert YouTube Shorts editor. Select the most engaging clips.",
            model=settings.OMNIROUTE_MODEL if settings.AI_PROVIDER == "omniroute" else settings.OLLAMA_MODEL,
            temperature=0.4,
        )

        # Clean response (remove markdown if present)
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
            clip.setdefault("layout", story_plan.get("recommended_layout", "centered_face"))
            clip.setdefault("cta", story_plan.get("cta", "Follow for more."))
            valid_clips.append(clip)

        # Sort by score
        valid_clips.sort(key=lambda x: x.get("virality_score", 0.0), reverse=True)
        if len(valid_clips) >= num_clips:
            return valid_clips[:num_clips]

        # Fill the rest with heuristically strong moments from across the video,
        # so each source video can still generate a healthy set of viral moments.
        heuristic_clips = []
        total_duration = segments[-1]["end"] if segments else settings.MIN_CLIP_SECONDS
        clip_length = min(settings.MAX_CLIP_SECONDS, max(settings.MIN_CLIP_SECONDS, total_duration / max(1, num_clips)))

        for i, seg in enumerate(segments):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            strength = 0.0
            strength += min(3.0, len(text.split()) / 12.0)
            if "?" in text or "!" in text:
                strength += 1.5
            for keyword in ["wow", "wait", "really", "crazy", "funny", "no way", "seriously", "stop", "what", "why"]:
                if keyword.lower() in text.lower():
                    strength += 0.8
            if strength < 1.2:
                continue

            start = max(0.0, seg["start"] - 1.0)
            end = min(total_duration, start + clip_length)
            if end - start < settings.MIN_CLIP_SECONDS * 0.8:
                end = min(total_duration, max(start + settings.MIN_CLIP_SECONDS, seg["end"]))
            title = " ".join(text.split()[:5])[:48].strip() or f"Viral moment {i + 1}"
            heuristic_clips.append({
                "start": float(start),
                "end": float(end),
                "title": title,
                "hook": "Watch this moment!",
                "virality_score": round(strength, 2),
                "story_type": story_plan.get("story_type", "reaction"),
                "layout": story_plan.get("recommended_layout", "centered_face"),
                "cta": story_plan.get("cta", "Follow for more."),
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
        # Fallback: simple heuristic when AI endpoint is unavailable
        try:
            total_duration = segments[-1]["end"] if segments else settings.MIN_CLIP_SECONDS
            # choose clip_length as min(MAX_CLIP_SECONDS, total_duration/num_clips*1.1) but at least MIN
            clip_length = min(settings.MAX_CLIP_SECONDS, max(settings.MIN_CLIP_SECONDS, total_duration / max(1, num_clips)))
            fallback_clips = []
            for i in range(num_clips):
                start = round(max(0.0, (i * (total_duration / max(1, num_clips)))))
                end = min(total_duration, start + clip_length)
                fallback_clips.append({
                    "start": float(start),
                    "end": float(end),
                    "title": f"Auto clip {i+1}",
                    "hook": "Watch this moment!",
                    "virality_score": 0.5 - (i * 0.1),
                    "story_type": "reaction",
                    "layout": "centered_face",
                    "cta": "Follow for more."
                })
            return fallback_clips
        except Exception as e2:
            logger.error(f"Fallback clip selection also failed: {e2}")
            raise Exception(f"Clip selection failed: {e}")