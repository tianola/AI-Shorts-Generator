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
    title: str = Field(..., description="Under 55 chars, names one concrete element of this clip; no hashtags, no emojis, no generic hype")
    description: str = Field(..., description="2-3 sentences of real context for this clip, then one call to action")
    hashtags: List[str] = Field(default_factory=lambda: ["#Shorts"], description="3-5 niche-specific tags starting with #Shorts; never #Viral/#Trending/#FYP")
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
    return t


_PLACEHOLDER_RE = re.compile(r"^\s*viral\s*moment\b", re.IGNORECASE)


def is_placeholder_title(title: str) -> bool:
    """True for a title that says nothing about the clip.

    `clip_selector` used to answer an LLM outage with `VIRAL MOMENT {n}`, and
    those strings went out to the channel as real titles (35 clip rows across
    jobs 80/81/83). A publish path must be able to recognise that shape.
    """
    t = (title or "").strip()
    return not t or bool(_PLACEHOLDER_RE.match(t))


_LEAD_FILLER = {
    "so", "and", "but", "or", "well", "now", "okay", "ok", "yeah", "yes",
    "um", "uh", "like", "i", "it's", "its", "you", "your", "see", "then",
    "actually", "basically", "literally", "right", "here", "there",
}


def derive_title_from_text(text: str, max_words: int = 7) -> str:
    """Title-case the first real words of a transcript window, no placeholder.

    Used whenever the LLM is unavailable: the clip still has to be describable,
    and the spoken opening is the only honest source left.
    """
    words = re.findall(r"[^\s]+", (text or "").strip())
    words = [w.strip('",.;:!?()[]{}') for w in words]
    words = [w for w in words if w]
    while words and words[0].lower() in _LEAD_FILLER:
        words = words[1:]
    if not words:
        return ""
    picked = words[:max_words]
    title = " ".join(picked).strip()
    title = title[0].upper() + title[1:] if title else title
    return _clean_title(title)


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
        "You are a top-tier YouTube Shorts strategist. You write titles that name the "
        "specific thing the clip actually says or shows - a number, a mechanism, a named "
        "object, a real consequence. You never write a title that could sit on any video, "
        "and you never promise a reveal the clip does not deliver."
    )

    user_prompt = f"""
    Clip Transcript:
    \"\"\"{transcript.strip()}\"\"\"

    Context:
    - Current Title draft: {title or 'None'}
    - Key Hook: {hook or 'None'}

    Requirements:
    1. Title: under 55 characters, front-loaded, and it must contain one concrete
       element taken from the transcript (a number, a named thing, a mechanism, a
       result). Bad: 'Wait For The Reaction', 'You Will Not Believe This',
       'The Secret Nobody Knew' - those describe nothing. Good: 'Rereading Fails
       After 3 Pages', 'One Recall Beats Ten Rereads'.
    2. Do NOT put hashtags or emojis in the title.
    3. Description: 2-3 sentences that state the context of THIS clip, then one
       plain call to action.
    4. Hashtags: 3-5 tags, starting with #Shorts, all specific to the clip's niche.
       Never #Viral, #Trending, #FYP, #ForYou, #Explore.
    """

    try:
        data: ClipMetadata = await call_llm_structured(
            response_model=ClipMetadata,
            prompt=user_prompt,
            system=system_prompt,
            model=settings.OMNIROUTE_MODEL if settings.AI_PROVIDER == "omniroute" else settings.METADATA_OLLAMA_MODEL,
            temperature=0.3,
        )

        final_title = _clean_title(data.title) or derive_title_from_text(hook or transcript)
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
        fallback_t = _clean_title(title or hook or "") or derive_title_from_text(transcript)
        fallback_d = (transcript.strip()[:180] + '...').strip()
        return {
            "title": fallback_t,
            "description": fallback_d,
            "hashtags": ["#Shorts"],
            "story_type": "reaction",
            "cta": "Follow for more."
        }
