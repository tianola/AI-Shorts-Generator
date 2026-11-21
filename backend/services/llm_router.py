import json
import re
import httpx
from typing import Optional, Type, TypeVar
from pydantic import BaseModel
from loguru import logger
import instructor
from openai import AsyncOpenAI

from config import settings

T = TypeVar("T", bound=BaseModel)

# Total OmniRoute tries before the Ollama fallback. More than one attempt is
# worth it because a pooled `auto/*` route resolves to a different upstream per
# call, so a timeout on one says nothing about the next; job 110 lost its whole
# clip selection to a single ReadTimeout. Two is the smallest number that helps.
TRANSPORT_ATTEMPTS = 2

# Read timeout for one OmniRoute call. The free combos (free/best, free/coding)
# fall through their members server-side with a 120 s first-output budget each, so
# a 120 s client timeout gave up at the exact moment the combo moved to its next
# member. 300 s lets the combo try a second member inside one call.
OMNIROUTE_TIMEOUT_SECONDS = 300.0

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


_DECODER = json.JSONDecoder()
_FIRST_ARRAY = re.compile(r'"([^"]+)"\s*:\s*\[')


def omniroute_chain(model: Optional[str] = None) -> list:
	"""The OmniRoute models to try in order: this one, then the configured fallbacks.

	Built from the caller's `model`, not from settings, because clip_selector names
	the model explicitly - reading only the setting would leave production calls
	single-legged. Deduplicated so a fallback that repeats the head is not tried
	twice before any other model gets a chance, and blanks skipped so a trailing
	comma in .env does not become an empty leg.
	"""
	legs = []
	candidates = [model or settings.OMNIROUTE_MODEL] + (
		settings.OMNIROUTE_MODEL_FALLBACKS or ""
	).split(",")
	for candidate in candidates:
		candidate = (candidate or "").strip()
		if candidate and candidate not in legs:
			legs.append(candidate)
	return legs


def _json_object_in(content) -> str:
	"""The JSON object a reply carries, or "" when it carries none.

	`_extract_json_text` hands its input back unchanged when there are no braces,
	so a bare refusal used to count as a successful answer. Under a chain that test
	decides whether to hop to the next model, so it has to mean what it says.

	A thinking model behind a combo can answer with a content array
	([{"type": "thinking", ...}, {"type": "text", "text": "{...}"}]) instead of a
	string; job 110 lost its selection to that shape. The text blocks are the answer.
	"""
	if isinstance(content, list):
		content = "".join(
			str(block.get("text") or "")
			for block in content
			if isinstance(block, dict) and block.get("type") in ("text", "output_text")
		)
	if not isinstance(content, str) or "{" not in content:
		return ""
	return _extract_json_text(content)


def salvage_incomplete_json(text: str) -> dict:
	"""Parse JSON the model never finished emitting.

	A reply cut at the output token limit still contains every array element that
	closed before the cut - and _extract_json_text strips the envelope down to
	`{"clips": [ ... }`, which json.loads rejects at EOF. Recovering the complete
	elements keeps the analysis; discarding the whole reply throws it away and the
	caller silently degrades to its no-AI fallback.

	Returns {} only when there is no array to recover from.
	"""
	text = _extract_json_text(text)
	start = text.find("{")
	if start == -1:
		return {}
	try:
		return json.loads(text[start:])
	except ValueError:
		pass

	array = _FIRST_ARRAY.search(text)
	if not array:
		return {}

	pos = array.end()
	items = []
	while pos < len(text):
		while pos < len(text) and text[pos] in " \t\r\n,":
			pos += 1
		if pos >= len(text) or text[pos] == "]":
			break
		try:
			item, pos = _DECODER.raw_decode(text, pos)
		except ValueError:
			break
		items.append(item)

	return {array.group(1): items}


def pick_model(ollama_model: Optional[str] = None) -> str:
	"""The model name for the configured provider: OmniRoute's, or the given Ollama one."""
	if settings.AI_PROVIDER.lower() == "ollama":
		return ollama_model or settings.OLLAMA_MODEL
	return settings.OMNIROUTE_MODEL


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
	"""Call the configured LLM backend (OmniRoute primary, Ollama fallback) and return raw JSON content."""
	provider = settings.AI_PROVIDER.lower()

	if provider == "omniroute":
		base_url = settings.OMNIROUTE_BASE_URL.rstrip("/")
		api_key = settings.OMNIROUTE_API_KEY or ""
		if not api_key:
			logger.warning("OMNIROUTE_API_KEY is missing, falling back to Ollama")
		else:
			legs = omniroute_chain(model)
			# The first reply with no JSON in it, kept so a chain in which every model
			# refused still hands the caller the model's own words. The callers turn
			# prose into their own error already; a raise would read as a different bug.
			unusable_reply = None

			for target_model in legs:
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

				for attempt in range(1, TRANSPORT_ATTEMPTS + 1):
					try:
						async with httpx.AsyncClient(timeout=OMNIROUTE_TIMEOUT_SECONDS) as client:
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

						json_text = _json_object_in(content)
						if json_text:
							logger.info(f"Successfully called OmniRoute model '{target_model}'")
							return json_text
						# A 200 with no JSON object is not a transport error, and re-asking the
						# model that just refused costs 120 s to hear the same words again. The
						# next model in the chain is tried instead, without burning this leg's
						# retry - which belongs to the transport, not to the content.
						if unusable_reply is None and isinstance(content, str) and content.strip():
							unusable_reply = content
						logger.warning(
							f"OmniRoute model '{target_model}' returned no usable JSON "
							f"(content={repr(content)[:120]}), trying the next model..."
						)
						break
					except Exception as e:
						# Some httpx failures stringify to "" and left the log reading
						# "OmniRoute request failed ()", which names no cause at all.
						if attempt < TRANSPORT_ATTEMPTS:
							logger.warning(
								f"OmniRoute attempt {attempt} for '{target_model}' failed "
								f"({type(e).__name__}: {e}); retrying this model..."
							)
							continue
						logger.warning(
							f"OmniRoute model '{target_model}' failed ({type(e).__name__}: {e}), "
							f"trying the next of {len(legs)} model(s)..."
						)

			if unusable_reply is not None:
				logger.warning(
					f"No OmniRoute model in the chain returned usable JSON "
					f"({len(legs)} tried); passing the first reply through unchanged"
				)
				return unusable_reply

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
