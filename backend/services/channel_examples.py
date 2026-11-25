"""What this channel's audience actually watched, as examples for clip selection.

services.youtube_stats writes YouTube's view counts back onto published clips.
This module turns the best and worst of them into a short block of text that the
candidate and judge prompts can read, so the model calibrates "would a stranger
keep watching" against this audience's real behaviour instead of only its own
general taste.

The block is empty until enough clips have real numbers: a ranking of two clips
says more about luck than about the audience, and an empty block leaves the
prompts exactly as they were.
"""
from typing import List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip

# Below this many clips with synced views the examples are noise, not signal.
MIN_SYNCED_CLIPS = 6
_TITLE_CHARS = 80
_HOOK_CHARS = 160


def _clip_text(value: Optional[str], limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text.replace('"', "'")


def _line(clip: Clip) -> str:
    parts = [f"{clip.view_count:,} views", f'title "{_clip_text(clip.title, _TITLE_CHARS)}"']
    hook = _clip_text(clip.hook, _HOOK_CHARS)
    if hook:
        parts.append(f'opening line "{hook}"')
    return "- " + ", ".join(parts)


def channel_examples(db: Session, *, per_side: Optional[int] = None,
                     exclude_job_id: Optional[int] = None) -> str:
    """The `per_side` most and least watched published clips, as prompt text.

    Returns "" when the feature is off (CHANNEL_EXAMPLES=0) or when fewer than
    MIN_SYNCED_CLIPS clips have views on record. The best and worst lists never
    share a clip, so with few clips each side is shorter rather than repeated.
    """
    per_side = settings.CHANNEL_EXAMPLES if per_side is None else per_side
    if per_side <= 0:
        return ""
    query = db.query(Clip).filter(Clip.view_count.isnot(None), Clip.title.isnot(None))
    if exclude_job_id is not None:
        query = query.filter(Clip.job_id != exclude_job_id)
    clips: List[Clip] = [c for c in query.all() if (c.title or "").strip()]
    if len(clips) < MIN_SYNCED_CLIPS:
        return ""

    # Newest first among equal views keeps the output stable and slightly
    # favours what the audience does now.
    ranked = sorted(clips, key=lambda c: (-c.view_count, -(c.id or 0)))
    take = min(per_side, len(ranked) // 2)
    best = ranked[:take]
    worst = ranked[-take:]

    return (
        "How earlier shorts from this same channel did with its audience (real YouTube "
        "view counts). Use them to calibrate what this audience stops scrolling for: the "
        "kind of opening, stakes and title that worked or failed. They are about other "
        "videos, so do not copy their topics or wording.\n"
        "Watched the most:\n" + "\n".join(_line(c) for c in best) + "\n"
        "Watched the least:\n" + "\n".join(_line(c) for c in worst)
    )


def channel_examples_safe(exclude_job_id: Optional[int] = None) -> str:
    """channel_examples with its own session; any database trouble gives ""."""
    from database.session import SessionLocal

    db = SessionLocal()
    try:
        return channel_examples(db, exclude_job_id=exclude_job_id)
    except Exception as exc:  # the examples are a hint; selection must not fail on them
        logger.warning(f"Channel examples unavailable ({type(exc).__name__}: {exc})")
        return ""
    finally:
        db.close()
