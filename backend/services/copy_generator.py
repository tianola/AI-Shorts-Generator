"""
Platform-Specific Copy Generator.
Generates tailored titles, descriptions, and hashtags for TikTok, Instagram Reels, and YouTube Shorts.
"""
from typing import Dict, Any
from loguru import logger
from pydantic import BaseModel, Field
from services.llm_router import call_llm_structured

class PlatformCopy(BaseModel):
    title: str = Field(..., description="Optimized title for the platform")
    description: str = Field(..., description="Engaging caption/description")
    hashtags: list[str] = Field(..., description="Targeted trending hashtags")

class MultiPlatformCopy(BaseModel):
    tiktok: PlatformCopy
    instagram: PlatformCopy
    youtube: PlatformCopy

async def generate_platform_copies(transcript: str, base_title: str) -> Dict[str, Any]:
    """
    Generate platform-specific copies for TikTok, IG Reels, and YT Shorts using LLM.
    """
    system_prompt = (
        "You are an elite social media growth expert. Your task is to take a video clip transcript "
        "and base title, and generate hyper-optimized, viral titles, descriptions, and hashtags tailored "
        "specifically for TikTok, Instagram Reels, and YouTube Shorts."
    )

    user_prompt = f"""
    Base Title: {base_title}
    Transcript: {transcript[:1000]}

    Provide tailored viral copy for each of the three platforms (TikTok, Instagram Reels, YouTube Shorts).
    """

    try:
        result: MultiPlatformCopy = await call_llm_structured(
            response_model=MultiPlatformCopy,
            prompt=user_prompt,
            system=system_prompt,
            temperature=0.5
        )
        return result.model_dump()
    except Exception as e:
        logger.warning(f"Platform copy generation failed ({e}), using generic fallback copy")
        # Fallback copy
        fallback = {
            "title": base_title,
            "description": f"Watch this amazing moment! #{base_title.replace(' ', '')}",
            "hashtags": ["#shorts", "#viral", "#foryou"]
        }
        return {
            "tiktok": fallback,
            "instagram": fallback,
            "youtube": fallback
        }
