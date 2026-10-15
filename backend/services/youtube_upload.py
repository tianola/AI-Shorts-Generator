"""
YouTube Data API v3 Upload & OAuth Service for YouTube Shorts.
Handles token lifecycle, resumable video uploading, and shorts metadata formatting.
"""
import os
import json
from pathlib import Path
from typing import Dict, Any, List, Optional
from config import settings
from utils.logger import job_info, job_warning

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly"
]

def _get_secrets_dir() -> Path:
    secrets_dir = Path(settings.STORAGE_ROOT) / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    return secrets_dir

def get_client_secrets_path() -> Path:
    return _get_secrets_dir() / "client_secrets.json"

def get_token_path() -> Path:
    return _get_secrets_dir() / "youtube_token.json"


def get_youtube_auth_status() -> Dict[str, Any]:
    """Check if YouTube OAuth credentials and active token exist."""
    client_secrets = get_client_secrets_path()
    token_file = get_token_path()

    has_secrets = client_secrets.exists() and client_secrets.stat().st_size > 0
    is_authenticated = False
    channel_name = None

    if has_secrets and token_file.exists() and token_file.stat().st_size > 0:
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            from google.auth.transport.requests import Request

            with open(token_file, "r") as f:
                token_data = json.load(f)

            creds = Credentials.from_authorized_user_info(token_data, SCOPES)
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
                with open(token_file, "w") as f:
                    f.write(creds.to_json())

            if creds and creds.valid:
                is_authenticated = True
                # Fetch channel info
                youtube = build("youtube", "v3", credentials=creds)
                resp = youtube.channels().list(part="snippet", mine=True).execute()
                items = resp.get("items", [])
                if items:
                    channel_name = items[0]["snippet"].get("title")
        except Exception as e:
            job_warning(0, f"YouTube auth check warning: {e}")
            is_authenticated = False

    return {
        "has_client_secrets": has_secrets,
        "authenticated": is_authenticated,
        "channel_name": channel_name or ("Connected Channel" if is_authenticated else None),
        "client_secrets_path": str(client_secrets.resolve()),
        "token_path": str(token_file.resolve()),
    }


DEFAULT_REDIRECT_URI = "http://localhost:5001/api/youtube/oauth2callback"


_oauth_verifier_by_state: Dict[str, str] = {}
_last_oauth_verifier: Optional[str] = None


def get_auth_url(redirect_uri: str = DEFAULT_REDIRECT_URI) -> str:
    """Generate the Google OAuth consent URL."""
    global _last_oauth_verifier
    from google_auth_oauthlib.flow import Flow

    client_secrets = get_client_secrets_path()
    if not client_secrets.exists() or client_secrets.stat().st_size == 0:
        raise FileNotFoundError(
            f"client_secrets.json not found at {client_secrets}. "
            "Please download your OAuth client credentials from Google Cloud Console."
        )

    flow = Flow.from_client_secrets_file(
        str(client_secrets),
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    auth_url, state = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true"
    )
    if state and flow.code_verifier:
        _oauth_verifier_by_state[state] = flow.code_verifier
    if flow.code_verifier:
        _last_oauth_verifier = flow.code_verifier

    return auth_url


def exchange_code_for_token(code: str, state: Optional[str] = None, redirect_uri: str = DEFAULT_REDIRECT_URI) -> Dict[str, Any]:
    """Exchange authorization code for tokens and save to storage/secrets/youtube_token.json."""
    from google_auth_oauthlib.flow import Flow

    client_secrets = get_client_secrets_path()
    flow = Flow.from_client_secrets_file(
        str(client_secrets),
        scopes=SCOPES,
        redirect_uri=redirect_uri,
        state=state
    )

    verifier = None
    if state and state in _oauth_verifier_by_state:
        verifier = _oauth_verifier_by_state.pop(state)
    elif _last_oauth_verifier:
        verifier = _last_oauth_verifier

    if verifier:
        flow.code_verifier = verifier

    flow.fetch_token(code=code)
    creds = flow.credentials

    token_file = get_token_path()
    with open(token_file, "w") as f:
        f.write(creds.to_json())

    return get_youtube_auth_status()


def save_client_secrets_json(secrets_json_content: str) -> bool:
    """Save client_secrets.json directly from user upload or paste."""
    # Verify valid JSON
    data = json.loads(secrets_json_content)
    if "installed" not in data and "web" not in data:
        raise ValueError("Invalid client_secrets.json format. Expected 'installed' or 'web' root key.")

    client_secrets = get_client_secrets_path()
    with open(client_secrets, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return True


def format_shorts_metadata(
    title: str,
    description: str,
    hashtags: Optional[List[str]] = None,
    source_title: Optional[str] = None
) -> Dict[str, Any]:
    """
    Format metadata specifically optimized for YouTube Shorts algorithm:
      - Title capped to 100 chars, with #Shorts suffix if not present
      - Viral Hook & SEO description with structured tags and CTA
      - Comprehensive tag cloud
    """
    clean_title = (title or "Viral Moment").strip()
    if not clean_title.lower().endswith("#shorts") and "#shorts" not in clean_title.lower():
        if len(clean_title) <= 90:
            clean_title = f"{clean_title} #Shorts"
        else:
            clean_title = clean_title[:90].rsplit(" ", 1)[0] + " #Shorts"

    tags = []
    if hashtags:
        for h in hashtags:
            clean_h = h.strip().lstrip("#")
            if clean_h and clean_h.lower() not in [t.lower() for t in tags]:
                tags.append(clean_h)

    # Core viral tags for high Shorts reach
    for default_tag in ["Shorts", "YouTubeShorts", "Viral", "Trending", "FYP", "ForYou", "Storytime", "Explore"]:
        if default_tag.lower() not in [t.lower() for t in tags]:
            tags.append(default_tag)

    hashtags_str = " ".join([f"#{t}" for t in tags[:8]])
    
    desc_lines = []
    desc_lines.append(f"🔥 {clean_title.replace(' #Shorts', '')}")
    if description and description.strip():
        desc_lines.append(f"\n{description.strip()}")
    if source_title and source_title.strip():
        desc_lines.append(f"\n🎬 Original Story: {source_title.strip()}")
    
    desc_lines.append("\n👉 Like, Share & Subscribe for more daily viral clips!\n")
    desc_lines.append(hashtags_str)

    return {
        "title": clean_title[:100],
        "description": "\n".join(desc_lines),
        "tags": tags[:20]
    }


def upload_clip_to_youtube(
    video_path: str,
    title: str,
    description: str,
    hashtags: Optional[List[str]] = None,
    source_title: Optional[str] = None,
    privacy_status: str = "unlisted",
    category_id: str = "24",  # 24: Entertainment, 22: People & Blogs
    progress_callback = None,
    job_id: int = 0
) -> Dict[str, Any]:
    """
    Upload video file to YouTube as a Short using Resumable Upload protocol.
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file to upload does not exist: {video_path}")

    # Pre-upload dimension validation with warning (non-blocking)
    try:
        from services.dimension_checker import check_dimensions
        dims = check_dimensions(video_path)
        if not dims.get("is_valid_916"):
            width = dims.get("width", "?")
            height = dims.get("height", "?")
            job_warning(job_id, f"Video not 9:16 ({width}x{height})")
    except Exception as dim_err:
        job_warning(job_id, f"Dimension check failed: {dim_err}")

    status = get_youtube_auth_status()
    if not status["authenticated"]:
        raise PermissionError("YouTube account is not authenticated. Please connect your YouTube account first.")

    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.auth.transport.requests import Request

    token_file = get_token_path()
    with open(token_file, "r") as f:
        token_data = json.load(f)

    creds = Credentials.from_authorized_user_info(token_data, SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w") as f:
            f.write(creds.to_json())

    youtube = build("youtube", "v3", credentials=creds)

    meta = format_shorts_metadata(title, description, hashtags, source_title=source_title)

    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta["tags"],
            "categoryId": str(category_id)
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False
        }
    }

    media = MediaFileUpload(
        video_path,
        mimetype="video/mp4",
        chunksize=1024 * 1024 * 2,  # 2MB chunksize
        resumable=True
    )

    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media
    )

    job_info(0, f"Starting YouTube Shorts upload for '{meta['title']}' ({os.path.getsize(video_path)/(1024*1024):.2f} MB)...")

    response = None
    while response is None:
        upload_status, response = request.next_chunk()
        if upload_status and progress_callback:
            progress_pct = int(upload_status.progress() * 100)
            progress_callback(progress_pct)
            job_info(0, f"YouTube upload progress: {progress_pct}%")

    video_id = response.get("id")
    if not video_id:
        raise Exception(f"YouTube upload finished but returned no video ID: {response}")

    shorts_url = f"https://youtube.com/shorts/{video_id}"
    watch_url = f"https://www.youtube.com/watch?v={video_id}"

    job_info(0, f"YouTube Shorts upload SUCCESS: {shorts_url}")

    # Every real publish resets the pacing clock, whichever path made it: the
    # worker's per-clip upload, a batch sweep, or a one-click from the UI.
    from utils.upload_pacing import note_upload
    note_upload("youtube")

    return {
        "success": True,
        "video_id": video_id,
        "shorts_url": shorts_url,
        "watch_url": watch_url,
        "title": meta["title"],
        "privacy_status": privacy_status
    }


def upload_all_clips_for_job(
    job_id: int,
    db,
    privacy_status: str = "unlisted",
    category_id: str = "24",
    clip_progress_callback = None,
    pace: bool = False,
    stop=None
) -> Dict[str, Any]:
    """
    Batch upload all rendered clips from a specific job directly to YouTube Shorts.

    ``pace`` spaces the publishes like the worker's per-clip path does; the API
    endpoints leave it off, because a human pressing "upload" asked for them now.
    """
    from database.models import Clip, Job
    from datetime import datetime
    from pathlib import Path
    from config import settings

    status = get_youtube_auth_status()
    if not status["authenticated"]:
        raise PermissionError("YouTube account is not connected. Please connect your YouTube account in settings first.")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job {job_id} not found")

    # Read source title from meta.json if available
    source_title = None
    job_dir = Path(settings.STORAGE_ROOT) / "jobs" / str(job_id)
    meta_file = job_dir / "meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as mf:
                mdata = json.load(mf)
                source_title = mdata.get("title")
        except Exception:
            pass

    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.index.asc()).all()
    if not clips:
        raise ValueError(f"No clips found for Job {job_id}")

    results = []
    total_clips = len(clips)

    for idx, clip in enumerate(clips):
        if clip.uploaded and clip.youtube_video_id:
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": clip.title or f"Clip #{clip.index + 1}",
                "video_id": clip.youtube_video_id,
                "shorts_url": f"https://youtube.com/shorts/{clip.youtube_video_id}",
                "status": "uploaded"
            })
            continue

        # Locate video file
        video_path = None
        if clip.file_path:
            cand = Path(settings.STORAGE_ROOT) / clip.file_path
            if cand.exists():
                video_path = str(cand.resolve())
            elif os.path.exists(clip.file_path):
                video_path = str(Path(clip.file_path).resolve())

        if not video_path:
            clip_dir = job_dir / f"clip_{clip.index}"
            for cand_name in ["final_video.mp4", "final_short.mp4", "short.mp4"]:
                cand = clip_dir / cand_name
                if cand.exists():
                    video_path = str(cand.resolve())
                    break

        if not video_path or not os.path.exists(video_path):
            err_msg = f"Video file not found for clip #{clip.index} (ID: {clip.id})"
            clip.upload_error = err_msg
            clip.uploaded = False
            db.commit()
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": clip.title or f"Clip #{clip.index + 1}",
                "status": "failed",
                "error": err_msg
            })
            continue

        if clip_progress_callback:
            clip_progress_callback(idx + 1, total_clips, clip.title or f"Clip #{clip.index + 1}")

        title = clip.title or f"Viral Moment #{clip.index + 1}"
        description = clip.description or "Watch this viral moment!"
        hashtags = clip.hashtags or ["#Shorts", "#Viral", "#Trending"]

        if pace:
            from utils.upload_pacing import wait_for_slot
            waited = wait_for_slot("youtube", stop=stop)
            if stop is not None and stop.is_set():
                job_info(job_id, f"Lease lost during upload pacing; stopping the YouTube sweep before clip #{clip.index}")
                results.append({"clip_id": clip.id, "clip_index": clip.index,
                                "title": title, "status": "skipped",
                                "reason": "lease lost during pacing"})
                break
            if waited:
                job_info(job_id, f"⏳ Spacing the publish: waited {int(waited)} s")

        try:
            res = upload_clip_to_youtube(
                video_path=video_path,
                title=title,
                description=description,
                hashtags=hashtags,
                source_title=source_title,
                privacy_status=privacy_status,
                category_id=category_id,
                job_id=job_id
            )

            clip.uploaded = True
            clip.youtube_video_id = res["video_id"]
            clip.published_at = datetime.utcnow()
            clip.upload_error = None
            db.commit()
            db.refresh(clip)

            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": res["title"],
                "video_id": res["video_id"],
                "shorts_url": res["shorts_url"],
                "status": "uploaded"
            })
        except Exception as e:
            clip.uploaded = False
            clip.upload_error = str(e)
            clip.upload_retry_count += 1
            db.commit()
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": title,
                "status": "failed",
                "error": str(e)
            })

    uploaded_count = len([r for r in results if r["status"] == "uploaded"])
    failed_count = len([r for r in results if r["status"] == "failed"])

    return {
        "success": uploaded_count > 0,
        "job_id": job_id,
        "total_clips": total_clips,
        "uploaded_count": uploaded_count,
        "failed_count": failed_count,
        "results": results
    }
