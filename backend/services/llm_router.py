import json
import httpx
from typing import Optional
from loguru import logger

from config import settings


import json
import httpx
from typing import Optional, Type, TypeVar
from pydantic import BaseModel
from loguru import logger
import instructor
from openai import AsyncOpenAI

from config import settings

T = TypeVar("T", bound=BaseModel)

def _extract_json_text(text: str) -> str:
	"""Helper to extract JSON text from raw LLM output."""
	text = text.strip()
	if text.startswith("```json"):
		text = text[7:]
	elif text.startswith("```"):
		text = text[3:]
	if text.endswith("```"):
		text = text[:-3]
	text = text.strip()

	start_idx = text.find("{")
	end_idx = text.rfind("}")
	if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
		return text[start_idx:end_idx + 1]
	return text


async def call_llm_structured(
	response_model: Type[T],
	prompt: str,
	system: str,
	model: Optional[str] = None,
	temperature: float = 0.4,
	max_tokens: int = 2000,
) -> T:
	"""
	Call LLM using Instructor to return a validated Pydantic model directly.
	"""
	provider = settings.AI_PROVIDER.lower()

	if provider == "omniroute":
		base_url = settings.OMNIROUTE_BASE_URL.rstrip("/")
		api_key = settings.OMNIROUTE_API_KEY or "none"
		target_model = model or settings.OMNIROUTE_MODEL

		try:
			# Use AsyncOpenAI wrapped with Instructor
			client = instructor.from_openai(
				AsyncOpenAI(base_url=base_url, api_key=api_key)
			)
			response = await client.chat.completions.create(
				model=target_model,
				response_model=response_model,
				messages=[
					{"role": "system", "content": system},
					{"role": "user", "content": prompt},
				],
				temperature=temperature,
				max_tokens=max_tokens,
			)
			logger.info(f"Successfully called OmniRoute (Instructor) model '{target_model}'")
			return response
		except Exception as e:
			logger.warning(f"OmniRoute Instructor request failed ({e}), falling back to standard JSON parsing...")

	# Fallback using standard call_llm_json and parsing into Pydantic
	json_str = await call_llm_json(prompt=prompt, system=system, model=model, temperature=temperature, max_tokens=max_tokens)
	data = json.loads(json_str)
	return response_model.model_validate(data)


async def call_llm_json(
	prompt: str,
	system: str,
	model: Optional[str] = None,
	temperature: float = 0.4,
	max_tokens: int = 2000,
) -> str:
	"""Call the configured LLM backend (OmniRoute primary with auto/pro-reasoning model, Ollama fallback) and return raw JSON content."""
	provider = settings.AI_PROVIDER.lower()

	if provider == "omniroute":
		base_url = settings.OMNIROUTE_BASE_URL.rstrip("/")
		api_key = settings.OMNIROUTE_API_KEY or ""
		if not api_key:
			logger.warning("OMNIROUTE_API_KEY is missing, falling back to Ollama")
		else:
			target_model = model or settings.OMNIROUTE_MODEL
			payload = {
				"model": target_model,
				"messages": [
					{"role": "system", "content": system},
					{"role": "user", "content": prompt},
				],
				"temperature": temperature,
				"max_tokens": max_tokens,
				"response_format": {"type": "json_object"},
			}

			try:
				async with httpx.AsyncClient(timeout=120.0) as client:
					response = await client.post(
						f"{base_url}/chat/completions",
						headers={
							"Authorization": f"Bearer {api_key}",
							"Content-Type": "application/json",
						},
						json=payload,
					)
					response.raise_for_status()
					data = response.json()

				choice = data.get("choices", [{}])[0]
				message = choice.get("message", {})
				content = message.get("content", "")

				if content and isinstance(content, str):
					json_text = _extract_json_text(content)
					if json_text:
						logger.info(f"Successfully called OmniRoute model '{target_model}'")
						return json_text
			except Exception as e:
				logger.warning(f"OmniRoute request failed ({e}), attempting Ollama fallback...")

	# Fallback / Direct Ollama API
	ollama_model = model if provider == "ollama" else settings.OLLAMA_MODEL
	payload = {
		"model": ollama_model,
		"prompt": prompt,
		"system": system,
		"stream": False,
		"format": "json",
		"options": {
			"temperature": temperature,
			"num_ctx": 8192,
		},
	}

	try:
		async with httpx.AsyncClient(timeout=120.0) as client:
			response = await client.post(f"{settings.OLLAMA_URL.rstrip('/')}/api/generate", json=payload)
			response.raise_for_status()
			result = response.json()

		response_text = result.get("response", "")
		if not isinstance(response_text, str) or not response_text.strip():
			raise ValueError("Ollama response did not produce a valid text answer")
		return _extract_json_text(response_text)
	except Exception as e:
		logger.warning(f"Ollama request failed: {e}")
		raise RuntimeError(f"All LLM backends failed (OmniRoute and Ollama: {e})")
