"""Helpers shared by more than one API router."""
from typing import Optional

from fastapi import HTTPException

from config import settings

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
