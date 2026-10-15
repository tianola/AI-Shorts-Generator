"""
TikTok publishing service for AI Shorts Generator.

The HTTP work lives in services/tiktok_content_api.py (Content Posting API v2);
this module is the layer the worker and the API endpoints talk to: credential
status, viral caption formatting, and per-job batch publishing.
"""
import os
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

# YouTube-style visibility -> TikTok's privacy enum. TikTok has no 'unlisted';
# FOLLOWER_OF_CREATOR is the nearest thing to "not for the whole world".
TIKTOK_PRIVACY_BY_VISIBILITY = {
    "public": "PUBLIC_TO_EVERYONE",
    "unlisted": "FOLLOWER_OF_CREATOR",
    "private": "SELF_ONLY",
}


def tiktok_privacy_for(visibility: Optional[str]) -> str:
    """Translate an AUTO_UPLOAD_PRIVACY value, rejecting anything unknown."""
    normalized = (visibility or "").lower().strip()
    if normalized not in TIKTOK_PRIVACY_BY_VISIBILITY:
        raise ValueError(
            f"Unknown TikTok visibility '{visibility}'. "
            f"Expected one of {sorted(TIKTOK_PRIVACY_BY_VISIBILITY)}"
        )
    return TIKTOK_PRIVACY_BY_VISIBILITY[normalized]

def get_secrets_dir() -> Path:
    from config import settings
    sec_dir = Path(settings.STORAGE_ROOT) / "secrets"
    sec_dir.mkdir(parents=True, exist_ok=True)
    return sec_dir

def get_tiktok_token_path() -> Path:
    return get_secrets_dir() / "tiktok_token.json"

def get_tiktok_auth_status() -> Dict[str, Any]:
    """Publishing readiness, straight from the OAuth token store.

    The legacy ``tiktok_token.json`` held a scraped browser session_id, which
    the Content Posting API has never accepted. It used to be enough to make
    this report 'authenticated' and let the upload path return a made-up video
    id, so it no longer counts: a stored session is reported as a note, never
    as a connection.
    """
    from services import tiktok_content_api as api

    status = api.connection_status()
    legacy_username = None
    legacy_file = get_tiktok_token_path()
    if legacy_file.exists() and legacy_file.stat().st_size:
        try:
            legacy_username = json.loads(legacy_file.read_text(encoding="utf-8")).get("username")
        except Exception:
            legacy_username = None

    authenticated = bool(status["connected"])
    return {
        "authenticated": authenticated,
        "username": status.get("username") or (legacy_username if not authenticated else None),
        "auth_type": "oauth2" if authenticated else ("session_id" if legacy_username else None),
        "has_token": authenticated,
        "client_configured": status["client_configured"],
        "post_mode": status["post_mode"],
        "scopes": api.requested_scopes(),
        "direct_post_available": api.has_direct_post_rights(),
        "connected_at": status.get("expires_at"),
        "token_path": status["token_path"] if authenticated else str(legacy_file.resolve()),
        "note": None if authenticated else (
            "A TikTok app client key/secret and an OAuth connection are required to publish; "
            "the stored browser session_id cannot post."
            if legacy_username else "TikTok is not connected."
        ),
    }

def save_tiktok_credentials(username: str, session_id: str, auth_type: str = "session_id") -> Dict[str, Any]:
    """Store a legacy session_id — kept so the settings page does not break.

    It cannot publish. Callers get the truth in ``publishing`` rather than a
    success that only looks like one.
    """
    clean_username = (username or "").strip()
    if not clean_username.startswith("@") and clean_username:
        clean_username = f"@{clean_username}"

    clean_session = (session_id or "").strip()
    if not clean_username or not clean_session:
        raise ValueError("Both Username and Session ID / Token are required.")

    token_file = get_tiktok_token_path()
    token_data = {
        "username": clean_username,
        "session_id": clean_session,
        "access_token": clean_session,
        "auth_type": auth_type,
        "connected_at": datetime.utcnow().isoformat()
    }
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2)

    logger.info(f"TikTok session_id stored for {clean_username} (cannot publish — needs OAuth)")
    return {
        "success": True,
        "username": clean_username,
        "authenticated": False,
        "publishing": False,
        "message": "Saved, but a session_id cannot post to TikTok. Connect the account "
                   "through the TikTok app OAuth flow (/api/tiktok/auth-url) to publish.",
    }

def disconnect_tiktok_account() -> bool:
    """Disconnect and delete stored TikTok credentials."""
    from services import tiktok_content_api as api
    api.disconnect()
    token_file = get_tiktok_token_path()
    if token_file.exists():
        try:
            token_file.unlink()
            logger.info("TikTok account disconnected successfully.")
        except Exception as e:
            logger.error(f"Failed to delete TikTok credentials: {e}")
            return False
    return True

def format_tiktok_metadata(
    title: str,
    description: str,
    hashtags: Optional[List[str]] = None,
    source_title: Optional[str] = None
) -> Dict[str, Any]:
    """
    Format metadata specifically optimized for the TikTok algorithm:
      - Punchy viral caption (under 2200 chars, ideal 100-300 chars)
      - Dynamic trending tags (#FYP, #Viral, #Trending, #Storytime)
      - Channel credit & call to follow
    """
    clean_title = (title or "Viral Moment").strip()
    
    tags = []
    if hashtags:
        for h in hashtags:
            clean_h = h.strip().lstrip("#")
            if clean_h and clean_h.lower() not in [t.lower() for t in tags]:
                tags.append(clean_h)

    for default_tag in ["FYP", "ForYou", "TikTok", "Viral", "Trending", "Storytime"]:
        if default_tag.lower() not in [t.lower() for t in tags]:
            tags.append(default_tag)

    hashtags_str = " ".join([f"#{t}" for t in tags[:10]])

    caption_lines = [
        f"🔥 {clean_title}",
    ]
    if description and description.strip():
        caption_lines.append(f"\n{description.strip()}")
    if source_title and source_title.strip():
        caption_lines.append(f"\n🎬 Credit: {source_title.strip()}")
    
    from config import settings as _settings
    caption_lines.append(f"\n✨ Follow {_settings.WATERMARK_HANDLE} for more daily clips!\n")
    caption_lines.append(hashtags_str)

    return {
        "title": clean_title,
        "caption": "\n".join(caption_lines),
        "tags": tags[:20]
    }

def upload_clip_to_tiktok(
    video_path: str,
    title: str,
    description: str,
    hashtags: Optional[List[str]] = None,
    source_title: Optional[str] = None,
    privacy_level: str = "SELF_ONLY",
    post_mode: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Publish one clip through the TikTok Content Posting API.

    Raises if TikTok refuses or the account is not connected — this never
    returns a video id that TikTok did not hand back.
    """
    from services import tiktok_content_api as api

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at {video_path}")

    if not dry_run:
        auth_status = get_tiktok_auth_status()
        if not auth_status.get("authenticated"):
            raise ValueError(
                "❌ TikTok is not connected through OAuth, so nothing can be published. "
                "Create a TikTok for Developers app (Content Posting API + video.publish), "
                "set its client key/secret, then authorise the account at /api/tiktok/auth-url. "
                "A browser session_id is not accepted by the API."
            )

    meta = format_tiktok_metadata(title, description, hashtags, source_title=source_title)
    result = api.upload_video(
        video_path=video_path,
        caption=meta["caption"],
        privacy_level=privacy_level,
        post_mode=post_mode,
        dry_run=dry_run,
    )
    if result.get("dry_run"):
        return {**result, "title": meta["title"], "caption": meta["caption"]}

    from utils.upload_pacing import note_upload
    note_upload("tiktok")

    logger.info(f"Published to TikTok ({result['post_mode']}): {meta['title']} "
                f"publish_id={result['publish_id']} video_id={result['video_id']}")
    return {
        "success": result.get("status") == "PUBLISH_COMPLETE" or result.get("post_mode") != "DIRECT_PUBLISH",
        "video_id": result.get("video_id") or result.get("publish_id"),
        "publish_id": result.get("publish_id"),
        "tiktok_url": result.get("tiktok_url"),
        "status": result.get("status"),
        "post_mode": result.get("post_mode"),
        "privacy_level": result.get("privacy_level"),
        "username": result.get("username"),
        "title": meta["title"],
        "caption": meta["caption"],
    }

def upload_all_clips_to_tiktok_for_job(
    job_id: int,
    db,
    privacy_level: str = "SELF_ONLY",
    pace: bool = False,
    stop=None
) -> Dict[str, Any]:
    """
    Batch publish all rendered clips from a job through the Content Posting API.

    ``pace`` spaces the publishes against the same per-platform clock the
    worker's per-clip upload uses.
    """
    from database.models import Clip, Job
    from pathlib import Path
    from config import settings

    auth_status = get_tiktok_auth_status()
    if not auth_status.get("authenticated"):
        raise ValueError("❌ TikTok account is NOT connected! Please click 'TikTok Auth' and connect your TikTok profile first.")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job {job_id} not found")

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
        # The per-clip auto-upload in the worker already published this clip.
        # Re-publishing produced duplicate TikTok posts from the same job.
        if clip.tiktok_video_id:
            results.append({"clip_id": clip.id, "clip_index": clip.index,
                            "index": clip.index, "skipped": True, "status": "skipped",
                            "reason": "already published", "tiktok_url": clip.tiktok_video_id})
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
            err_msg = f"Video file not found for clip #{clip.index}"
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": clip.title or f"Clip #{clip.index + 1}",
                "status": "failed",
                "error": err_msg
            })
            continue

        title = clip.title or f"Viral Moment #{clip.index + 1}"
        description = clip.description or "Watch this viral moment!"
        hashtags = clip.hashtags or ["#TikTok", "#Viral", "#FYP"]

        if pace:
            from utils.upload_pacing import wait_for_slot
            waited = wait_for_slot("tiktok", stop=stop)
            if stop is not None and stop.is_set():
                results.append({"clip_id": clip.id, "clip_index": clip.index,
                                "title": title, "status": "skipped",
                                "reason": "lease lost during pacing"})
                break
            if waited:
                logger.info(f"⏳ Spacing the TikTok publish: waited {int(waited)} s")

        try:
            res = upload_clip_to_tiktok(
                video_path=video_path,
                title=title,
                description=description,
                hashtags=hashtags,
                source_title=source_title,
                privacy_level=privacy_level
            )
            clip.tiktok_video_id = res.get("video_id") or res.get("tiktok_url")
            db.commit()
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": res["title"],
                "video_id": res["video_id"],
                "tiktok_url": res["tiktok_url"],
                "status": "uploaded"
            })
        except Exception as e:
            results.append({
                "clip_id": clip.id,
                "clip_index": clip.index,
                "title": title,
                "status": "failed",
                "error": str(e)
            })

    uploaded_count = len([r for r in results if r["status"] == "uploaded"])
    failed_count = len([r for r in results if r["status"] == "failed"])
    skipped_count = len([r for r in results if r["status"] == "skipped"])

    return {
        # The worker publishes per clip while it renders, so this sweep normally
        # finds every clip already posted. Nothing left to do is not a failure.
        "success": failed_count == 0,
        "job_id": job_id,
        "username": auth_status.get("username"),
        "total_clips": total_clips,
        "uploaded_count": uploaded_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "results": results
    }
