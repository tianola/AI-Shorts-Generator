"""TikTok connect and upload endpoints.

main.py mounts this router under /api, so the URLs a caller sees did not change.
"""
import json
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip, Job
from database.session import get_database

router = APIRouter()

# state -> redirect_uri for the consent URLs we handed out. In-process on
# purpose: this is a single-user local app and the state is only ever consumed
# by the browser that asked for it.
_TIKTOK_OAUTH_STATES: dict = {}

# ===========================================================================
# TIKTOK DIRECT PUBLISHING & BATCH UPLOAD ENDPOINTS
# ===========================================================================

@router.get("/tiktok/status")
async def get_tiktok_status():
    """Get current TikTok authentication and channel connection status."""
    from services.tiktok_upload import get_tiktok_auth_status
    return get_tiktok_auth_status()


class TikTokConnectRequest(BaseModel):
    username: str
    session_id: str
    auth_type: Optional[str] = "session_id"


@router.post("/tiktok/connect")
async def connect_tiktok_endpoint(req: TikTokConnectRequest):
    """Save TikTok session/token credentials."""
    from services.tiktok_upload import save_tiktok_credentials
    try:
        res = save_tiktok_credentials(
            username=req.username,
            session_id=req.session_id,
            auth_type=req.auth_type or "session_id"
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect TikTok: {str(e)}")


@router.post("/tiktok/disconnect")
async def disconnect_tiktok_endpoint():
    """Disconnect and remove stored TikTok credentials."""
    from services.tiktok_upload import disconnect_tiktok_account
    success = disconnect_tiktok_account()
    return {"success": success, "authenticated": False}


_TIKTOK_PRIVACY = {"public": "PUBLIC_TO_EVERYONE",
                   "unlisted": "FOLLOWER_OF_CREATOR",
                   "private": "SELF_ONLY"}


def _resolve_tiktok_privacy(value: Optional[str]) -> str:
    """Map a YouTube-style visibility to its TikTok equivalent."""
    from services.tiktok_upload import tiktok_privacy_for
    normalized = (value or settings.AUTO_UPLOAD_PRIVACY).lower().strip()
    if normalized not in _TIKTOK_PRIVACY:
        raise HTTPException(status_code=400, detail=f"Invalid privacy_level '{value}'")
    return tiktok_privacy_for(normalized)


@router.post("/tiktok/auth-url")
async def generate_tiktok_auth_url(redirect_uri: Optional[str] = None):
    """The TikTok consent URL to open once, which returns to the callback below.

    TikTok insists on PKCE, so the verifier is minted here and kept beside the
    state until the callback spends it.
    """
    from services import tiktok_content_api as api
    try:
        state = api.new_state()
        verifier = api.new_code_verifier()
        _TIKTOK_OAUTH_STATES[state] = {
            "redirect_uri": redirect_uri or settings.TIKTOK_REDIRECT_URI,
            "code_verifier": verifier,
        }
        return {"auth_url": api.authorization_url(state=state, target_redirect_uri=redirect_uri,
                                                  code_challenge=api.pkce_challenge(verifier)),
                "state": state,
                "redirect_uri": redirect_uri or settings.TIKTOK_REDIRECT_URI}
    except api.TikTokAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/tiktok/oauth2callback", response_class=HTMLResponse)
async def tiktok_oauth2_callback(code: Optional[str] = None, state: Optional[str] = None,
                                 error: Optional[str] = None, error_description: Optional[str] = None):
    """TikTok lands here with ?code=; exchange it and store the token."""
    from services import tiktok_content_api as api

    if error:
        return HTMLResponse(_tiktok_page("TikTok connection failed",
                                         f"<p class='bad'>{_escape(error_description or error)}</p>"), status_code=400)
    if not code:
        return HTMLResponse(_tiktok_page("TikTok connection failed",
                                         "<p class='bad'>No authorization code was returned.</p>"), status_code=400)
    pending = _TIKTOK_OAUTH_STATES.pop(state, None)
    if state and pending is None:
        # The verifier only ever lives in this dict: a restart between opening
        # the consent screen and landing here makes the exchange impossible.
        return HTMLResponse(_tiktok_page(
            "TikTok connection failed",
            "<p class='bad'>This sign-in attempt is no longer known to the server "
            "(it was used once already, or the app restarted since). Open Connect "
            "again to get a fresh link.</p>"), status_code=400)
    pending = pending or {}
    redirect = pending.get("redirect_uri") or settings.TIKTOK_REDIRECT_URI
    try:
        token = api.exchange_code(code, redirect, code_verifier=pending.get("code_verifier"))
    except Exception as e:
        return HTMLResponse(_tiktok_page("TikTok connection failed",
                                         f"<p class='bad'>{_escape(str(e))}</p>"), status_code=500)
    username = token.get("username") or "your account"
    return HTMLResponse(_tiktok_page(
        "TikTok connected",
        f"<p class='ok'>Signed in as {_escape(username)}. Clips will be sent as "
        f"<b>{_escape(settings.TIKTOK_POST_MODE)}</b> — change TIKTOK_POST_MODE to "
        f"DIRECT_PUBLISH once you trust the output.</p>"))


class TikTokClientSecretsRequest(BaseModel):
    client_key: str
    client_secret: str


@router.post("/tiktok/client-credentials")
async def save_tiktok_client_credentials(req: TikTokClientSecretsRequest):
    """Store the app's client key/secret so the OAuth flow can start."""
    from services import tiktok_content_api as api
    try:
        api.save_client_credentials(req.client_key, req.client_secret)
        return {"success": True, "client_configured": True}
    except api.TikTokAPIError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _escape(value: str) -> str:
    import html
    return html.escape(str(value or ""))


def _tiktok_page(heading: str, body_html: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_escape(heading)}</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:grid;place-items:center;height:100vh;margin:0}}
.card{{max-width:34rem;padding:2rem;border:1px solid #333;border-radius:12px;background:#181818;text-align:center}}
.ok{{color:#4ade80}} .bad{{color:#f87171}}</style></head>
<body><div class="card"><h2>{_escape(heading)}</h2>{body_html}</div></body></html>"""


@router.post("/clips/{clip_id}/upload-tiktok")
async def upload_clip_to_tiktok_endpoint(
    clip_id: int,
    privacy_level: Optional[str] = None,
    db: Session = Depends(get_database)
):
    """Publish a single clip directly to TikTok."""
    # Kept local: the TikTok client is optional and must not break importing the API.
    from services.tiktok_upload import upload_clip_to_tiktok

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")

    job = db.query(Job).filter(Job.id == clip.job_id).first() if clip.job_id else None

    source_title = None
    if job:
        job_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(job.id)
        meta_file = job_dir / "meta.json"
        if meta_file.exists():
            try:
                with open(meta_file, "r", encoding="utf-8") as mf:
                    mdata = json.load(mf)
                    source_title = mdata.get("title")
            except Exception:
                pass

    # Resolve video path
    video_path = None
    if clip.file_path:
        cand = Path(settings.STORAGE_ROOT) / clip.file_path
        if cand.exists():
            video_path = str(cand.resolve())
        elif os.path.exists(clip.file_path):
            video_path = str(Path(clip.file_path).resolve())

    if not video_path and job:
        clip_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(job.id) / f"clip_{clip.index}"
        for cand_name in ["final_video.mp4", "final_short.mp4", "short.mp4"]:
            cand = clip_dir / cand_name
            if cand.exists():
                video_path = str(cand.resolve())
                break

    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail=f"Rendered video file not found for clip #{clip.index}")

    title = clip.title or f"Viral Moment #{clip.index + 1}"
    description = clip.description or "Watch this viral moment!"
    hashtags = clip.hashtags or ["#TikTok", "#Viral", "#FYP"]

    try:
        res = upload_clip_to_tiktok(
            video_path=video_path,
            title=title,
            description=description,
            hashtags=hashtags,
            source_title=source_title,
            privacy_level=_resolve_tiktok_privacy(privacy_level)
        )
        if res.get("video_id") or res.get("tiktok_url"):
            clip.tiktok_video_id = res.get("video_id") or res.get("tiktok_url")
            db.commit()
        return res
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TikTok upload failed: {str(e)}")


@router.post("/jobs/{job_id}/upload-all-tiktok")
async def upload_all_clips_to_tiktok_endpoint(
    job_id: int,
    privacy_level: Optional[str] = None,
    db: Session = Depends(get_database)
):
    """Batch upload all clips from a job to TikTok."""
    from services.tiktok_upload import upload_all_clips_to_tiktok_for_job
    resolved = _resolve_tiktok_privacy(privacy_level)
    try:
        res = upload_all_clips_to_tiktok_for_job(job_id, db, privacy_level=resolved)
        return res
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"TikTok batch upload failed: {str(e)}")
