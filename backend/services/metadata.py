"""
Service for generating metadata (title, description, hashtags) for clips.
Uses Instructor structured LLM output via OmniRoute/Ollama.
"""
import re
from typing import List
from pydantic import BaseModel, Field
from loguru import logger
from config import settings
from services.llm_router import call_llm_structured

class ClipMetadata(BaseModel):
    title: str = Field(..., description="Captivating, concise, viral title under 60 chars (no emojis, no hashtags in title)")
    description: str = Field(..., description="2-3 sentence engaging SEO description with context and CTA")
    hashtags: List[str] = Field(default_factory=lambda: ["#Shorts"], description="4-5 relevant hashtags starting with #Shorts")
    story_type: str = Field(default="reaction", description="question_answer|reveal|challenge|emotion|lesson|reaction")
    cta: str = Field(default="Follow for more.", description="Call to action")

def _clean_title(raw_title: str) -> str:
    """Clean and polish a clip title for TikTok/Shorts overlays."""
    t = raw_title.strip().strip('"\'`')
    t = re.sub(r'#\w+', '', t).strip()
    # Remove trailing punctuation like dots or commas
    t = re.sub(r'[\.,;]+$', '', t).strip()
    if len(t) > 60:
        t = t[:57].rsplit(' ', 1)[0] + '...'
    return t or "Viral Moment"

async def generate_metadata(
    transcript: str,
    title: str = "",
    hook: str = ""
) -> dict:
    """
    Generate clean, viral metadata for a clip using AI.

    Args:
        transcript: The clip's transcript text
        title: Optional existing title
        hook: Optional existing hook

    Returns:
        Dictionary with title, description, and hashtags
    """
    system_prompt = (
        "You are a top-tier viral YouTube Shorts and TikTok strategist. "
        "Create concise, high-converting, curiosity-inducing titles (under 60 chars) "
        "and clean SEO metadata based on the clip transcript."
    )

    user_prompt = f"""
    Clip Transcript:
    \"\"\"{transcript.strip()}\"\"\"

    Context:
    - Current Title draft: {title or 'None'}
    - Key Hook: {hook or 'None'}

    Requirements:
    1. Title: Must be SHORT (under 60 chars), punchy, curiosity-driven (e.g. 'He Actually Did This...', 'The Secret Nobody Knew', 'Wait For The Reaction').
    2. Do NOT put hashtags or emojis in the title.
    3. Description: 2-3 sentences explaining context with a clean CTA.
    4. Hashtags: 4-5 relevant tags starting with #Shorts.
    """

    try:
        data: ClipMetadata = await call_llm_structured(
            response_model=ClipMetadata,
            prompt=user_prompt,
            system=system_prompt,
            model=settings.OMNIROUTE_MODEL if settings.AI_PROVIDER == "omniroute" else settings.METADATA_OLLAMA_MODEL,
            temperature=0.3,
        )

        final_title = _clean_title(data.title)
        tags = [tag if tag.startswith('#') else f"#{tag}" for tag in data.hashtags if tag]
        if "#Shorts" not in tags:
            tags.insert(0, "#Shorts")

        # Prepend mandatory hashtags from config (always present, deduped, config wins order)
        mandatory = [
            t.strip() if t.strip().startswith("#") else f"#{t.strip()}"
            for t in settings.MANDATORY_HASHTAGS.split(",")
            if t.strip()
        ]
        combined = mandatory + [t for t in tags if t not in mandatory]
        tags = combined[:7]

        return {
            "title": final_title,
            "description": data.description.strip(),
            "hashtags": tags,
            "story_type": data.story_type,
            "cta": data.cta
        }

    except Exception as e:
        logger.error(f"Structured metadata generation failed: {e}")
        fallback_t = _clean_title(title or hook or (transcript.strip().split('\n')[0][:50]))
        fallback_d = (transcript.strip()[:180] + '...').strip()
        return {
            "title": fallback_t,
            "description": fallback_d,
            "hashtags": ["#Shorts", "#Viral", "#Trending"],
            "story_type": "reaction",
            "cta": "Follow for more."
        }
