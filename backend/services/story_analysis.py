"""Story analysis and hook extraction for AI Shorts generation."""
import json
from typing import List, Dict, Any, Optional

from loguru import logger

from services.llm_router import call_llm_json, pick_model

_STORY_TYPES = ["question_answer", "reveal", "challenge", "emotion", "lesson", "reaction"]

STORY_SCHEMA = {
	"type": "object",
	"properties": {
		"story_summary": {"type": "string"},
		"hook": {"type": "string"},
		"story_type": {"type": "string", "enum": _STORY_TYPES},
		"recommended_layout": {"type": "string", "enum": ["centered_face", "split_screen", "screen_share_face"]},
		"cta": {"type": "string"},
		"key_moments": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"start": {"type": "number"},
					"end": {"type": "number"},
					"reason": {"type": "string"},
				},
				"required": ["start", "end", "reason"],
				"additionalProperties": False,
			},
		},
	},
	"required": ["story_summary", "hook", "story_type", "recommended_layout", "cta", "key_moments"],
	"additionalProperties": False,
}


def transcript_block(segments: List[Dict[str, Any]]) -> str:
	"""The timestamped transcript every pass over a video reads.

	Story analysis and candidate selection must send byte-identical text: on
	Claude it is the cached prefix, and one differing character makes the second
	call pay full price for the whole transcript again.
	"""
	lines = "".join(
		f"[{float(seg.get('start', 0)):.1f}s - {float(seg.get('end', 0)):.1f}s] {str(seg.get('text', '')).strip()}\n"
		for seg in segments
		if str(seg.get("text", "")).strip()
	)
	return f"Transcript:\n{lines}"


async def _story_json(transcript: str, instructions: str) -> str:
	"""Claude when a key is set (the transcript cached for the selection pass), else the free chain."""
	from services.claude_client import SHORTS_SYSTEM, call_claude_json, claude_available

	if claude_available():
		try:
			return await call_claude_json(
				prompt=instructions,
				system=SHORTS_SYSTEM,
				schema=STORY_SCHEMA,
				max_tokens=16000,
				effort="medium",
				context=transcript,
			)
		except Exception as exc:
			logger.warning(f"Claude story analysis failed ({type(exc).__name__}: {exc}); trying OmniRoute")

	return await call_llm_json(
		prompt=f"{transcript}\n{instructions}",
		system=SHORTS_SYSTEM,
		model=pick_model(),
		temperature=0.3,
		max_tokens=1500,
	)


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

	transcript = transcript_block(segments)
	title_context = f"\nSource video title: {video_title}\n" if video_title else ""

	instructions = f"""
The transcript above is the whole video, with timestamps.{title_context}
As a short-form producer, find the highest-stakes, most gripping narrative arc in it.

- story_summary: one or two sentences on what the video is about and where its tension is.
- hook: the line or idea that creates the most tension, curiosity or emotional shock.
- story_type: question_answer, reveal, challenge, emotion, lesson, or reaction.
- recommended_layout: centered_face, split_screen, or screen_share_face.
- cta: a short call to action for the end of a clip.
- key_moments: up to 8 moments where a climax, confrontation, reveal or key insight
  happens, each with start and end in seconds from the transcript and a one-line reason.

Return ONLY valid JSON in this shape:
{{
  "story_summary": "string",
  "hook": "string",
  "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
  "recommended_layout": "centered_face|split_screen|screen_share_face",
  "cta": "string",
  "key_moments": [{{"start": float, "end": float, "reason": "string"}}]
}}
"""

	try:
		response_text = await _story_json(transcript, instructions)
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
