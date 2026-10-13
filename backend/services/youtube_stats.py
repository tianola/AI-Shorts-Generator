"""Read the results back.

The pipeline used to stop at the upload: nothing ever asked what a published
short did, so a hook experiment had no measurement to be an experiment about.
This reads the statistics of clips we hold a real YouTube id for - read-only,
never uploads, never touches anything but the four stats columns - and skips the
placeholder ids old rows carry ('queued' and friends), which are not videos.

The client is injectable because the API is not installed on every machine and
because no test should have to reach Google.
"""
import json
import re
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from sqlalchemy.orm import Session

from database.models import Clip
from utils.logger import job_debug

# YouTube video ids are exactly this shape; anything else is a placeholder or a
# truncated value and must not be sent to the API as if it were a video.
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
BATCH = 50


def _build_client():
    """An authorised youtube v3 client, refreshing the stored token if needed."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from services.youtube_upload import SCOPES, get_token_path

    token_file = get_token_path()
    with open(token_file, encoding="utf-8") as fh:
        creds = Credentials.from_authorized_user_info(json.load(fh), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(token_file, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def real_video_ids(clips: Iterable[Clip]) -> List[str]:
    """The ids worth asking about, deduplicated and in first-seen order."""
    seen, out = set(), []
    for clip in clips:
        vid = (clip.youtube_video_id or "").strip()
        if VIDEO_ID.match(vid) and vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def fetch_statistics(video_ids: List[str], client=None) -> Dict[str, Dict[str, int]]:
    """Return {video_id: {"viewCount": n, ...}} for the ids YouTube still has.

    The API answers with strings; anything it omits (a private video reports no
    likes) is left out rather than reported as zero.
    """
    if not video_ids:
        return {}
    client = client or _build_client()
    found: Dict[str, Dict[str, int]] = {}
    for start in range(0, len(video_ids), BATCH):
        batch = video_ids[start:start + BATCH]
        response = client.videos().list(part="statistics", id=",".join(batch)).execute()
        for item in response.get("items", []):
            stats = item.get("statistics") or {}
            found[item["id"]] = {
                key: int(stats[key])
                for key in ("viewCount", "likeCount", "commentCount")
                if str(stats.get(key, "")).lstrip("-").isdigit()
            }
    return found


def sync_clip_stats(db: Session, *, job_id: Optional[int] = None,
                    client=None) -> Dict[str, int]:
    """Write the current view counts onto the clips that were published.

    Only clips with a real video id are updated. An id YouTube does not return
    was deleted (or never existed), and its row is counted as missing rather than
    zeroed - a deleted video is a different fact from a video nobody watched.
    """
    query = db.query(Clip)
    if job_id is not None:
        query = query.filter(Clip.job_id == job_id)
    clips = [c for c in query.all() if (c.youtube_video_id or "").strip()]

    ids = real_video_ids(clips)
    placeholder = sum(1 for c in clips if not VIDEO_ID.match((c.youtube_video_id or "").strip()))
    if not ids:
        return {"clips_with_id": len(clips), "placeholders": placeholder,
                "videos_queried": 0, "updated": 0, "missing_on_youtube": 0}

    stats = fetch_statistics(ids, client=client)
    now = datetime.utcnow()
    updated = 0
    missing = 0
    for clip in clips:
        vid = (clip.youtube_video_id or "").strip()
        if not VIDEO_ID.match(vid):
            continue
        entry = stats.get(vid)
        if entry is None:
            clip.stats_synced_at = now
            missing += 1
            continue
        clip.view_count = entry.get("viewCount")
        clip.like_count = entry.get("likeCount")
        clip.comment_count = entry.get("commentCount")
        clip.stats_synced_at = now
        updated += 1
    db.commit()
    job_debug(job_id or 0, f"YouTube stats: {updated} clips updated, "
                           f"{missing} ids no longer on YouTube")
    return {"clips_with_id": len(clips), "placeholders": placeholder,
            "videos_queried": len(ids), "updated": updated,
            "missing_on_youtube": missing}
