"""
Platform-Specific Copy Generator.
Generates tailored titles, descriptions and hashtags for TikTok, Instagram Reels and YouTube Shorts.
"""
from typing import Any, Dict

from loguru import logger
from pydantic import BaseModel, Field, model_validator

from services.llm_router import call_llm_structured
from services.metadata import derive_title_from_text

_TITLE_KEYS = ("title", "title_options", "titles", "hook", "name")
_DESC_KEYS = ("description", "caption", "text", "body")
_TAG_KEYS = ("hashtags", "tags", "hashtag")


def _first_string(value: Any) -> str:
    """Pull one usable string out of a field the model may answer any shape for."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, dict):
                inner = _first_string(item.get("title") or item.get("text") or item.get("value"))
                if inner:
                    return inner
    if isinstance(value, dict):
        for key in _TITLE_KEYS + _DESC_KEYS:
            if value.get(key):
                inner = _first_string(value[key])
                if inner:
                    return inner
    return ""


def _tag_list(value: Any) -> list:
    if isinstance(value, str):
        raw = [t for t in value.replace(",", " ").split() if t]
    elif isinstance(value, list):
        raw = [str(t).strip() for t in value if str(t).strip()]
    else:
        raw = []
    tags = []
    for tag in raw:
        clean = tag.lstrip("#").strip()
        if clean and f"#{clean}" not in tags:
            tags.append(f"#{clean}")
    return tags


class PlatformCopy(BaseModel):
    """One platform's copy.

    Every field has a default because the strict version threw away the whole
    answer - specific titles, descriptions and niche tags included - whenever the
    model answered `title_options` instead of `title`, or skipped a platform.
    """
    title: str = Field(default="", description="Optimized title for the platform")
    description: str = Field(default="", description="Engaging caption/description")
    hashtags: list = Field(default_factory=list, description="Targeted hashtags, # included")

    @model_validator(mode="before")
    @classmethod
    def _accept_loose_shapes(cls, data: Any) -> Any:
        if data is None:
            return {}
        if isinstance(data, str):
            return {"title": data}
        if not isinstance(data, dict):
            return {}
        out: Dict[str, Any] = {}
        for key in _TITLE_KEYS:
            if key in data:
                picked = _first_string(data[key])
                if picked:
                    out["title"] = picked
                    break
        for key in _DESC_KEYS:
            if key in data:
                picked = _first_string(data[key])
                if picked:
                    out["description"] = picked
                    break
        for key in _TAG_KEYS:
            if key in data:
                tags = _tag_list(data[key])
                if tags:
                    out["hashtags"] = tags
                    break
        return out


class MultiPlatformCopy(BaseModel):
    tiktok: PlatformCopy = Field(default_factory=PlatformCopy)
    instagram: PlatformCopy = Field(default_factory=PlatformCopy)
    youtube: PlatformCopy = Field(default_factory=PlatformCopy)

    @model_validator(mode="before")
    @classmethod
    def _fill_missing_platforms(cls, data: Any) -> Any:
        """A platform the model omitted inherits from the ones it did answer.

        Job 105 answered tiktok + youtube only, and the missing `instagram` key
        was enough to discard all of it.
        """
        if data is None:
            return {}
        if not isinstance(data, dict):
            return {}
        known = ("youtube", "tiktok", "instagram")
        present = {k: data[k] for k in known if isinstance(data.get(k), (dict, str))}
        if not present:
            # Some models wrap the answer one level deeper.
            for value in data.values():
                if isinstance(value, dict) and any(k in value for k in known):
                    present = {k: value[k] for k in known if isinstance(value.get(k), (dict, str))}
                    break
        donor = present.get("youtube") or present.get("tiktok") or present.get("instagram") or {}
        return {k: present.get(k, donor) for k in known}


async def generate_platform_copies(transcript: str, base_title: str) -> Dict[str, Any]:
    """
    Generate platform-specific copies for TikTok, IG Reels, and YT Shorts using LLM.
    """
    system_prompt = (
        "You are an elite social media growth expert. Take a video clip transcript and a base title "
        "and write copy that names what actually happens: the specific idea, object, number or person "
        "in the clip. Never write generic hype ('you won't believe', 'watch this amazing moment'), "
        "and never reuse a tag that could sit on any video."
    )

    user_prompt = f"""
    Base Title: {base_title}
    Transcript: {transcript[:1000]}

    Give tailored copy for each of the three platforms (TikTok, Instagram Reels, YouTube Shorts).
    Each platform object needs:
    - title: under 40 characters, front-loaded, states the specific claim or moment of this clip.
    - description: 1-2 sentences of real context, then one call to action.
    - hashtags: 3-5 tags, # included, starting with the niche the clip is actually in.
      Do not use #viral, #trending, #foryou, #fyp or #explore.
    """

    fallback_description = (transcript.strip()[:180] + "...").strip()
    try:
        result: MultiPlatformCopy = await call_llm_structured(
            response_model=MultiPlatformCopy,
            prompt=user_prompt,
            system=system_prompt,
            temperature=0.5
        )
        copies = result.model_dump()
    except Exception as e:
        logger.warning(f"Platform copy generation failed ({e}), deriving copy from the transcript")
        copies = {"tiktok": {}, "instagram": {}, "youtube": {}}

    for platform, copy in copies.items():
        if not copy.get("title"):
            copy["title"] = base_title or derive_title_from_text(transcript)
        if not copy.get("description"):
            copy["description"] = fallback_description
        if not copy.get("hashtags"):
            copy["hashtags"] = ["#Shorts"] if platform == "youtube" else []

    return copies
