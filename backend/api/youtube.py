"""YouTube OAuth and upload endpoints.

main.py mounts this router under /api, so the URLs a caller sees did not change -
only the file they live in. The HTML escapers moved here with the OAuth callback,
which is the only thing that renders inline HTML.
"""
import json
import os
import html as html_escape_mod
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.common import resolve_upload_privacy
from config import settings
from database.models import Clip
from database.session import get_database

router = APIRouter()

def esc_html(value: object) -> str:
    """Escape a value before splicing it into one of the inline HTML responses.

    Google controls the ``error`` query parameter and the channel title, so
    these must not reach the browser as markup.
    """
    return html_escape_mod.escape("" if value is None else str(value), quote=True)


def esc_js(value: object) -> str:
    """Encode a value as a JS string literal safe inside an inline <script>.

    json.dumps keeps '<' verbatim, so a '</script>' inside the data would close
    the block early; those get written as \\u003c / \\u003e instead.
    """
    return json.dumps("" if value is None else str(value)).replace("<", "\\u003c").replace(">", "\\u003e")


# ==========================================
# YouTube OAuth & Real Upload Endpoints
# ==========================================

class YouTubeUploadRequest(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    hashtags: Optional[List[str]] = None
    privacy_status: Optional[str] = "public"


class YouTubeCodeRequest(BaseModel):
    code: str
    redirect_uri: Optional[str] = None


@router.get("/youtube/status")
async def get_youtube_status():
    """Check if YouTube API credentials and active channel connection exist."""
    from services.youtube_upload import get_youtube_auth_status
    return get_youtube_auth_status()


@router.post("/youtube/auth-url")
async def generate_youtube_auth_url(redirect_uri: Optional[str] = None):
    """Generate the OAuth authorization URL for the user."""
    from services.youtube_upload import get_auth_url, DEFAULT_REDIRECT_URI
    target_redirect = redirect_uri or DEFAULT_REDIRECT_URI
    try:
        url = get_auth_url(redirect_uri=target_redirect)
        if not url:
            raise ValueError("client_secrets.json not found or invalid OAuth configuration.")
        return {"auth_url": url, "redirect_uri": target_redirect}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/youtube/oauth2callback", response_class=HTMLResponse)
async def youtube_oauth2_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None
):
    """Callback endpoint for Google OAuth loopback flow."""
    from services.youtube_upload import exchange_code_for_token, DEFAULT_REDIRECT_URI
    if error:
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html><head><title>Authentication Cancelled</title>
        <style>body{{font-family:system-ui,-apple-system,sans-serif;background:#0b0f19;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;}}
        .card{{background:#131b2e;border:1px solid #ef4444;padding:2rem;border-radius:16px;text-align:center;max-width:440px;}}</style>
        </head><body><div class="card"><h2>❌ Access Denied</h2><p style="color:#94a3b8;">Google returned an error: {esc_html(error)}</p>
        <button onclick="window.close()" style="padding:8px 16px;border-radius:8px;border:none;background:#ef4444;color:#fff;cursor:pointer;">Close</button></div></body></html>
        """, status_code=400)

    if not code:
        return HTMLResponse(content="<h3>Missing authorization code from Google</h3>", status_code=400)

    try:
        status_info = exchange_code_for_token(code.strip(), state=state, redirect_uri=DEFAULT_REDIRECT_URI)
        channel_title = status_info.get("channel_name") or "YouTube Channel"
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html>
        <head>
          <title>YouTube Connected</title>
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0b0f19; color: #fff; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
            .card {{ background: #131b2e; border: 1px solid #22c55e; padding: 2.5rem; border-radius: 16px; text-align: center; max-width: 460px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
            h1 {{ color: #22c55e; margin: 0 0 0.5rem 0; font-size: 1.5rem; }}
            p {{ color: #94a3b8; font-size: 0.95rem; line-height: 1.5; margin-bottom: 1.5rem; }}
            .btn {{ background: #22c55e; color: #000; font-weight: 700; padding: 0.75rem 1.75rem; border-radius: 8px; text-decoration: none; display: inline-block; cursor: pointer; border: none; font-size: 0.9rem; }}
          </style>
        </head>
        <body>
          <div class="card">
            <div style="font-size: 3.5rem; margin-bottom: 0.8rem;">🎉</div>
            <h1>YouTube Connected!</h1>
            <p>Your YouTube channel <strong>{esc_html(channel_title)}</strong> is now successfully linked to AI Shorts Generator.</p>
            <button class="btn" onclick="window.close()">Close Window</button>
          </div>
          <script>
            try {{
              if (window.opener) {{
                window.opener.postMessage({{ type: 'YOUTUBE_AUTH_SUCCESS', channel: {esc_js(channel_title)} }}, window.location.origin);
              }}
            }} catch(e) {{}}
            setTimeout(() => {{ window.close(); }}, 2500);
          </script>
        </body>
        </html>
        """)
    except Exception as e:
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html><head><title>Auth Failed</title>
        <style>body{{font-family:sans-serif;background:#0b0f19;color:#fff;display:flex;align-items:center;justify-content:center;height:100vh;}}
        .card{{background:#131b2e;border:1px solid #ef4444;padding:2rem;border-radius:12px;text-align:center;max-width:440px;}}</style>
        </head><body><div class="card"><h2>❌ Authorization Failed</h2><p style="color:#ef4444;">{esc_html(str(e))}</p>
        <button onclick="window.close()" style="padding:8px 16px;border-radius:6px;border:none;background:#334155;color:#fff;cursor:pointer;">Close</button></div></body></html>
        """, status_code=500)


@router.post("/youtube/authorize-code")
async def submit_youtube_auth_code(req: YouTubeCodeRequest):
    """Exchange authorization code for valid Google OAuth tokens."""
    from services.youtube_upload import exchange_code_for_token, DEFAULT_REDIRECT_URI
    try:
        redirect = req.redirect_uri or DEFAULT_REDIRECT_URI
        status_info = exchange_code_for_token(req.code.strip(), redirect_uri=redirect)
        # channel_name is repeated at the top level because the client read it
        # from the response root, where it never was.
        return {"success": True, "channel_name": status_info.get("channel_name"), "status": status_info}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to authorize code: {str(e)}")


@router.post("/youtube/client-secrets")
async def upload_client_secrets(file: UploadFile = File(...)):
    """Upload client_secrets.json from Google Cloud Console."""
    from services.youtube_upload import save_client_secrets_json
    try:
        content = await file.read()
        text_content = content.decode("utf-8")
        save_client_secrets_json(text_content)
        from services.youtube_upload import get_youtube_auth_status
        return {"success": True, "status": get_youtube_auth_status()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid client_secrets.json: {str(e)}")


@router.post("/clips/{clip_id}/upload-youtube")
async def upload_clip_to_youtube_endpoint(
    clip_id: int,
    req: YouTubeUploadRequest = YouTubeUploadRequest(),
    db: Session = Depends(get_database)
):
    """Upload a generated clip directly to YouTube Shorts."""
    from services.youtube_upload import upload_clip_to_youtube, get_youtube_auth_status

    auth_status = get_youtube_auth_status()
    if not auth_status["authenticated"]:
        raise HTTPException(
            status_code=401,
            detail="YouTube account is not connected. Please connect your YouTube account in settings first."
        )

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    # Locate the video file on disk
    video_path = None
    if clip.file_path:
        cand = Path(settings.STORAGE_ROOT) / clip.file_path
        if cand.exists():
            video_path = str(cand.resolve())
        elif os.path.exists(clip.file_path):
            video_path = str(Path(clip.file_path).resolve())

    if not video_path:
        job_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(clip.job_id) / f"clip_{clip.index}"
        for cand_name in ["final_video.mp4", "final_short.mp4", "short.mp4"]:
            cand = job_dir / cand_name
            if cand.exists():
                video_path = str(cand.resolve())
                break

    if not video_path or not os.path.exists(video_path):
        raise HTTPException(status_code=404, detail=f"Video file for clip {clip_id} not found on disk")

    # Prepare metadata
    title = req.title or clip.title or "Viral Moment"
    description = req.description or clip.description or "Watch this amazing clip!"
    hashtags = req.hashtags if req.hashtags is not None else (clip.hashtags or ["#Shorts", "#Viral"])
    privacy = resolve_upload_privacy(req.privacy_status)

    try:
        result = upload_clip_to_youtube(
            video_path=video_path,
            title=title,
            description=description,
            hashtags=hashtags,
            privacy_status=privacy,
            job_id=clip.job_id if clip.job_id else 0
        )

        clip.uploaded = True
        clip.youtube_video_id = result["video_id"]
        clip.published_at = datetime.utcnow()
        clip.upload_error = None
        db.commit()
        db.refresh(clip)

        return {
            "success": True,
            "video_id": result["video_id"],
            "shorts_url": result["shorts_url"],
            "watch_url": result["watch_url"],
            "title": result["title"],
            "privacy_status": result["privacy_status"],
            "clip_id": clip.id
        }
    except Exception as e:
        clip.uploaded = False
        clip.upload_error = str(e)
        clip.upload_retry_count += 1
        db.commit()
        raise HTTPException(status_code=500, detail=f"YouTube upload failed: {str(e)}")


class BatchYouTubeUploadRequest(BaseModel):
    privacy_status: Optional[str] = None
    category_id: Optional[str] = "24"


@router.post("/jobs/{job_id}/upload-all-youtube")
async def upload_all_clips_to_youtube_endpoint(
    job_id: int,
    req: BatchYouTubeUploadRequest = BatchYouTubeUploadRequest(),
    db: Session = Depends(get_database)
):
    """Batch upload all clips from a job directly to YouTube Shorts."""
    from services.youtube_upload import upload_all_clips_for_job, get_youtube_auth_status

    auth_status = get_youtube_auth_status()
    if not auth_status["authenticated"]:
        raise HTTPException(
            status_code=401,
            detail="YouTube account is not connected. Please connect your YouTube account first."
        )

    try:
        result = upload_all_clips_for_job(
            job_id=job_id,
            db=db,
            privacy_status=resolve_upload_privacy(req.privacy_status),
            category_id=req.category_id or "24"
        )
        return result
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except PermissionError as pe:
        raise HTTPException(status_code=401, detail=str(pe))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch upload failed: {str(e)}")
