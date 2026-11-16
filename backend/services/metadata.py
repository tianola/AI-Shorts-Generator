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
    title: str = Field(..., description="Under 45 chars; teases the most surprising concrete thing in this clip without giving the answer away; true to the clip; no hashtags, no emojis")
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
    hook: str = "",
    keep_title: bool = False,
) -> dict:
    """
    Generate clean, viral metadata for a clip using AI.

    Args:
        transcript: The clip's transcript text
        title: Optional existing title
        hook: Optional existing hook
        keep_title: The given title is final (the clip selector wrote it while
            reading the whole video and it survived the ranking pass). Only the
            description and hashtags are generated; a second model rewriting the
            headline from the clip text alone is how a teasing title turned back
            into a plain summary.

    Returns:
        Dictionary with title, description, and hashtags
    """
    fixed_title = ""
    if keep_title and not is_placeholder_title(title):
        fixed_title = _clean_title(title)

    system_prompt = (
        "You are a top-tier YouTube Shorts strategist. A title has one job: make a "
        "stranger in the feed need to watch. You build it on the specific thing the clip "
        "actually says or shows - a number, a name, a consequence - and you tease it "
        "instead of summarising it. You never write a title that could sit on any video, "
        "and you never promise a reveal the clip does not deliver."
    )

    title_requirement = (
        f"""1. Title: it is already decided - return exactly: {fixed_title}"""
        if fixed_title else
        """1. Title: under 45 characters and at most 7 words, in the language spoken in the
       transcript. Take the most surprising concrete element of the transcript (a
       number, a named thing, a consequence) and open a curiosity gap around it: say
       enough to intrigue, hold back the answer. It must be true to the clip.
       Describes (bad): 'He Talks About His First Business Failing'.
       Generic (bad): 'Wait For The Reaction', 'You Will Not Believe This'.
       Teases (good): 'He Lost 40,000 In One Weekend', 'The One Question That Got Him Fired'."""
    )

    user_prompt = f"""
    Clip Transcript:
    \"\"\"{transcript.strip()}\"\"\"

    Context:
    - Current Title draft: {title or 'None'}
    - Key Hook: {hook or 'None'}

    Requirements:
    {title_requirement}
    2. Do NOT put hashtags or emojis in the title.
    3. Description: 2-3 sentences that state the context of THIS clip, then one
       plain call to action. Same language as the transcript.
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

        final_title = fixed_title or _clean_title(data.title) or derive_title_from_text(hook or transcript)
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
