"""Helpers shared by more than one API router."""
from typing import Optional

from fastapi import HTTPException

from config import settings
from database.models import Clip


def resolve_upload_privacy(value: Optional[str]) -> str:
    """Validate the per-job upload visibility, falling back to the configured default."""
    allowed = {"public", "unlisted", "private"}
    if value is None:
        return settings.AUTO_UPLOAD_PRIVACY
    normalized = value.lower().strip()
    if normalized not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid upload_privacy '{value}'. Allowed: {', '.join(sorted(allowed))}"
        )
    return normalized


def clip_payload(clip: Clip) -> dict:
    """Single wire shape for a clip, used by every endpoint that returns one.

    Three hand-copied variants used to drift (platform_metadata, tiktok_video_id
    and the score breakdown were missing from some of them), so the frontend
    received different fields depending on which endpoint it called.
    """
    return {
        "id": clip.id,
        "job_id": clip.job_id,
        "index": clip.index,
        "start": clip.start,
        "end": clip.end,
        "title": clip.title,
        "hook": clip.hook,
        "score": clip.score,
        "hook_score": clip.hook_score,
        "coherence_score": clip.coherence_score,
        "connection_score": clip.connection_score,
        "trend_score": clip.trend_score,
        "score_breakdown": clip.score_breakdown,
        "caption_style": clip.caption_style or "mrbeast",
        "layout_mode": clip.layout_mode or "single_speaker",
        "virality_explanation": clip.virality_explanation,
        "trim_start": clip.trim_start,
        "trim_end": clip.trim_end,
        "translated_transcript": clip.translated_transcript,
        "file_path": clip.file_path,
        "thumbnail_path": clip.thumbnail_path,
        "description": clip.description,
        "hashtags": clip.hashtags,
        "platform_metadata": clip.platform_metadata,
        "uploaded": clip.uploaded,
        "youtube_video_id": clip.youtube_video_id,
        "tiktok_video_id": clip.tiktok_video_id,
        "upload_error": clip.upload_error,
        "upload_retry_count": clip.upload_retry_count,
        "published_at": clip.published_at,
        "hook_style": clip.hook_style,
        "view_count": clip.view_count,
        "like_count": clip.like_count,
        "comment_count": clip.comment_count,
        "stats_synced_at": clip.stats_synced_at,
        "created_at": clip.created_at,
    }
