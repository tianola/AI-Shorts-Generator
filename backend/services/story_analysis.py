"""Story analysis and hook extraction for AI Shorts generation."""
import json
from typing import List, Dict, Any, Optional

from loguru import logger

from config import settings
from services.llm_router import call_llm_json


async def analyze_story_segments(segments: List[Dict[str, Any]], video_title: Optional[str] = None) -> Dict[str, Any]:
	"""Detect the strongest story arc and hook in a transcript.

	Returns a structured dict with a short hook, a summary of the story, and
	recommended editing/layout hints for the resulting short-form clips.
	"""
	if not segments:
		return {
			"story_summary": "High-energy moment",
			"hook": "This is the big reveal.",
			"story_type": "reaction",
			"recommended_layout": "centered_face",
			"cta": "Follow for more.",
			"key_moments": [],
		}

	transcript_text = "\n".join(
		f"[{seg.get('start', 0):.1f}s - {seg.get('end', 0):.1f}s] {str(seg.get('text', '')).strip()}"
		for seg in segments
		if str(seg.get('text', '')).strip()
	)

	title_context = f"\nSource Video Title / Context: {video_title}\n" if video_title else ""

	prompt = f"""
	You are a master YouTube Shorts & TikTok viral producer.
	Read the transcript{title_context}and identify the highest-stakes, most gripping narrative arc for short-form viral videos.

	Focus on:
	- The HIGHEST STAKES Hook: Identify what creates the most tension, suspense, curiosity gap, or emotional shock.
	- Core Conflict: What is the most shocking revelation, confrontation, danger, or twist?
	- High-Energy Moments: Exactly where the climax, confrontation, or critical insight happens.
	- Story Type: question_answer, reveal, challenge, emotion, lesson, or reaction.
	- Recommended Layout: centered_face, split_screen, or screen_share_face.
	- A snappy CTA for the end.

	Transcript:
	{transcript_text}

	Return ONLY valid JSON with this exact schema:
	{{
	  "story_summary": "string",
	  "hook": "string",
	  "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
	  "recommended_layout": "centered_face|split_screen|screen_share_face",
	  "cta": "string",
	  "key_moments": [
		{{"start": float, "end": float, "reason": "string"}}
	  ]
	}}
	"""

	try:
		response_text = await call_llm_json(
			prompt=prompt,
			system="You are an expert storyteller for short-form viral video. Extract the strongest hook and story angle.",
			model=settings.OMNIROUTE_MODEL if settings.AI_PROVIDER == "omniroute" else settings.OLLAMA_MODEL,
			temperature=0.3,
			max_tokens=1500,
		)
		response_text = response_text.replace("```json", "").replace("```", "").strip()
		data = json.loads(response_text)

		key_moments = data.get("key_moments") or []
		if not isinstance(key_moments, list):
			key_moments = []

		return {
			"story_summary": str(data.get("story_summary") or "High-energy moment")[:200],
			"hook": str(data.get("hook") or "This is the big reveal.")[:160],
			"story_type": str(data.get("story_type") or "reaction").lower(),
			"recommended_layout": str(data.get("recommended_layout") or "centered_face").lower(),
			"cta": str(data.get("cta") or "Follow for more.")[:120],
			"key_moments": key_moments,
		}
	except Exception as exc:
		logger.warning(f"Story analysis failed: {exc}")

		first_text = ""
		for seg in segments:
			text = str(seg.get("text") or "").strip()
			if text:
				first_text = text
				break

		hook = first_text[:140] if first_text else "This is the big reveal."
		story_type = "reaction" if any(ch in hook for ch in ['?', '!']) else "lesson"

		return {
			"story_summary": "The strongest moment is a direct, high-energy statement with a clear payoff.",
			"hook": hook,
			"story_type": story_type,
			"recommended_layout": "centered_face",
			"cta": "Follow for more.",
			"key_moments": [
				{"start": float(segments[0].get("start", 0.0)), "end": float(segments[-1].get("end", 0.0)), "reason": "Strong narrative peak"}
			] if segments else [],
		}
