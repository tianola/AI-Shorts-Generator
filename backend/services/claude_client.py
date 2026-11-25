"""Claude (Anthropic API) client for the clip selector.

Kept apart from llm_router.py on purpose: that module speaks the OpenAI wire
format to OmniRoute/Ollama, this one uses the official Anthropic SDK. Only the
clip selection is meant to come here - metadata and scene attention stay on the
free chain.

The key is read from settings (backend/.env), not from the process environment,
so the NSSM services pick it up the same way they pick up every other setting.
"""
from typing import Optional

from loguru import logger

from config import settings

# Server-side refusal fallback: if a safety classifier declines the request, the
# API re-runs it on a fallback model inside the same call instead of returning
# nothing to the selector.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"

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


async def call_claude_json(
    prompt: str,
    system: str,
    schema: dict,
    model: Optional[str] = None,
    max_tokens: int = 16000,
    effort: str = "medium",
    context: str = "",
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
    """
    import anthropic

    client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    target_model = model or settings.CLAUDE_SELECTION_MODEL

    # Streamed because a long transcript plus a large output budget can outlast
    # the HTTP timeout of a plain request.
    async with client.beta.messages.stream(
        model=target_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": _user_content(prompt, context)}],
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        betas=[_FALLBACK_BETA],
        fallbacks="default",
    ) as stream:
        message = await stream.get_final_message()

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


def _user_content(prompt: str, context: str):
    """The user turn: the shared context as a cached block, then this call's task."""
    if not context:
        return prompt
    return [
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": prompt},
    ]
