"""Does the database's idea of what was published match what YouTube has?

342 clips are in the database and 181 of them are flagged uploaded. Two of those
carry the id 'queued' - a placeholder an old caller posted to the status endpoint,
which marked the clip published, recorded no video, and removed it from every
pending list without anything ever reaching YouTube. The endpoint validates ids
now, but the rows it let through are still there, and a clip whose file path
moved (the renderer writes final_video.mp4, older jobs kept final_with_sfx.mp4)
looks un-uploadable to the API even while the file sits in the clip directory.

Usage:
    python -m scripts.reconcile_uploads                     # local audit, changes nothing
    python -m scripts.reconcile_uploads --verify            # also ask YouTube about each id
    python -m scripts.reconcile_uploads --status placeholder_id
    python -m scripts.reconcile_uploads --apply             # rewrite stale file paths
    python -m scripts.reconcile_uploads --apply --reset-placeholders

--apply only ever repairs pointers and clears flags that were set on a lie. It
never marks a clip uploaded, and it never uploads anything: publishing stays a
human decision.
"""
import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import text

from config import settings
from database.session import SessionLocal

# YouTube ids are exactly 11 characters of [A-Za-z0-9_-]; same rule the API
# endpoint applies to incoming status updates.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Every status classify() can assign, so the report always shows the same rows.
STATUSES = ("published", "id_not_on_youtube", "placeholder_id", "no_video_recorded",
            "duplicate_id", "id_without_flag", "uploadable", "blocked_by_retries",
            "nothing_to_upload")

# The names a rendered clip can be saved under, newest pipeline first.
CLIP_FILE_NAMES = ("final_video.mp4", "final_short.mp4", "short.mp4")


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


@dataclass
class ClipState:
    id: int
    job_id: int
    index: int
    uploaded: bool
    video_id: Optional[str]
    file_path: Optional[str]
    retry_count: int
    job_status: str = ""
    status: str = ""
    note: str = ""
    resolved_path: Optional[str] = None   # the file that is actually there
    path_needs_fix: bool = False           # recorded path is not that file


@dataclass
class Report:
    clips: List[ClipState] = field(default_factory=list)
    youtube_errors: List[str] = field(default_factory=list)

    def by_status(self, status: str) -> List[ClipState]:
        return [c for c in self.clips if c.status == status]

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for clip in self.clips:
            out[clip.status] = out.get(clip.status, 0) + 1
        return out


def _tables(db) -> set:
    return {r.name for r in db.execute(
        text("SELECT name FROM sqlite_master WHERE type='table'")).all()}


def _columns(db, table: str) -> set:
    return {r.name for r in db.execute(
        text("SELECT name FROM pragma_table_info(:name)"), {"name": table}).all()}


def load_clips(db) -> List[ClipState]:
    """Every clip row, with the job status next to it.

    Raw SQL over the columns this script reads: the shipped app.db has not been
    through the startup column-sync for every new model field, and the ORM would
    ask for all of them.
    """
    if "clips" not in _tables(db):
        raise LookupError("the database has no 'clips' table")

    clip_cols = _columns(db, "clips")
    job_cols = _columns(db, "jobs") if "jobs" in _tables(db) else set()

    def col(name, fallback):
        return f'c."{name}"' if name in clip_cols else f"{fallback} AS {name}"

    select = [
        col("id", "0"), col("job_id", "NULL"), col("index", "0"),
        col("uploaded", "0"), col("youtube_video_id", "NULL"),
        col("file_path", "NULL"), col("upload_retry_count", "0"),
        'j."status" AS job_status' if "status" in job_cols else "'' AS job_status",
    ]

    query = "SELECT " + ", ".join(select) + " FROM clips c"
    if job_cols:
        query += " LEFT JOIN jobs j ON j.id = c.job_id"

    clips = []
    for row in db.execute(text(query)).all():
        clips.append(ClipState(
            id=row.id, job_id=row.job_id, index=row.index,
            uploaded=bool(row.uploaded), video_id=row.youtube_video_id,
            file_path=row.file_path, retry_count=row.upload_retry_count or 0,
            job_status=row.job_status or "",
        ))
    return clips


def _abs(storage_root: str, path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(storage_root, path)


def resolve_file(storage_root: str, clip: ClipState) -> Optional[str]:
    """The rendered file that exists for this clip, preferring the recorded path."""
    if clip.file_path:
        recorded = _abs(storage_root, clip.file_path)
        if os.path.isfile(recorded):
            return recorded
    directory = os.path.join(storage_root, "jobs", str(clip.job_id), f"clip_{clip.index}")
    for name in CLIP_FILE_NAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return candidate
    return None


def classify(clips: List[ClipState], storage_root: str, *, max_retries: int) -> Report:
    """Label every clip, and find the file that is really there while at it."""
    report = Report()
    seen_ids: Dict[str, int] = {}
    for clip in clips:
        if clip.video_id and VIDEO_ID_RE.match(clip.video_id):
            if clip.video_id in seen_ids:
                clip.status = "duplicate_id"
                clip.note = (f"clip {seen_ids[clip.video_id]} already claims "
                             f"{clip.video_id}")
            elif not clip.uploaded:
                clip.status = "id_without_flag"
                clip.note = f"has id {clip.video_id} but uploaded is False"
            else:
                seen_ids[clip.video_id] = clip.id
                clip.status = "published"
                clip.note = f"claimed as {clip.video_id}"
        elif clip.uploaded:
            # Marked published with something that is not a video id: the old
            # 'queued' placeholder, or an id that was simply never recorded.
            clip.status = "placeholder_id" if clip.video_id else "no_video_recorded"
            clip.note = (f"uploaded flag set with id {clip.video_id!r}; "
                         "no real video was ever recorded")
        else:
            clip.status = ""  # decided below, once the file is known

        clip.resolved_path = resolve_file(storage_root, clip)
        if clip.resolved_path:
            recorded = _norm(_abs(storage_root, clip.file_path)) if clip.file_path else None
            clip.path_needs_fix = recorded != _norm(clip.resolved_path)
        report.clips.append(clip)

    for clip in report.clips:
        if clip.status:
            continue
        if not clip.resolved_path:
            clip.status = "nothing_to_upload"
            clip.note = "no rendered file on disk"
        elif clip.retry_count >= max_retries:
            clip.status = "blocked_by_retries"
            clip.note = (f"{clip.retry_count} failed attempts hide it from "
                         "/clips/pending-upload")
        else:
            clip.status = "uploadable"
            clip.note = "not uploaded"
        if clip.path_needs_fix:
            clip.note += f"; recorded path is stale, file is {clip.resolved_path}"
    return report


def verify_with_youtube(report: Report, lookup) -> Report:
    """Fill in what YouTube says about each recorded id.

    `lookup` takes a list of ids and returns {id: {title, privacy, upload_status}};
    separated out so the classification can be tested without the Google client.
    """
    ids = sorted({c.video_id for c in report.clips if c.video_id and VIDEO_ID_RE.match(c.video_id)})
    if not ids:
        return report
    try:
        found = lookup(ids)
    except Exception as exc:
        report.youtube_errors.append(f"YouTube verification failed: {exc}")
        return report

    for clip in report.clips:
        if not clip.video_id or not VIDEO_ID_RE.match(clip.video_id):
            continue
        entry = found.get(clip.video_id)
        if entry is None:
            clip.note = f"YouTube has no video {clip.video_id} (deleted, or never uploaded)"
            if clip.status == "published":
                clip.status = "id_not_on_youtube"
        else:
            clip.note = (f"on YouTube as {entry.get('privacy') or '?'} / "
                         f"{entry.get('upload_status') or '?'}")
    return report


def youtube_lookup(video_ids: List[str]) -> Dict[str, dict]:
    """Ask the Data API which of these ids exist. Read-only."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from services.youtube_upload import SCOPES, get_token_path, get_youtube_auth_status

    if not get_youtube_auth_status()["authenticated"]:
        raise PermissionError("YouTube is not authenticated; connect the channel first")

    with open(get_token_path(), encoding="utf-8") as token_file:
        creds = Credentials.from_authorized_user_info(json.load(token_file), SCOPES)
    youtube = build("youtube", "v3", credentials=creds)

    found: Dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        batch = video_ids[start:start + 50]
        resp = youtube.videos().list(part="snippet,status", id=",".join(batch)).execute()
        for item in resp.get("items", []):
            found[item["id"]] = {
                "title": (item.get("snippet") or {}).get("title"),
                "privacy": (item.get("status") or {}).get("privacyStatus"),
                "upload_status": (item.get("status") or {}).get("uploadStatus"),
            }
    return found


def apply_repairs(db, clips: List[ClipState], storage_root: str, *,
                  reset_placeholders: bool) -> tuple:
    """Rewrite stale file paths, optionally clear flags that were set on a lie.

    Returns (paths repaired, flags cleared, errors).
    """
    paths_done = flags_cleared = 0
    errors = []

    for clip in clips:
        if clip.path_needs_fix and clip.resolved_path:
            relative = os.path.relpath(clip.resolved_path, storage_root)
            try:
                db.execute(text('UPDATE clips SET file_path=:p WHERE id=:c'),
                           {"p": relative, "c": clip.id})
                db.commit()
                clip.file_path = relative
                clip.path_needs_fix = False
                paths_done += 1
            except Exception as exc:
                db.rollback()
                errors.append(f"clip {clip.id} path: {exc}")

        if reset_placeholders and clip.status in ("placeholder_id", "no_video_recorded"):
            try:
                db.execute(text("UPDATE clips SET uploaded=0, youtube_video_id=NULL, "
                                "upload_error=:n WHERE id=:c"),
                           {"n": f"reconcile: cleared invalid upload flag ({clip.video_id!r})",
                            "c": clip.id})
                db.commit()
                clip.uploaded = False
                clip.video_id = None
                flags_cleared += 1
            except Exception as exc:
                db.rollback()
                errors.append(f"clip {clip.id} flag: {exc}")
    return paths_done, flags_cleared, errors


# Statuses worth reading line by line: the database claims something that is not
# true. The rest are queue statistics and are only summarised.
PROBLEM_STATUSES = ("placeholder_id", "no_video_recorded", "duplicate_id",
                    "id_without_flag", "id_not_on_youtube", "blocked_by_retries")


def format_report(report: Report, limit: int = 40, only: Optional[str] = None) -> str:
    lines = [f"{'status':<24}{'clips':>6}"]
    for status, count in sorted(report.counts().items(), key=lambda kv: -kv[1]):
        lines.append(f"{status:<24}{count:>6}")

    clips = report.by_status(only) if only else report.clips
    stale = [c for c in clips if c.path_needs_fix]
    if stale:
        lines.append("")
        lines.append(f"{len(stale)} clip(s) record a path that is not the rendered file; "
                     "--apply rewrites them.")

    problems = [c for c in clips if c.status in PROBLEM_STATUSES]
    lines.append("")
    if not problems:
        lines.append("Nothing contradicts itself: every clip flagged uploaded has a real "
                     "video id, and every id looks normal.")
    for clip in problems[:limit]:
        lines.append(f"  clip {clip.id} (job {clip.job_id}, index {clip.index}) "
                     f"[{clip.status}] {clip.note}")
        lines.append(f"      uploaded={clip.uploaded} id={clip.video_id!r} "
                     f"path={clip.file_path!r} job={clip.job_status}")
    if len(problems) > limit:
        lines.append(f"  ... {len(problems) - limit} more")

    for error in report.youtube_errors:
        lines.append(f"  verification: {error}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit the upload state of every clip (changes nothing without --apply).")
    parser.add_argument("--storage-root", default=settings.STORAGE_ROOT)
    parser.add_argument("--verify", action="store_true",
                        help="ask the YouTube Data API whether each recorded id still exists")
    parser.add_argument("--apply", action="store_true",
                        help="rewrite file paths that no longer point at the rendered file")
    parser.add_argument("--reset-placeholders", action="store_true",
                        help="with --apply: clear the uploaded flag on clips whose id is not a "
                             "video id, putting them back on the pending list")
    parser.add_argument("--status", default=None, help="only list clips in this status")
    parser.add_argument("--list-all", action="store_true")
    args = parser.parse_args(argv)

    db = SessionLocal()
    try:
        clips = load_clips(db)
    except LookupError as exc:
        print(f"Refusing to run: {exc}", file=sys.stderr)
        db.close()
        return 2

    report = classify(clips, args.storage_root, max_retries=settings.MAX_UPLOAD_RETRIES)

    if args.verify:
        report = verify_with_youtube(report, youtube_lookup)
        for error in report.youtube_errors:
            print(f"warning: {error}", file=sys.stderr)

    print(format_report(report, only=args.status,
                        limit=10 ** 9 if args.list_all else 40))

    target = report.by_status(args.status) if args.status else report.clips

    if args.apply:
        paths, flags, errors = apply_repairs(db, target, args.storage_root,
                                             reset_placeholders=args.reset_placeholders)
        print(f"\nRepaired {paths} file path(s), cleared {flags} invalid uploaded flag(s).")
        for error in errors:
            print(f"  failed: {error}", file=sys.stderr)
        if not args.reset_placeholders:
            print("Invalid upload flags were left in place; add --reset-placeholders to clear them.")
    else:
        print("\nRead-only run: nothing was changed. Use --apply to repair file paths.")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
