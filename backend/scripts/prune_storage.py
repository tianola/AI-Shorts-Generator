"""Reclaim space from finished jobs. Reports first, deletes only when told.

The jobs tree holds 51 GB, and most of it is material a finished job no longer
needs: full-length source videos (job 80 is 17 GB of a 3-hour VOD), wav files
the transcriber already consumed, fragments of interrupted downloads, and
directories whose job row is long gone.

Usage:
    python -m scripts.prune_storage                     # report only
    python -m scripts.prune_storage --apply             # delete that report
    python -m scripts.prune_storage --older-than-days 3 --keep-last 2
    python -m scripts.prune_storage --include-uploaded-clips --apply

Nothing here touches a job that is still queued or running, and a clip that was
never uploaded is never a candidate: it is the only copy of the output.
"""
import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from config import settings
from database.session import SessionLocal

# A job in one of these states may still be worked on, so its files stay put.
ACTIVE_STATUSES = {"created", "queued", "pending", "processing"}
FINISHED_STATUSES = {"completed", "failed"}

PARTIAL_SUFFIXES = (".part", ".ytdl")
PARTIAL_PREFIXES = ("source.f",)
TRANSCRIPT_INPUTS = ("audio.wav", "audio_groq.mp3")


@dataclass
class Item:
    path: str
    size: int
    rule: str
    reason: str


@dataclass
class JobRow:
    id: int
    status: str
    created_at: datetime


@dataclass
class ClipRow:
    id: int
    job_id: int
    index: int
    file_path: str
    uploaded: bool
    youtube_video_id: str


def _tables(db) -> set:
    rows = db.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).all()
    return {r.name for r in rows}


def _columns(db, table: str) -> set:
    rows = db.execute(text("SELECT name FROM pragma_table_info(:name)"), {"name": table}).all()
    return {r.name for r in rows}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            total += _size(os.path.join(root, name))
    return total


def _is_partial(name: str) -> bool:
    return name.endswith(PARTIAL_SUFFIXES) or name.startswith(PARTIAL_PREFIXES)


def _has_transcript(job_dir: str) -> bool:
    for name in ("segments.json", "words.json"):
        path = os.path.join(job_dir, name)
        if _size(path) <= 2:
            return False
    return True


def _parse_ts(value):
    """Raw SQL hands back SQLite's TEXT, the ORM would have parsed it."""
    if value is None or isinstance(value, datetime):
        return value
    text_value = str(value).strip()
    try:
        return datetime.fromisoformat(text_value)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text_value, fmt)
            except ValueError:
                continue
    return None


def _age_days(created_at) -> float:
    if created_at is None:
        return float(10_000)
    now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        # The models write naive UTC timestamps, so read them as UTC.
        created_at = created_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - created_at).total_seconds() / 86400.0)


def _job_dirs(jobs_dir: str) -> list:
    try:
        entries = os.listdir(jobs_dir)
    except OSError:
        return []
    return [e for e in sorted(entries, key=lambda x: (len(x), x))
            if os.path.isdir(os.path.join(jobs_dir, e))]


def plan_partials(jobs_dir: str) -> list:
    """Fragments of downloads that never completed."""
    items = []
    for entry in _job_dirs(jobs_dir):
        job_dir = os.path.join(jobs_dir, entry)
        for name in sorted(os.listdir(job_dir)):
            path = os.path.join(job_dir, name)
            if os.path.isfile(path) and _is_partial(name):
                items.append(Item(path, _size(path), "partial", "interrupted download"))
    return items


def plan_transcript_inputs(jobs_dir: str) -> list:
    """The wav the transcriber already read, kept only because nothing deleted it."""
    items = []
    for entry in _job_dirs(jobs_dir):
        job_dir = os.path.join(jobs_dir, entry)
        if not _has_transcript(job_dir):
            continue
        for name in TRANSCRIPT_INPUTS:
            path = os.path.join(job_dir, name)
            if os.path.isfile(path):
                items.append(Item(path, _size(path), "audio", "transcript already written"))
    return items


def plan_orphans(jobs_dir: str, job_ids: set, clip_job_ids: set) -> tuple:
    """(deletable directories with no database row, directories left alone)."""
    deletable, protected = [], []
    for entry in _job_dirs(jobs_dir):
        if not entry.isdigit():
            continue
        job_id = int(entry)
        if job_id in job_ids:
            continue
        path = os.path.join(jobs_dir, entry)
        size = _dir_size(path)
        if job_id in clip_job_ids:
            protected.append(Item(path, size, "orphan-protected",
                                  "no job row, but clip rows still point here"))
        else:
            deletable.append(Item(path, size, "orphan", "no job row, no clip rows"))
    return deletable, protected


def plan_finished_sources(jobs_dir: str, jobs: dict, clips: dict, *, older_than_days: int,
                          keep_last: int) -> list:
    """Source videos of jobs that are finished, old, and not among the newest."""
    eligible = [
        j for j in jobs.values()
        if j.status in FINISHED_STATUSES and _age_days(j.created_at) >= older_than_days
    ]
    eligible.sort(key=lambda j: _age_days(j.created_at))  # newest first
    keep = {j.id for j in eligible[:keep_last]} if keep_last > 0 else set()

    items = []
    for job in eligible:
        if job.id in keep:
            continue
        path = os.path.join(jobs_dir, str(job.id), "source.mp4")
        if not os.path.isfile(path):
            continue
        pending = sum(1 for c in clips.get(job.id, ()) if not c.uploaded)
        items.append(Item(path, _size(path), "source",
                          f"job {job.id} {job.status} after {_age_days(job.created_at):.0f} days, "
                          f"{pending} clip(s) never uploaded"))
    return items


def plan_uploaded_clips(jobs_dir: str, jobs: dict, clips: dict) -> list:
    """Rendered clips YouTube already has. Opt-in, because it is the only copy locally."""
    items = []
    for job_id in sorted(clips):
        job = jobs.get(job_id)
        if job is None or job.status in ACTIVE_STATUSES:
            continue
        for clip in clips[job_id]:
            if not clip.uploaded:
                continue
            path = clip.file_path or os.path.join(jobs_dir, str(job_id),
                                                  f"clip_{clip.index}", "final_video.mp4")
            if os.path.isfile(path):
                items.append(Item(path, _size(path), "uploaded-clip",
                                  f"clip {clip.id} uploaded as {clip.youtube_video_id or 'id unknown'}"))
    return items


def load_state(db) -> tuple:
    """Read the job and clip rows this script needs.

    Deliberately raw SQL over a handful of columns: the ORM would ask for every
    mapped field, and a database that has not been through the app's startup
    column-sync yet (the shipped app.db predates claimed_by) then fails a script
    that only wanted to know which directories are dead.
    """
    if "jobs" not in _tables(db):
        # Fail closed: without the job rows every directory looks orphaned, and
        # --apply would delete the whole tree.
        raise LookupError("the database has no 'jobs' table")

    jobs = {
        row.id: JobRow(id=row.id, status=row.status or "", created_at=_parse_ts(row.created_at))
        for row in db.execute(text("SELECT id, status, created_at FROM jobs")).all()
    }
    clips: dict = {}
    clip_cols = _columns(db, "clips")
    if clip_cols:
        def col(name, fallback):
            return f'"{name}"' if name in clip_cols else f"{fallback} AS {name}"

        query = (
            "SELECT " + ", ".join([
                col("id", "0"), col("job_id", "NULL"), col("index", "0"),
                col("file_path", "NULL"), col("uploaded", "0"),
                col("youtube_video_id", "NULL"),
            ]) + " FROM clips"
        )
        for row in db.execute(text(query)).all():
            if row.job_id is None:
                continue
            clips.setdefault(row.job_id, []).append(
                ClipRow(id=row.id, job_id=row.job_id, index=row.index,
                        file_path=row.file_path, uploaded=bool(row.uploaded),
                        youtube_video_id=row.youtube_video_id)
            )
    return jobs, clips


def build_plan(jobs_dir: str, *, older_than_days: int = 7, keep_last: int = 2,
               include_uploaded_clips: bool = False) -> tuple:
    """(items to delete, items deliberately protected)."""
    db = SessionLocal()
    try:
        jobs, clips = load_state(db)
    finally:
        db.close()

    clip_job_ids = {c.job_id for rows in clips.values() for c in rows}
    orphans, protected = plan_orphans(jobs_dir, set(jobs), clip_job_ids)
    dead = {os.path.basename(i.path) for i in orphans}

    def outside_dead_dir(item: Item) -> bool:
        return os.path.basename(os.path.dirname(item.path)) not in dead

    items = [i for i in plan_partials(jobs_dir) if outside_dead_dir(i)]
    items += [i for i in plan_transcript_inputs(jobs_dir) if outside_dead_dir(i)]
    items += orphans
    items += plan_finished_sources(jobs_dir, jobs, clips,
                                   older_than_days=older_than_days, keep_last=keep_last)
    if include_uploaded_clips:
        items += plan_uploaded_clips(jobs_dir, jobs, clips)
    return items, protected


def execute(items: list) -> tuple:
    """Delete every item. Returns (bytes freed, files removed, errors)."""
    freed = removed = 0
    errors = []
    for item in items:
        try:
            if os.path.isdir(item.path):
                shutil.rmtree(item.path)
            else:
                os.remove(item.path)
        except OSError as exc:
            errors.append(f"{item.path}: {exc}")
            continue
        freed += item.size
        removed += 1
    return freed, removed, errors


def _fmt(nbytes: int) -> str:
    value = float(nbytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024:
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def format_report(items: list, protected: list, *, applied: bool, limit: int = 30) -> str:
    if not items:
        lines = ["Nothing to prune: every byte in the jobs tree is still in use."]
    else:
        by_rule: dict = {}
        for item in items:
            row = by_rule.setdefault(item.rule, [0, 0])
            row[0] += 1
            row[1] += item.size
        lines = [f"{'rule':<12}{'files':>7}{'size':>12}"]
        for rule, (n, size) in sorted(by_rule.items(), key=lambda kv: -kv[1][1]):
            lines.append(f"{rule:<12}{n:>7}{_fmt(size):>12}")
        lines.append("")
        ordered = sorted(items, key=lambda i: -i.size)
        for item in ordered[:limit]:
            lines.append(f"  {_fmt(item.size):>9}  [{item.rule}] {item.path}")
            lines.append(f"              {item.reason}")
        if len(ordered) > limit:
            rest = sum(i.size for i in ordered[limit:])
            lines.append(f"  ... {len(ordered) - limit} more, {_fmt(rest)}")
        total = sum(i.size for i in items)
        verb = "Deleted" if applied else "Would delete"
        lines.append("")
        lines.append(f"{verb} {len(items)} file(s), {_fmt(total)}")
        if not applied:
            lines.append("Re-run with --apply to delete these.")
    for item in protected:
        lines.append(f"  protected: {item.path} ({_fmt(item.size)}) - {item.reason}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Reclaim storage from finished jobs (dry run unless --apply).")
    parser.add_argument("--apply", action="store_true",
                        help="delete what is listed; without it this only reports")
    parser.add_argument("--jobs-dir", default=settings.JOBS_DIR)
    parser.add_argument("--older-than-days", type=int, default=7,
                        help="minimum age before a finished job's source video is a candidate")
    parser.add_argument("--keep-last", type=int, default=2,
                        help="never prune this many most recent finished jobs")
    parser.add_argument("--include-uploaded-clips", action="store_true",
                        help="also prune clips that YouTube already has")
    parser.add_argument("--list-all", action="store_true",
                        help="print every file instead of the 30 largest")
    args = parser.parse_args(argv)

    if not os.path.isdir(args.jobs_dir):
        print(f"No jobs directory at {args.jobs_dir}", file=sys.stderr)
        return 2

    limit = 10**9 if args.list_all else 30
    try:
        items, protected = build_plan(args.jobs_dir, older_than_days=args.older_than_days,
                                      keep_last=args.keep_last,
                                      include_uploaded_clips=args.include_uploaded_clips)
    except LookupError as exc:
        print(f"Refusing to run: {exc}", file=sys.stderr)
        return 2

    if not args.apply:
        print(format_report(items, protected, applied=False, limit=limit))
        return 0

    freed, removed, errors = execute(items)
    print(format_report(items, protected, applied=True, limit=limit))
    if removed != len(items):
        print(f"Only {removed} of {len(items)} removed, {_fmt(freed)} actually reclaimed",
              file=sys.stderr)
    for error in errors:
        print(f"  could not remove {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
