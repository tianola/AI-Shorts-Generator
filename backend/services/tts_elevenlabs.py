"""
Lightweight ElevenLabs TTS helper using httpx. Does not require the official SDK.
Writes an mp3 file to the provided output path.
"""
import os
import httpx
from typing import Optional
from config import settings
from utils.logger import job_info, job_error

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_VOICE_ID = "CwhRBWXzGAHq8TQ4Fs17"


def _resolve_voice_id(voice_id: Optional[str]) -> str:
	"""Resolve a configured voice alias or name to a valid ElevenLabs voice ID."""
	selected = voice_id or settings.ELEVENLABS_VOICE_ID or DEFAULT_VOICE_ID
	if selected and len(selected) >= 10 and selected.count("-") == 0 and " " not in selected:
		return selected

	api_key = settings.ELEVENLABS_API_KEY
	if not api_key:
		return DEFAULT_VOICE_ID

	try:
		response = httpx.get(
			f"{ELEVENLABS_API_BASE}/voices",
			headers={"xi-api-key": api_key},
			timeout=30.0,
		)
		response.raise_for_status()
		voices = response.json().get("voices", [])
		for voice in voices:
			voice_name = str(voice.get("name", "")).lower()
			if voice_name == str(selected).lower() or str(selected).lower() in voice_name:
				return voice["voice_id"]
	except Exception:
		pass

	return DEFAULT_VOICE_ID


def synthesize_to_file(text: str, output_path: str, voice_id: Optional[str] = None) -> Optional[str]:
	"""
	Synthesize text using ElevenLabs and save to output_path (mp3).
	Requires ELEVENLABS_API_KEY to be set in environment or config.
	Returns output_path on success or None on failure.
	"""
	if not settings.TTS_ENABLED:
		job_error(0, "TTS is disabled in settings")
		return None

	api_key = settings.ELEVENLABS_API_KEY
	if not api_key:
		job_error(0, "ElevenLabs API key not configured")
		return None

	voice = _resolve_voice_id(voice_id)
	url = f"{ELEVENLABS_API_BASE}/text-to-speech/{voice}"

	headers = {
		"Accept": "audio/mpeg",
		"Content-Type": "application/json",
		"xi-api-key": api_key,
	}

	payload = {
		"text": text,
		"model_id": "eleven_turbo_v2_5",
		"voice_settings": {
			"stability": 0.5,
			"similarity_boost": 0.75,
		},
	}

	try:
		os.makedirs(os.path.dirname(output_path), exist_ok=True)
		response = httpx.post(url, headers=headers, json=payload, timeout=60.0)
		response.raise_for_status()
		with open(output_path, "wb") as f:
			f.write(response.content)
		job_info(0, f"TTS saved to {output_path}")
		return output_path

	except Exception as e:
		job_error(0, f"ElevenLabs TTS failed: {str(e)}")
		return None
