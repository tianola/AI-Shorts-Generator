"""LLM provider fallback chain.

Tries providers in order: OmniRoute → Together AI → HuggingFace Router → Google AI Studio.
Each provider is OpenAI-compatible except Google (uses genai SDK).
"""
import os
from typing import Optional

from loguru import logger
from openai import OpenAI

from config import settings


_clients: dict = {}


def _get_client(provider: str) -> Optional[OpenAI]:
    """Lazy-init OpenAI clients per provider."""
    if provider in _clients:
        return _clients[provider]

    if provider == "omniroute" and settings.OMNIROUTE_API_KEY:
        _clients[provider] = OpenAI(
            base_url=settings.OMNIROUTE_BASE_URL,
            api_key=settings.OMNIROUTE_API_KEY,
        )
    elif provider == "together" and settings.TOGETHER_API_KEY:
        _clients[provider] = OpenAI(
            base_url=settings.TOGETHER_BASE_URL,
            api_key=settings.TOGETHER_API_KEY,
        )
    elif provider == "hf_router":
        _clients[provider] = OpenAI(
            base_url=settings.HF_ROUTER_BASE_URL,
            api_key=settings.HF_KEY or os.getenv("HF_TOKEN", ""),
        )
    else:
        return None

    return _clients[provider]


def chat_completion(
    messages: list,
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
) -> Optional[str]:
    """Try each provider in order, return first successful response.

    Args:
        messages: OpenAI-format messages list
        model: Override model (uses provider default if None)
        temperature: Sampling temperature
        max_tokens: Max response tokens

    Returns:
        Response text or None if all providers failed
    """
    providers = [
        ("omniroute", settings.OMNIROUTE_MODEL, settings.OMNIROUTE_API_KEY),
        ("together", settings.TOGETHER_MODEL, settings.TOGETHER_API_KEY),
        ("hf_router", settings.HF_ROUTER_MODEL, settings.HF_KEY),
    ]

    for provider_name, default_model, api_key in providers:
        if not api_key:
            continue

        client = _get_client(provider_name)
        if not client:
            continue

        try:
            use_model = model or default_model
            logger.debug(f"Trying {provider_name} with model {use_model}")

            response = client.chat.completions.create(
                model=use_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            content = response.choices[0].message.content
            logger.info(f"{provider_name} succeeded ({len(content)} chars)")
            return content

        except Exception as e:
            logger.warning(f"{provider_name} failed: {e}")
            continue

    logger.error("All LLM providers failed")
    return None


def google_gemini_completion(
    prompt: str,
    model: Optional[str] = None,
) -> Optional[str]:
    """Google AI Studio (Gemini) for vision tasks.

    Separate from the chain because it uses a different SDK.
    """
    if not settings.GOOGLE_AI_STUDIO_KEY:
        return None

    try:
        from google import genai

        client = genai.Client(api_key=settings.GOOGLE_AI_STUDIO_KEY)
        use_model = model or settings.GOOGLE_AI_STUDIO_MODEL

        response = client.models.generate_content(
            model=use_model,
            contents=prompt,
        )
        content = response.text
        logger.info(f"Google Gemini succeeded ({len(content)} chars)")
        return content

    except Exception as e:
        logger.warning(f"Google Gemini failed: {e}")
        return None
