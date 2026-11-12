"""Startup migrations that go beyond adding a missing column.

``database.session._sync_columns`` owns columns. This module owns the one thing
a column cannot do: enforce a relationship *between* rows, which needs an index,
and an index that the live database cannot accept yet.

Why the caution: ``storage/app.db`` already contains clip rows that share a
``(job_id, index)`` slot - job 70 has three such pairs, each row carrying its
own ``youtube_video_id``, i.e. the same short twice on the channel. Those rows
are evidence, and un-deleting a published duplicate is a real-world action on
someone's channel, so no automated migration may rewrite or remove them. A
plain ``CREATE UNIQUE INDEX`` here would then fail on every boot of the service
and take the whole pipeline down over a hole it cannot close by itself.

So the rule for everything in this module: detect, report, and refuse. The
warning names the pairs and says what a human has to do; the rows stay exactly
where they are.
"""
import logging
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from sqlalchemy import inspect as sa_inspect, text

logger = logging.getLogger(__name__)

#: Declared on the model too (``Clip.__table_args__``) so a brand new database
#: gets it from ``create_all`` and this migration only has to care about the
#: databases that were already out there.
CLIP_UNIQUE_INDEX = "ux_clips_job_index"


@dataclass
class ClipSlotViolation:
    """One ``(job_id, index)`` slot that more than one row claims."""
    job_id: int
    index: int
    count: int
    row_ids: List[int] = field(default_factory=list)
    video_ids: List[str] = field(default_factory=list)


@dataclass
class MigrationReport:
    created: bool
    violations: List[ClipSlotViolation] = field(default_factory=list)
    note: Optional[str] = None


def _index_exists(conn, index_name: str) -> bool:
    return any(ix["name"] == index_name
               for ix in sa_inspect(conn).get_indexes("clips"))


def find_clip_slot_violations(conn) -> List[ClipSlotViolation]:
    """Slots with more than one row, without touching a single row.

    ``index`` is a reserved word, hence the quoting; SQLite's group_concat gives
    us the ids and the video ids in the same pass so the warning can be
    specific enough to act on.
    """
    rows = conn.execute(text(
        'SELECT job_id, `index`, COUNT(*) AS n, '
        '       GROUP_CONCAT(id) AS ids, '
        '       GROUP_CONCAT(youtube_video_id) AS vids '
        'FROM clips GROUP BY job_id, `index` HAVING n > 1 '
        'ORDER BY job_id, `index`'
    )).all()
    violations = []
    for row in rows:
        violations.append(ClipSlotViolation(
            job_id=int(row.job_id), index=int(row.index), count=int(row.n),
            row_ids=[int(p) for p in str(row.ids or "").split(",") if p],
            video_ids=[v for v in str(row.vids or "").split(",") if v],
        ))
    return violations


def describe_violations(violations, index_name: str = CLIP_UNIQUE_INDEX) -> str:
    """The operator-facing text: what is wrong, and what this code will not do."""
    pairs = "; ".join(
        f"job {v.job_id} index {v.index} has {v.count} rows "
        f"(ids {', '.join(str(i) for i in v.row_ids)}"
        + (f"; youtube_video_ids {', '.join(v.video_ids)}" if v.video_ids else "")
        + ")"
        for v in violations
    )
    return (
        f"Skipped CREATE UNIQUE INDEX {index_name} on clips(job_id, `index`): the "
        f"table already violates it - {pairs}. Those rows are evidence and more "
        "than one of them can be live on YouTube, so this migration will not "
        "rewrite or delete anything. A human must review each pair against the "
        "channel (yt-dlp / YouTube Data API, read-only), decide which row is the "
        "real publish, and reconcile the rest by hand; only then does the index "
        "get created, on the next start. Until then nothing at the database level "
        "stops a second worker re-recording a clip slot."
    )


def _report_warning(message: str) -> None:
    """Put the refusal where a human will actually find it.

    stdlib logging is what pytest captures and what lands on stderr; the
    rotating ``storage/logs/app.log`` is written by loguru, and it is the file
    the operator greps after a boot. A warning that only reaches one of the two
    is a warning nobody reads.
    """
    logger.warning(message)
    try:
        from loguru import logger as loguru_logger
        loguru_logger.warning(message)
    except Exception:  # pragma: no cover - loguru is a hard dependency
        pass


def ensure_clip_index_uniqueness(engine, *,
                                 warn: Callable[[str], None] = _report_warning,
                                 index_name: str = CLIP_UNIQUE_INDEX) -> MigrationReport:
    """Create the unique clip index, but only where the data already supports it."""
    known_tables = set(sa_inspect(engine).get_table_names())
    if "clips" not in known_tables:
        # A database this old has no rows to protect; create_all() owns the
        # whole table, index included, so there is nothing to migrate.
        return MigrationReport(created=False, note="no clips table to migrate")

    with engine.connect() as conn:
        if _index_exists(conn, index_name):
            return MigrationReport(created=True, note="index already present")

        violations = find_clip_slot_violations(conn)
        if violations:
            warn(describe_violations(violations, index_name))
            return MigrationReport(created=False, violations=violations,
                                   note="unique index refused: existing violations")

        conn.execute(text(
            f'CREATE UNIQUE INDEX "{index_name}" ON clips (job_id, `index`)'))
        conn.commit()

    return MigrationReport(created=True, note="index created")
