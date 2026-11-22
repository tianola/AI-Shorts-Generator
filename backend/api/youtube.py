"""YouTube OAuth and upload endpoints.

main.py mounts this router under /api, so the URLs a caller sees did not change -
only the file they live in. The HTML escapers moved here with the OAuth callback,
which is the only thing that renders inline HTML.
"""
import json
import html as html_escape_mod
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.common import resolve_upload_privacy
from database.models import Clip
from database.session import get_database

# `os`, `pathlib` and `settings` left with the upload handlers below: a publish
# request no longer walks the storage tree itself, it asks the queue's path to do it.

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


# The status asks Google for the channel name, which takes a while and used to block the
# event loop on every page load. It is a plain sync handler now (run in the threadpool), and a
# healthy answer is kept for a few minutes. The cache is keyed on the two credential files, so
# reconnecting the channel is seen at once.
_status_cache = {"key": None, "at": 0.0, "value": None}
STATUS_TTL_SECONDS = 300


@router.get("/youtube/status")
def get_youtube_status():
    """Check if YouTube API credentials and active channel connection exist."""
    import time
    from services.youtube_upload import get_client_secrets_path, get_token_path, get_youtube_auth_status

    def stamp(path):
        return path.stat().st_mtime if path.exists() else 0.0

    key = (stamp(get_client_secrets_path()), stamp(get_token_path()))
    fresh = time.monotonic() - _status_cache["at"] < STATUS_TTL_SECONDS
    if _status_cache["key"] == key and fresh and _status_cache["value"]:
        return _status_cache["value"]
    value = get_youtube_auth_status()
    if value.get("authenticated"):
        _status_cache.update({"key": key, "at": time.monotonic(), "value": value})
    else:
        _status_cache.update({"key": None, "at": 0.0, "value": None})
    return value


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
    """Publish one clip because a human clicked, under the queue's own rules.

    This handler used to call `upload_clip_to_youtube` directly, which skipped every
    piece of publish discipline the worker applies: no duplicate check (a double click
    uploaded the same clip twice), no channel gap, no rolling upload budget, no claim,
    no release slot, and a `publish_due_at` left behind a completed publish - clip 461
    is `uploaded=1` with a video id, `scheduled_publish_at IS NULL` and a due time still
    set, which is why it went public on click and why the row reads as two states at
    once. Its exception handler also charged a strike for any error, including
    `uploadLimitExceeded`, the one refusal the queue deliberately forgives (clip 371).

    The click is now a request the queue answers: the channel's gap and budget are
    checked first, the clip keeps a claim so nothing races it, and the upload carries
    its release slot. A refusal does not lose the request - the row is parked for the
    moment the channel allows it and the worker ships it then.
    """
    from services.youtube_upload import get_youtube_auth_status
    from worker import publish_clip_for_operator

    auth_status = get_youtube_auth_status()
    if not auth_status["authenticated"]:
        raise HTTPException(
            status_code=401,
            detail="YouTube account is not connected. Please connect your YouTube account in settings first."
        )

    clip = db.query(Clip).filter(Clip.id == clip_id).first()
    if not clip:
        raise HTTPException(status_code=404, detail=f"Clip {clip_id} not found")

    # The edits the panel sent are written before the publish is asked for, so the
    # row, the queue and YouTube all end up holding the copy that was clicked. They
    # used to reach only the upload, which left the row describing a different video
    # than the channel - and any later queue pass republished the old copy.
    changed = False
    if req.title is not None and req.title != clip.title:
        clip.title = req.title
        changed = True
    if req.description is not None and req.description != clip.description:
        clip.description = req.description
        changed = True
    if req.hashtags is not None and req.hashtags != clip.hashtags:
        clip.hashtags = req.hashtags
        changed = True
    if changed:
        db.commit()

    privacy = resolve_upload_privacy(req.privacy_status)
    outcome = publish_clip_for_operator(db, clip, now=datetime.utcnow(),
                                        privacy_status=privacy)
    state = outcome["status"]

    if state == "published":
        result = outcome["result"]
        db.refresh(clip)
        return {
            "success": True,
            "video_id": result.get("video_id"),
            "shorts_url": result.get("shorts_url"),
            "watch_url": result.get("watch_url"),
            "title": result.get("title"),
            "privacy_status": result.get("privacy_status"),
            "clip_id": clip.id,
            # What the operator needs to see now that a click no longer means
            # "public this second": the release is YouTube's, not this machine's.
            "scheduled_publish_at": clip.scheduled_publish_at,
        }

    if state == "already_published":
        video_id = outcome.get("video_id")
        where = f" as {video_id} (https://youtube.com/shorts/{video_id})" if video_id else ""
        raise HTTPException(
            status_code=409,
            detail=f"Clip {clip_id} is already published{where}. Refusing to upload a "
                   f"second copy of it to the channel."
        )

    if state == "unresolved_attempt":
        attempt = outcome["attempt_at"]
        raise HTTPException(
            status_code=409,
            detail=f"Clip {clip_id} has a publish attempt from "
                   f"{attempt:%Y-%m-%d %H:%M} UTC that never recorded a video id, so it may "
                   f"already be on YouTube. Check the channel (python -m "
                   f"scripts.reconcile_uploads --verify); only once you know it is not there "
                   f"should you clear publish_attempt_at on this row and click again."
        )

    if state == "held":
        return {
            "success": False,
            "queued": True,
            "clip_id": clip.id,
            "held_until": outcome["retry_at"],
            "message": f"Held by the publish queue: {outcome['reason']}",
            # `error` is what the Studio panel reads on a non-success response.
            "error": f"Not uploaded: {outcome['reason']}. The clip is queued and will "
                     f"publish itself at {outcome['retry_at']:%Y-%m-%d %H:%M} UTC.",
        }

    if state == "contended":
        raise HTTPException(
            status_code=409,
            detail=f"Another publish already owns clip {clip_id}; try again after its "
                   f"claim expires."
        )

    if outcome.get("code") == "missing_file":
        raise HTTPException(status_code=404,
                            detail=f"Video file for clip {clip_id} not found on disk")

    raise HTTPException(status_code=500,
                        detail=f"YouTube upload failed: {outcome.get('error')}")


class BatchYouTubeUploadRequest(BaseModel):
    privacy_status: Optional[str] = None
    category_id: Optional[str] = "24"


@router.post("/jobs/{job_id}/upload-all-youtube")
async def upload_all_clips_to_youtube_endpoint(
    job_id: int,
    req: BatchYouTubeUploadRequest = BatchYouTubeUploadRequest(),
    db: Session = Depends(get_database)
):
    """Queue a whole job for publishing; at most one clip leaves in this call.

    The batch button was the single-clip bypass with a loop around it -
    `upload_all_clips_for_job(..., pace=False)` on the reasoning that a human
    pressing "upload" asked for them now, which is how five clips of job 91 went out
    inside four seconds. Pressing it still means "publish this job", and the job now
    gets that: every clip takes a release slot and a queue row, and the channel's gap
    ships the first one immediately and the rest one per hour by itself.
    """
    from services.youtube_upload import get_youtube_auth_status
    from worker import publish_clips_of_job_for_operator

    auth_status = get_youtube_auth_status()
    if not auth_status["authenticated"]:
        raise HTTPException(
            status_code=401,
            detail="YouTube account is not connected. Please connect your YouTube account first."
        )

    try:
        return publish_clips_of_job_for_operator(
            db, job_id,
            now=datetime.utcnow(),
            privacy_status=resolve_upload_privacy(req.privacy_status),
            category_id=req.category_id or "24",
        )
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except PermissionError as pe:
        raise HTTPException(status_code=401, detail=str(pe))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch upload failed: {str(e)}")
