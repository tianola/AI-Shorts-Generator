"""The publish queue: when each finished clip goes out, and the upload itself.

This used to live in worker.py. It is the part of the worker that runs after
rendering: it spaces uploads across the day, holds clips that need review or
whose script does not match the channel, records every attempt before it starts
so a crash cannot cause a double upload, and publishes on operator request.
worker.py re-exports the public names, so `from worker import ...` keeps working.
"""
import json
import os
import random
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from loguru import logger
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from config import settings
from database.models import Clip, Job
from utils.logger import job_error, job_info, job_warning


PUBLISH_CLAIM_MINUTES = 30

# An upload attempt whose outcome is still unrecorded once its own claim has
# expired is not in flight any more - whoever started it is gone, and nothing left
# behind says whether YouTube got the video. The grace is the claim window because
# that is exactly how long a live attempt is promised to be.
PUBLISH_ATTEMPT_GRACE_MINUTES = PUBLISH_CLAIM_MINUTES


def next_publish_slot(db, now: Optional[datetime] = None) -> datetime:
    """The earliest moment the next YouTube publish may go out.

    The spacing used to live in a dict inside the pacing module, so a worker
    restart reset it and the very next upload fired immediately. The last publish
    that actually happened is already recorded on its clip row, so the queue reads
    the gap from the database and survives the restart.
    """
    now = now or datetime.utcnow()
    gap = max(0, int(settings.UPLOAD_MIN_GAP_SECONDS))
    if gap == 0:
        return now
    last = db.query(func.max(Clip.published_at)).filter(
        Clip.youtube_video_id.isnot(None),
        Clip.youtube_video_id != "",
    ).scalar()
    if not last:
        return now
    jitter = max(0, int(settings.UPLOAD_GAP_JITTER_SECONDS))
    return max(now, last + timedelta(seconds=gap + random.uniform(0, jitter)))


def _release_window():
    """The (start, end) UTC hours a release may land in, or None when unset.

    End is exclusive, so "6-22" means 06:00 through 21:59.
    """
    raw = (getattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "") or "").strip()
    if not raw:
        return None
    try:
        start, end = raw.split("-", 1)
        start, end = int(start), int(end)
    except ValueError:
        logger.warning(f"PUBLISH_RELEASE_WINDOW_UTC={raw!r} is not 'start-end'; ignored")
        return None
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        logger.warning(f"PUBLISH_RELEASE_WINDOW_UTC={raw!r} is an empty window; ignored")
        return None
    return start, end


def _snap_into_release_window(moment: datetime) -> datetime:
    """Move a slot forward, never back, until it sits inside the release window.

    A channel that publishes at 03:17, 15:17 and 03:17 again reads as a cron job to
    the people who report spam, and to anyone comparing the timestamps. The window is
    cosmetic to YouTube and not to its audience, so it is applied after the cadence
    maths and can only delay a release, which is the safe direction.
    """
    bounds = _release_window()
    if not bounds:
        return moment
    start, end = bounds
    while True:
        if start < end and start <= moment.hour < end:
            return moment
        if start > end and (moment.hour >= start or moment.hour < end):
            return moment
        if moment.hour < start:
            return moment.replace(hour=start, minute=0, second=0, microsecond=0)
        return (moment + timedelta(days=1)).replace(hour=start, minute=0,
                                                    second=0, microsecond=0)


def uploads_in_window(db, now: Optional[datetime] = None, hours: int = 24):
    """Uploads YouTube accepted in the last `hours`, oldest first, as the channel counts them.

    The budget is spent at upload, so this reads the successes, not the releases: a clip
    sitting private with a publishAt tomorrow has already cost the channel one slot.
    """
    now = now or datetime.utcnow()
    since = now - timedelta(hours=hours)
    rows = db.query(Clip.published_at).filter(
        Clip.youtube_video_id.isnot(None),
        Clip.youtube_video_id != "",
        Clip.published_at.isnot(None),
        Clip.published_at > since,
    ).order_by(Clip.published_at.asc()).all()
    return [row[0] for row in rows]


def next_budget_slot(db, now: Optional[datetime] = None):
    """When the rolling upload budget has room again, or None while it already does.

    YouTube's own number is unpublished, so this waits for the oldest success to age out
    of the window rather than guessing a fixed retry delay.
    """
    budget = max(0, int(getattr(settings, "UPLOAD_DAILY_BUDGET", 0)))
    if budget <= 0:
        return None
    recent = uploads_in_window(db, now=now)
    if len(recent) < budget:
        return None
    return recent[0] + timedelta(hours=24)


def next_release_slot(db, now: Optional[datetime] = None,
                      source_url: Optional[str] = None) -> datetime:
    """The earliest instant YouTube may be given as the next clip's release.

    This is the channel's cadence — when the audience sees a video — and it is a
    different clock from next_publish_slot, which is how fast this machine may
    hand over uploads. The anchor is the latest scheduled_publish_at already in
    the database, whether that clip is waiting for its slot or already released,
    so a restart continues the schedule and two jobs cannot be parked on the
    same minute. Nothing is ever released before PUBLISH_SCHEDULE_LEAD_SECONDS
    after now, which is the window to inspect a clip first, and no release lands
    outside the daily window.

    source_url adds the channel's other rule: clips cut from the same original
    video hold each other at PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS apart, so one
    stream cannot turn a week of the channel into fifteen pieces of itself.
    """
    now = now or datetime.utcnow()
    gap = max(60, int(settings.PUBLISH_SCHEDULE_GAP_SECONDS))
    lead = max(60, int(settings.PUBLISH_SCHEDULE_LEAD_SECONDS))
    candidate = now + timedelta(seconds=lead)

    last = db.query(func.max(Clip.scheduled_publish_at)).filter(
        Clip.scheduled_publish_at.isnot(None),
    ).scalar()
    if last:
        candidate = max(candidate, last + timedelta(seconds=gap))

    if source_url:
        same_source = db.query(func.max(Clip.scheduled_publish_at)).join(
            Job, Clip.job_id == Job.id).filter(
            Clip.scheduled_publish_at.isnot(None),
            Job.youtube_url == source_url,
        ).scalar()
        source_gap = max(0, int(getattr(settings, "PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS", 0)))
        if same_source and source_gap:
            candidate = max(candidate, same_source + timedelta(seconds=source_gap))

    return _snap_into_release_window(candidate)


# Selection paths that are not a model's choice: the transcript-heuristic filler
# and the evenly spaced grid the selector emits when every LLM is down.
UNREVIEWED_SELECTION_PATHS = ("heuristic", "grid")


def _needs_review_before_publish(clip) -> bool:
    """True for a clip that must not publish unattended: no model chose it, or its quality check failed.

    Only a failed check holds a clip back (a broken file, wrong size, missing audio, clipping);
    warnings are for a person to look at, but do not stop the queue.
    """
    breakdown = clip.score_breakdown if isinstance(clip.score_breakdown, dict) else {}
    if breakdown.get("selection_path") in UNREVIEWED_SELECTION_PATHS:
        return True
    quality = getattr(clip, "quality_report", None)
    return isinstance(quality, dict) and quality.get("status") == "failed"


def enqueue_clips_for_publish(db, job_id: int, now: Optional[datetime] = None,
                              include_unreviewed: bool = False) -> int:
    """Park every rendered clip of a job in the publish queue.

    Two clocks are written per clip: publish_due_at is when this machine uploads
    it, scheduled_publish_at is when YouTube releases it. The upload time is
    derived from the release, not from how fast this PC can push bytes, because
    the channel's upload budget is spent at upload: fifteen clips dated over two
    weeks would otherwise burn the whole rolling day of uploads on the first
    afternoon and be refused before half of them ever had a turn.
    Idempotent: a clip that already waits keeps the slot it was given, and one
    that already has a video id is finished. Returns how many rows were added.

    A clip the selector did not get from a model (see UNREVIEWED_SELECTION_PATHS)
    is left out unless include_unreviewed is set, which only the operator's own
    "upload" request does. Those windows exist so an LLM outage still renders
    something to look at; queued automatically they put filler on the channel.
    """
    now = now or datetime.utcnow()
    source_url = db.query(Job.youtube_url).filter(Job.id == job_id).scalar()
    pending = db.query(Clip).filter(
        Clip.job_id == job_id,
        Clip.publish_due_at.is_(None),
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
    ).order_by(Clip.index.asc()).all()
    if not include_unreviewed:
        unreviewed = [clip for clip in pending if _needs_review_before_publish(clip)]
        if unreviewed:
            pending = [clip for clip in pending if clip not in unreviewed]
            job_warning(job_id, "Not queued automatically (no model chose these windows): clip "
                                + ", ".join(f"#{clip.index}" for clip in unreviewed)
                                + ". Review them and press upload to publish.")
    # A row parked before the release clock existed has a due time and no slot.
    # Left that way it uploads unscheduled, i.e. public the instant the queue
    # reaches it, so the backlog gets slots continuing after the new ones.
    unslotted = db.query(Clip).filter(
        Clip.job_id == job_id,
        Clip.publish_due_at.isnot(None),
        Clip.scheduled_publish_at.is_(None),
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
    ).order_by(Clip.index.asc()).all()
    if not pending and not unslotted:
        return 0
    lead = max(60, int(getattr(settings, "PUBLISH_UPLOAD_LEAD_SECONDS", 21600)))
    release = next_release_slot(db, now=now, source_url=source_url)
    release_gap = max(60, int(settings.PUBLISH_SCHEDULE_GAP_SECONDS))
    # Each slot is placed after the one before it and snapped again on the way,
    # so a job's own clips cannot straddle the window's edge: adding the cadence
    # to a slot that was already pushed to 06:00 would otherwise hand the second
    # clip a 00:15 release, which is the cron-job look the window exists to stop.
    slot = release
    for clip in pending:
        clip.scheduled_publish_at = slot
        clip.publish_due_at = slot - timedelta(seconds=lead)
        slot = _snap_into_release_window(slot + timedelta(seconds=release_gap))
    for clip in unslotted:
        clip.scheduled_publish_at = slot
        # Never earlier than the row already asks for: a due time an operator set
        # by hand is a request to wait, and the release maths has no authority to
        # cancel it. It may still be pushed later, which is the whole point.
        clip.publish_due_at = max(clip.publish_due_at, slot - timedelta(seconds=lead))
        slot = _snap_into_release_window(slot + timedelta(seconds=release_gap))
    db.commit()
    return len(pending)


def claim_clip_for_publish(db, clip_id: int, expected_due: datetime,
                           now: Optional[datetime] = None) -> bool:
    """Take one queued clip, refusing it if anybody moved it first.

    The claim moves the due time out rather than deleting it: a worker that dies
    mid-upload leaves a row that simply becomes due again, the same way an expired
    job lease comes back to the queue.
    """
    now = now or datetime.utcnow()
    # Typed ORM comparison, not a raw text() bind: SQLite stores these columns as
    # '...:SS.000000' and an untyped datetime parameter renders without the
    # microsecond suffix, so a whole-second slot matched nothing and was never
    # claimed.
    result = db.query(Clip).filter(
        Clip.id == clip_id,
        Clip.publish_due_at == expected_due,
    ).update({"publish_due_at": now + timedelta(minutes=PUBLISH_CLAIM_MINUTES)},
             synchronize_session=False)
    db.commit()
    return result == 1


def _clip_video_path(clip) -> Optional[str]:
    if not clip.file_path:
        return None
    if os.path.exists(clip.file_path):
        return clip.file_path
    candidate = Path(settings.STORAGE_ROOT) / clip.file_path
    return str(candidate) if candidate.exists() else None


def _clip_thumbnail_path(clip) -> Optional[str]:
    """The designed thumbnail the render step already wrote for this clip."""
    if not getattr(clip, "thumbnail_path", None):
        return None
    if os.path.exists(clip.thumbnail_path):
        return clip.thumbnail_path
    candidate = Path(settings.STORAGE_ROOT) / clip.thumbnail_path
    return str(candidate) if candidate.exists() else None


def _source_title_for_job(job_id: int) -> Optional[str]:
    meta_file = Path(settings.STORAGE_ROOT) / "jobs" / str(job_id) / "meta.json"
    try:
        if meta_file.exists():
            return json.loads(meta_file.read_text(encoding="utf-8")).get("title")
    except Exception:
        return None
    return None


def _is_channel_auth_failure(message: str) -> bool:
    """True when the refusal says the channel is not connected, not that this clip failed.

    Two shapes reach here: the uploader's own `PermissionError` after its auth check,
    and Google's `RefreshError` from the refresh inside the upload. Both mean the
    refresh token is dead Google-side, which code cannot renew - only a person
    re-consenting can. Job 109 lost five clips to the first shape in ninety minutes,
    each on three strikes, with both clocks cleared so nothing could ever find them.
    """
    text = (message or "").lower()
    return any(marker in text for marker in
               ("not authenticated", "invalid_grant", "token has been expired or revoked"))


def _record_publish_failure(db, clip, now: datetime, message: str) -> None:
    """Keep the clip queued and decide when it may be tried again.

    YouTube's upload limit is the channel's budget, not a fault in this clip, so it
    is not charged as an attempt: the clip waits hours instead of retiring after
    three refusals on a busy day. A disconnected channel is the same shape but worse
    - every clip on it fails identically and no retry can ever succeed - so it is
    forgiven too, and said out loud instead. Everything else counts, and a clip that
    fails MAX_UPLOAD_RETRIES times leaves the queue for a human to read.

    An exception here is an outcome, so the attempt marker goes: leaving it set
    would hold a clip that simply needs another try alongside one whose upload
    genuinely vanished mid-flight.
    """
    clip.upload_error = message
    clip.publish_attempt_at = None
    if "uploadLimitExceeded" in message:
        retry_at = now + timedelta(hours=max(1, int(settings.PUBLISH_LIMIT_RETRY_HOURS)))
        clear_at = next_budget_slot(db, now=now)
        # A fixed wait is a guess about a window this code can measure: if the
        # budget is already full of recent successes, the clip is not ready at
        # +6 h, it is ready when the oldest success ages out of the rolling day.
        clip.publish_due_at = max(retry_at, clear_at) if clear_at else retry_at
        db.commit()
        return
    if _is_channel_auth_failure(message):
        clip.publish_due_at = now + timedelta(
            minutes=max(1, int(settings.PUBLISH_RETRY_MINUTES)))
        db.commit()
        job_error(clip.job_id, "YouTube is not connected: the OAuth token is expired or "
                               "revoked, so nothing on this channel can publish. Reconnect "
                               "it in Settings -> YouTube; only a person can do that. "
                               "No clip lost a retry and the release schedule is untouched.")
        return
    clip.upload_retry_count = (clip.upload_retry_count or 0) + 1
    if clip.upload_retry_count >= max(1, int(settings.MAX_UPLOAD_RETRIES)):
        clip.publish_due_at = None
        # The release slot belongs to the channel, not the clip, so a clip that
        # gave up must not keep the schedule open an hour past its own failure.
        clip.scheduled_publish_at = None
        job_warning(clip.job_id, f"Clip #{clip.index} left the publish queue after "
                                 f"{clip.upload_retry_count} failed attempts: {message}")
    else:
        clip.publish_due_at = now + timedelta(
            minutes=max(1, int(settings.PUBLISH_RETRY_MINUTES)))
    db.commit()


def _resolve_unresolved_attempts(db, now: datetime) -> int:
    """Stop on rows whose recorded upload never came back with an outcome.

    An attempt marker older than the claim window it was made under cannot still be
    in flight. At that point the row genuinely does not know whether YouTube has the
    video, and uploading again is the one action that turns an unknown into a
    duplicate on the channel - so the clip leaves the queue with the instant it
    started kept as evidence and the recovery named in its error, instead of going
    out twice or vanishing quietly the way a third retry would have retired it.
    """
    cutoff = now - timedelta(minutes=PUBLISH_ATTEMPT_GRACE_MINUTES)
    rows = db.query(Clip, Job).join(Job, Clip.job_id == Job.id).filter(
        Clip.publish_attempt_at.isnot(None),
        Clip.publish_attempt_at < cutoff,
        Clip.publish_due_at.isnot(None),
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
    ).order_by(Clip.publish_attempt_at.asc()).all()
    for clip, job in rows:
        started = clip.publish_attempt_at
        clip.upload_error = (
            f"Publish attempt started {started:%Y-%m-%d %H:%M} UTC never recorded a video id, "
            "so this clip may already be on YouTube. The queue refuses to upload it again. "
            "Settle it by hand: if the video is on the channel, store its id and clear "
            "publish_attempt_at; if it is not (`python -m scripts.reconcile_uploads --verify` "
            f"says so), clear publish_attempt_at and give row {clip.id} a fresh publish_due_at "
            "to queue it deliberately."
        )
        # Nulling the due time is also what keeps this announcement single: the row
        # drops out of the query above on the next pass instead of warning again.
        clip.publish_due_at = None
        job_warning(job.id, f"Clip #{clip.index} stopped with an unresolved upload attempt: "
                            f"{clip.upload_error}")
    if rows:
        db.commit()
    return len(rows)


def _ship_clip_to_youtube(db, clip, job, uploader: Optional[Callable], now: datetime,
                          privacy_status: Optional[str] = None,
                          category_id: Optional[str] = None) -> dict:
    """Upload one clip and record it, in the order that survives being killed.

    The single place this pipeline hands a file to YouTube. The order is the whole
    point: resolve the file, settle the release slot, commit the *intent*
    (`publish_attempt_at`), only then call the API, and clear the intent once the
    outcome - success or a refusal - is on the row. Doing it the other way round
    meant a process that died after the upload left a row that still looked
    un-published, and the queue would have shipped a second copy of the same clip.

    What remains is a narrower window, not a closed one: between the intent commit
    and the outcome commit the truth is unknown, and the response to that is to stop
    and tell a human (`_resolve_unresolved_attempts`), never to guess by re-uploading.
    """
    video_path = _clip_video_path(clip)
    if not video_path:
        message = "Rendered file is missing, nothing was uploaded"
        _record_publish_failure(db, clip, now, message)
        job_warning(job.id, f"Clip #{clip.index} could not be published: "
                            f"no file at {clip.file_path}")
        return {"status": "failed", "code": "missing_file", "error": message}

    blocked = _blocked_script(clip)
    if blocked is not None:
        # The clip stays queued and the intent marker is cleared: this is not a
        # failed upload, so holding it must not look like a publish that vanished
        # mid-flight, which is what `_resolve_unresolved_attempts` exists to recover.
        clip.publish_attempt_at = None
        db.commit()
        job_warning(job.id, f"Clip #{clip.index} held back: {blocked}. The title, "
                            f"description and hashtags are what the channel reads, so a "
                            f"non-Latin character there is a visible defect, not a "
                            f"metadata one. Edit the copy and click again.")
        return {"status": "held", "retry_at": clip.publish_due_at, "reason": blocked}

    if uploader is None:
        from services.youtube_upload import upload_clip_to_youtube
        uploader = upload_clip_to_youtube

    # A release slot can expire while the queue is being refused uploads, and
    # YouTube reads a publishAt in the past as "publish now". Moving the row keeps
    # the promise the clip is actually shipped with, and places the next clip
    # after it rather than on top of it. A clip with no slot at all - parked
    # before this column existed, or handed here by an operator's click - gets one
    # here, because the alternative is publishing it the instant its turn comes.
    lead_floor = now + timedelta(seconds=max(60, int(settings.PUBLISH_SCHEDULE_LEAD_SECONDS)))
    if clip.scheduled_publish_at is None:
        clip.scheduled_publish_at = next_release_slot(db, now=now,
                                                      source_url=job.youtube_url)
        db.commit()
    if clip.scheduled_publish_at < lead_floor:
        job_warning(job.id, f"Clip #{clip.index} release slot "
                            f"{clip.scheduled_publish_at:%Y-%m-%d %H:%M} UTC had already "
                            f"passed; moving it to {lead_floor:%Y-%m-%d %H:%M} UTC")
        clip.scheduled_publish_at = lead_floor
        db.commit()

    clip.publish_attempt_at = now
    db.commit()

    upload_kwargs = {
        "video_path": video_path,
        "title": clip.title,
        "description": clip.description or "",
        "hashtags": clip.hashtags or ["#Shorts"],
        "source_title": _source_title_for_job(job.id),
        "source_url": job.youtube_url,
        "privacy_status": privacy_status or getattr(job, "upload_privacy", None)
                          or settings.AUTO_UPLOAD_PRIVACY,
        "job_id": job.id,
        "thumbnail_path": _clip_thumbnail_path(clip),
        "publish_at": clip.scheduled_publish_at,
    }
    if category_id:
        upload_kwargs["category_id"] = category_id

    try:
        res = uploader(**upload_kwargs)
    except Exception as e:
        _record_publish_failure(db, clip, now, str(e))
        job_warning(job.id, f"Clip #{clip.index} failed to publish: {e}")
        return {"status": "failed", "code": "upload_failed", "error": str(e)}

    clip.uploaded = True
    clip.youtube_video_id = res.get("video_id")
    clip.published_at = datetime.utcnow()
    # Both clocks go: a row that says "published" and "a publish is due" at the
    # same time (clip 461) is a phantom every consumer of publish_due_at reads.
    clip.publish_due_at = None
    clip.publish_attempt_at = None
    clip.upload_error = None
    db.commit()
    job_info(job.id, f"🎉 Clip #{clip.index} published: {res.get('shorts_url')}")
    from utils.upload_pacing import note_upload
    note_upload("youtube")
    return {"status": "published", "result": res}


def _blocked_script(clip) -> Optional[str]:
    """The script of the copy this clip would be published with, or None when it is fine.

    The gate is about the script, not the language: a Greek title is not a defect, and the
    generator may write in any Latin or Greek word it likes. It is about the script, because
    clip 233 went live on a Greek/English channel with a Chinese title and nothing in the
    pipeline asked what script a published title was in before the upload call. The boundary
    is what decides what leaves this machine, so the check lives here and not at the
    generator: a clip an operator edits and re-clicks is still gated, and a clip rendered
    before this column existed is gated too.
    """
    for field in ("title", "description", "hashtags"):
        values = getattr(clip, field, None)
        if values is None:
            continue
        if field != "hashtags":
            values = [values]
        for txt in values:
            if not txt:
                continue
            for ch in txt:
                code = ord(ch)
                if ch.isspace() or ch.isdigit():
                    continue
                if ch.isascii():
                    continue
                if any(lo <= code <= hi for lo, hi in _ALLOWED_SCRIPT_RANGES):
                    continue
                category = unicodedata.category(ch)
                if not category.startswith("L"):
                    continue
                name = _LETTER_CATEGORIES.get(category, category.lower())
                offending = str(txt)[:60]
                return (f"{field} contains a {name} letter: {offending}. The title, "
                        f"description and hashtags are what the channel reads, so a "
                        f"non-Latin character there is a visible defect, not a "
                        f"metadata one. Edit the copy and click again.")
    return None


_LETTER_CATEGORIES = {
    "Lu": "uppercase", "Ll": "lowercase", "Lt": "titlecase",
    "Lm": "modifier", "Lo": "other",
}


_ALLOWED_SCRIPT_RANGES = (
    (0x0041, 0x005A), (0x0061, 0x007F),   # Basic Latin letters
    (0x00AA, 0x00AA), (0x00BA, 0x00BA),   # ordinal indicator, masculine ordinal
    (0x0100, 0x024F),                     # Latin Extended-A / B
    (0x1E00, 0x1EFF),                     # Latin Extended Additional
    (0x2C60, 0x2C7F),                     # Latin Extended-C
    (0xA720, 0xA7FF),                     # Latin Extended-D
    (0xFB00, 0xFB04),                     # Latin ligatures (fi, fl, ...)
    (0xFF21, 0xFF3A), (0xFF41, 0xFF5A),   # fullwidth Latin
    (0x0370, 0x03FF),                     # Greek and Coptic
    (0x1F00, 0x1FFF),                     # Greek Extended
)


def publish_queued_clips(db, now: Optional[datetime] = None, uploader: Optional[Callable] = None) -> int:
    """Upload the single oldest clip whose time has come. Returns 1 when one went out.

    One clip per call, and the channel's own gap is checked first: five clips parked
    at the same minute still leave one per hour, because the queue would otherwise
    find the second one overdue three seconds later and fire it — the burst this
    whole path exists to prevent. A row with `publish_due_at` set is the request;
    the job's `auto_upload` flag decided whether to park it, and is not re-read here.
    """
    now = now or datetime.utcnow()
    _resolve_unresolved_attempts(db, now)
    if now < next_publish_slot(db, now=now):
        return 0
    row = db.query(Clip, Job).join(Job, Clip.job_id == Job.id).filter(
        Clip.publish_due_at.isnot(None),
        Clip.publish_due_at <= now,
        or_(Clip.youtube_video_id.is_(None), Clip.youtube_video_id == ""),
        # A row carrying an attempt that has not resolved is not waiting for its
        # turn: it is either mid-upload in another process or the survivor of a
        # crash, and neither wants a second upload.
        Clip.publish_attempt_at.is_(None),
    ).order_by(Clip.publish_due_at.asc()).first()
    if not row:
        return 0

    clip, job = row
    # The channel's upload budget is checked before the clip is claimed: an upload
    # that will be refused still costs the row its turn, and a claim that then sits
    # thirty minutes in the future is a clip that lost its slot for nothing.
    clear_at = next_budget_slot(db, now=now)
    if clear_at is not None:
        clip.publish_due_at = clear_at
        db.commit()
        job_warning(job.id, f"Clip #{clip.index} held back: {int(settings.UPLOAD_DAILY_BUDGET)} "
                            f"uploads already went out in the last 24 h. YouTube counts the "
                            f"channel's unpublished budget as roughly ten, so the queue stops "
                            f"short of it rather than learning it from a refusal. Trying again "
                            f"at {clear_at:%Y-%m-%d %H:%M} UTC, when the oldest one ages out.")
        return 0
    if not claim_clip_for_publish(db, clip.id, clip.publish_due_at, now=now):
        return 0
    db.expire_all()

    outcome = _ship_clip_to_youtube(db, clip, job, uploader, now)
    return 1 if outcome["status"] == "published" else 0


def _hold_clip_for_channel(db, clip, when: datetime, reason: str) -> dict:
    """Park a clip the channel is not ready for, instead of dropping the request.

    A click is consent to publish, so a refusal has to leave the clip queued for the
    moment the channel allows it - that is the unattended half of the same promise.
    An existing due time is never pulled earlier: the release schedule the queue
    wrote belongs to the channel, and `max` keeps both promises at once.
    """
    if clip.publish_due_at is None or clip.publish_due_at < when:
        clip.publish_due_at = when
    db.commit()
    job_warning(clip.job_id, f"Clip #{clip.index} held back from a manual publish: {reason}")
    return {"status": "held", "retry_at": clip.publish_due_at, "reason": reason}


def publish_clip_for_operator(db, clip, *, uploader: Optional[Callable] = None,
                              now: Optional[datetime] = None,
                              privacy_status: Optional[str] = None,
                              category_id: Optional[str] = None) -> dict:
    """Publish one clip because a human asked, under every guard the queue obeys.

    The Studio buttons used to call `services.youtube_upload.upload_clip_to_youtube`
    straight from the endpoint, and that bypassed all of it: no duplicate check (a
    double click put the short on the channel twice), no gap, no rolling budget, no
    claim, no release slot (clip 461 went public on click with
    `scheduled_publish_at IS NULL`), a `publish_due_at` left set behind a publish,
    and a strike charged for `uploadLimitExceeded` - the refusal the queue forgives
    cost clip 371 two of its three attempts.

    Outcomes are data, not exceptions, because the queue and the API read them
    differently: "published", "already_published", "unresolved_attempt", "held",
    "contended", "failed".
    """
    now = now or datetime.utcnow()
    job = db.query(Job).filter(Job.id == clip.job_id).first()
    if job is None:
        return {"status": "failed", "code": "no_job",
                "error": f"Job {clip.job_id} is gone; nothing was uploaded"}

    if clip.youtube_video_id or clip.uploaded:
        return {"status": "already_published", "video_id": clip.youtube_video_id}

    if clip.publish_attempt_at is not None:
        return {"status": "unresolved_attempt", "attempt_at": clip.publish_attempt_at}

    slot = next_publish_slot(db, now=now)
    if now < slot:
        return _hold_clip_for_channel(
            db, clip, slot,
            f"the channel's gap: the next upload may go out at {slot:%Y-%m-%d %H:%M} UTC")

    clear_at = next_budget_slot(db, now=now)
    if clear_at is not None:
        return _hold_clip_for_channel(
            db, clip, clear_at,
            f"{int(settings.UPLOAD_DAILY_BUDGET)} uploads already went out in the last 24 h; "
            f"the window clears at {clear_at:%Y-%m-%d %H:%M} UTC")

    if not claim_clip_for_publish(db, clip.id, clip.publish_due_at, now=now):
        return {"status": "contended"}
    db.expire_all()

    outcome = _ship_clip_to_youtube(db, clip, job, uploader, now,
                                   privacy_status=privacy_status,
                                   category_id=category_id)
    return outcome


def publish_clips_of_job_for_operator(db, job_id: int, *, uploader: Optional[Callable] = None,
                                      now: Optional[datetime] = None,
                                      privacy_status: Optional[str] = None,
                                      category_id: Optional[str] = None) -> dict:
    """The batch button on the queue's rules: at most one clip leaves now, the rest wait.

    `upload_all_clips_for_job` was called with `pace=False` on the reasoning that a
    human pressing "upload" asked for them now, which is the sentence that put five
    clips of job 91 on the channel inside four seconds. The request is honoured as
    consent to publish the job - every clip gets a release slot and a queue row -
    while the channel's gap still ships only the first one.
    """
    now = now or datetime.utcnow()
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job {job_id} not found")
    clips = db.query(Clip).filter(Clip.job_id == job_id).order_by(Clip.index.asc()).all()
    if not clips:
        raise ValueError(f"No clips found for Job {job_id}")

    enqueue_clips_for_publish(db, job_id, now=now, include_unreviewed=True)

    results = []
    counts = {"uploaded": 0, "queued": 0, "failed": 0, "skipped": 0}
    for clip in clips:
        db.refresh(clip)
        row = {"clip_id": clip.id, "clip_index": clip.index,
               "title": clip.title or f"Clip #{clip.index + 1}"}
        if clip.youtube_video_id:
            counts["skipped"] += 1
            row.update({"status": "uploaded", "video_id": clip.youtube_video_id,
                        "shorts_url": f"https://youtube.com/shorts/{clip.youtube_video_id}"})
            results.append(row)
            continue

        outcome = publish_clip_for_operator(db, clip, uploader=uploader, now=now,
                                            privacy_status=privacy_status,
                                            category_id=category_id)
        status = outcome["status"]
        if status == "published":
            res = outcome["result"]
            counts["uploaded"] += 1
            row.update({"status": "uploaded", "video_id": res.get("video_id"),
                        "shorts_url": res.get("shorts_url"),
                        "privacy_status": res.get("privacy_status"),
                        "publish_at": res.get("publish_at")})
        elif status == "held":
            counts["queued"] += 1
            row.update({"status": "queued",
                        "held_until": outcome["retry_at"].isoformat(timespec="seconds"),
                        "error": f"Held by the publish queue: {outcome['reason']}"})
        elif status == "already_published":
            counts["skipped"] += 1
            row.update({"status": "uploaded", "video_id": outcome["video_id"]})
        elif status == "unresolved_attempt":
            counts["failed"] += 1
            row.update({"status": "blocked",
                        "error": "an earlier publish attempt never recorded a video id; "
                                 "this clip may already be on YouTube"})
        elif status == "contended":
            counts["queued"] += 1
            row.update({"status": "queued",
                        "error": "another publish owns this clip right now"})
        else:
            counts["failed"] += 1
            row.update({"status": "failed", "error": outcome.get("error")})
        results.append(row)

    return {
        "success": counts["uploaded"] > 0,
        "job_id": job_id,
        "total_clips": len(clips),
        "uploaded_count": counts["uploaded"],
        "queued_count": counts["queued"],
        "failed_count": counts["failed"],
        "skipped_count": counts["skipped"],
        "results": results,
    }


async def stage_publish_all_clips(job_id: int, db: Session, job: Job, tiktok_privacy: str, update_progress: Callable[[str, int], None], lease_stop: Optional[threading.Event] = None) -> None:
    """Queue every rendered clip when the job asked for it on completion.

    Runs only for a job where every clip rendered: a partially failed job is
    left for a human to review, never swept into a channel automatically. YouTube
    clips are parked in the publish queue here instead of uploaded in place,
    because a sweep that uploads on the spot ignores the spacing — that is how five
    clips of job 91 went out inside four seconds. TikTok still publishes directly.
    """
    if getattr(job, "auto_upload", False):
        try:
            job_info(job_id, f"Auto-upload triggered for Job #{job_id}...")
            update_progress("⚡ Queuing clips for YouTube & TikTok...", 100)

            try:
                queued = enqueue_clips_for_publish(db, job_id)
                job_info(job_id, f"Queued {queued} clip(s) for YouTube, one publish every "
                                 f"{int(settings.UPLOAD_MIN_GAP_SECONDS // 60)} min")
            except Exception as yt_err:
                job_warning(job_id, f"YouTube queue step failed: {yt_err}")

            # Auto upload to TikTok
            try:
                # The sweep publishes every clip of the job on the spot and has no
                # per-clip filter, so it does not run when the job holds a window
                # no model chose. The per-clip step already sent the model picks.
                unreviewed = [c.index for c in db.query(Clip).filter(Clip.job_id == job_id).all()
                              if _needs_review_before_publish(c)]
                if unreviewed:
                    raise RuntimeError(
                        f"skipped: clip(s) {unreviewed} were not chosen by a model and need review")
                from services.tiktok_upload import upload_all_clips_to_tiktok_for_job
                job_info(job_id, f"Auto-uploading to TikTok as '{tiktok_privacy}'...")
                upload_all_clips_to_tiktok_for_job(job_id, db, privacy_level=tiktok_privacy,
                                                   pace=True, stop=lease_stop)
                job_info(job_id, f"TikTok auto-upload complete for Job #{job_id}")
            except Exception as tt_err:
                job_warning(job_id, f"TikTok auto-upload failed: {tt_err}")

        except Exception as auto_err:
            job_warning(job_id, f"Auto-upload execution encountered an error: {auto_err}")

