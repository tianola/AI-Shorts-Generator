"""Claude (Anthropic API) client for the clip selector.

Kept apart from llm_router.py on purpose: that module speaks the OpenAI wire
format to OmniRoute/Ollama, this one uses the official Anthropic SDK. Only the
clip selection is meant to come here - metadata and scene attention stay on the
free chain.

The key is read from settings (backend/.env), not from the process environment,
so the NSSM services pick it up the same way they pick up every other setting.
"""
import asyncio
import time
import uuid
from typing import Optional

from loguru import logger

from config import settings

# Server-side refusal fallback: if a safety classifier declines the request, the
# API re-runs it on a fallback model inside the same call instead of returning
# nothing to the selector.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# How often a batched call checks whether its batch has finished.
_BATCH_POLL_SECONDS = 15

# One system prompt for every Claude pass over a video. The cache key is the
# prefix (system, then the transcript block), so story analysis and candidate
# selection only share the cached transcript if their system text is identical.
SHORTS_SYSTEM = (
    "You pick and judge moments from long videos for standalone vertical shorts. "
    "You are honest about quality and you answer with JSON only."
)


def claude_available() -> bool:
    """True when a key is configured, i.e. the selector may route to Claude."""
    return bool((settings.ANTHROPIC_API_KEY or "").strip())


async def call_claude_vision_json(
    prompt: str,
    system: str,
    schema: dict,
    image_paths: list,
    model: Optional[str] = None,
    max_tokens: int = 4000,
) -> str:
    """Like call_claude_json, with JPEG frames attached to the prompt, each preceded by its label.

    `image_paths` is a list of (label, path). Used by the editorial evaluator, which sends fixed
    frames of a finished clip. Raises on transport errors, a refusal or a reply cut at max_tokens.
    """
    import base64

    import anthropic

    content = []
    for label, path in image_paths:
        with open(path, "rb") as handle:
            data = base64.standard_b64encode(handle.read()).decode("ascii")
        content.append({"type": "text", "text": label})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}})
    content.append({"type": "text", "text": prompt})

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    target_model = model or settings.CLAUDE_EDITORIAL_MODEL
    async with client.beta.messages.stream(
        model=target_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": schema}},
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        message = await stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request (model '{target_model}')")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(f"Claude reply was cut at max_tokens={max_tokens}")
    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text.strip():
        raise RuntimeError("Claude returned no text block")
    logger.info(f"Successfully called Claude vision model '{message.model}' "
                f"(in={message.usage.input_tokens}, out={message.usage.output_tokens})")
    return text


async def call_claude_json(
    prompt: str,
    system: str,
    schema: dict,
    model: Optional[str] = None,
    max_tokens: int = 16000,
    effort: str = "medium",
    context: str = "",
    visual: Optional[list] = None,
) -> str:
    """Ask Claude for one JSON object matching `schema` and return it as text.

    `schema` is a JSON Schema with additionalProperties false and every property
    required; the API constrains the reply to it, so the caller can json.loads
    the result without salvage. Raises on transport errors, on a refusal and on a
    reply cut at max_tokens - the caller decides whether to fall back to the
    OmniRoute chain.

    `context` is large input that several calls on one video share (the
    transcript). It goes first, marked for prompt caching, so the second call
    within five minutes reads it at about a tenth of the input price.
    `visual` is a list of content blocks (images and their labels) placed
    after the context and before the task.
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    target_model = model or settings.CLAUDE_SELECTION_MODEL
    content = _user_content(prompt, context, visual)
    output_config = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}

    message = None
    if settings.CLAUDE_USE_BATCH:
        message = await _via_batch(client, target_model, max_tokens, system, content, output_config)
    if message is None:
        message = await _via_stream(client, target_model, max_tokens, system, content, output_config)
    return _json_text(message, target_model, max_tokens, effort)


async def _via_stream(client, target_model, max_tokens, system, content, output_config):
    # Streamed because a long transcript plus a large output budget can outlast
    # the HTTP timeout of a plain request.
    async with client.beta.messages.stream(
        model=target_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config=output_config,
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        return await stream.get_final_message()


async def _via_batch(client, target_model, max_tokens, system, content, output_config):
    """Send the call through the Message Batches API, at half the token price.

    A batch can take minutes instead of seconds, which is fine for a worker that
    renders overnight and not for anyone watching the progress bar, hence opt-in
    (CLAUDE_USE_BATCH). The server-side refusal fallback is not accepted on
    batches, so a refused batch request raises like a refused stream does.

    Returns None when the batch did not finish within CLAUDE_BATCH_TIMEOUT_MINUTES
    or could not be submitted; the caller then makes the normal streamed call.
    """
    custom_id = f"shorts-{uuid.uuid4().hex[:24]}"
    try:
        batch = await client.messages.batches.create(requests=[{
            "custom_id": custom_id,
            "params": {
                "model": target_model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": content}],
                "output_config": output_config,
            },
        }])
    except Exception as exc:
        logger.warning(f"Claude batch submit failed ({type(exc).__name__}: {exc}); calling directly")
        return None

    deadline = time.monotonic() + settings.CLAUDE_BATCH_TIMEOUT_MINUTES * 60
    while batch.processing_status != "ended":
        if time.monotonic() >= deadline:
            logger.warning(f"Claude batch {batch.id} still running after "
                           f"{settings.CLAUDE_BATCH_TIMEOUT_MINUTES} min; cancelling and calling directly")
            try:
                await client.messages.batches.cancel(batch.id)
            except Exception:
                pass
            return None
        await asyncio.sleep(_BATCH_POLL_SECONDS)
        batch = await client.messages.batches.retrieve(batch.id)

    async for entry in await client.messages.batches.results(batch.id):
        if entry.custom_id != custom_id:
            continue
        if entry.result.type == "succeeded":
            return entry.result.message
        detail = getattr(getattr(entry.result, "error", None), "error", None)
        raise RuntimeError(f"Claude batch request {entry.result.type}: {detail or 'no detail'}")
    raise RuntimeError(f"Claude batch {batch.id} ended without a result")


def _json_text(message, target_model: str, max_tokens: int, effort: str) -> str:
    if message.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request (model '{target_model}')")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            f"Claude reply was cut at max_tokens={max_tokens}; raise the budget"
        )

    text = next((b.text for b in message.content if b.type == "text"), "")
    if not text.strip():
        raise RuntimeError("Claude returned no text block")

    usage = message.usage
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    logger.info(
        f"Successfully called Claude model '{message.model}' "
        f"(in={usage.input_tokens}, cached={cached}, out={usage.output_tokens}, effort={effort})"
    )
    return text


def _user_content(prompt: str, context: str, visual: Optional[list] = None):
    """The user turn: the shared context as a cached block, any images, then this call's task."""
    if not context and not visual:
        return prompt
    blocks = []
    if context:
        blocks.append({"type": "text", "text": context, "cache_control": {"type": "ephemeral"}})
    blocks.extend(visual or [])
    blocks.append({"type": "text", "text": prompt})
    return blocks
