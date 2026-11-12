"""One clip row per (job, index) - enforced, not assumed.

`record_clip` reads-then-inserts, which is a promise that holds only while one
worker is writing. Job 70 has three pairs of rows for the same slots (214/232,
215/233, 216/234), each pair carrying a different `youtube_video_id`, i.e. the
same short published twice on the channel. Job 93 is the other shape of the same
hole: its rows run index 1..9 while `storage/jobs/93/clip_0/final_video.mp4`
sits on disk with no row pointing at it, never published - clip 1's write landed
in the row clip 0 had just created (app.log 08:42:36 and 08:44:20 both say
`saved to DB with ID 376`).

The production database already violates the constraint, and those rows are
evidence: un-deleting a duplicate is a real-world channel action. So the
migration must detect, report and skip - never rewrite. And because the
constraint is what a second worker would trip, `record_clip` has to turn that
trip into "reuse the existing row" instead of killing a 40-minute render at the
moment it writes its result.
"""
from datetime import datetime

import pytest
from sqlalchemy import create_engine, inspect as sa_inspect, text

from database.models import Clip, Job
from database.session import SessionLocal, init_database

import worker

UNIQUE_INDEX = "ux_clips_job_index"

# The legacy shape, i.e. what storage/app.db actually looks like today: the
# columns arrive one ALTER at a time and no index ever covered the pair.
LEGACY_CLIPS_DDL = """
CREATE TABLE clips (
    id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    "index" INTEGER NOT NULL,
    start FLOAT NOT NULL,
    "end" FLOAT NOT NULL,
    title VARCHAR,
    file_path VARCHAR,
    uploaded BOOLEAN,
    youtube_video_id VARCHAR,
    tiktok_video_id VARCHAR,
    publish_due_at DATETIME,
    created_at DATETIME,
    PRIMARY KEY (id)
)
"""


@pytest.fixture
def legacy_db(tmp_path):
    """A throwaway file database with the production schema, nothing shared."""
    url = f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text(LEGACY_CLIPS_DDL))
    yield engine
    engine.dispose()


def _insert_clip(engine, row_id, job_id, index, video_id=None):
    with engine.begin() as conn:
        conn.execute(
            text('INSERT INTO clips (id, job_id, "index", start, "end", title, '
                 'uploaded, youtube_video_id, created_at) '
                 'VALUES (:id, :job, :idx, :start, :end, :title, 1, :vid, :now)'),
            {"id": row_id, "job": job_id, "idx": index, "start": 10.0 * index,
             "end": 20.0 * index, "title": f"clip {index}", "vid": video_id,
             "now": datetime(2026, 9, 17, 13, 40)},
        )


def _index_names(engine):
    return {ix["name"] for ix in sa_inspect(engine).get_indexes("clips")}


# ---------------------------------------------------------------------------
# the migration
# ---------------------------------------------------------------------------

def test_a_clean_table_gets_the_unique_index(legacy_db):
    from database.migrations import ensure_clip_index_uniqueness

    _insert_clip(legacy_db, 214, 70, 0, "Lx1Eb12sWAs")
    _insert_clip(legacy_db, 215, 70, 1, "Gp96VYc4WTE")

    report = ensure_clip_index_uniqueness(legacy_db)

    assert report.created is True
    assert UNIQUE_INDEX in _index_names(legacy_db)


def test_the_index_blocks_a_second_row_for_the_same_slot(legacy_db):
    from database.migrations import ensure_clip_index_uniqueness

    _insert_clip(legacy_db, 214, 70, 0, "Lx1Eb12sWAs")
    ensure_clip_index_uniqueness(legacy_db)

    with pytest.raises(Exception) as excinfo:
        with legacy_db.begin() as conn:
            conn.execute(
                text('INSERT INTO clips (id, job_id, "index", start, "end") '
                     "VALUES (999, 70, 0, 1.0, 2.0)"))

    assert "UNIQUE constraint failed" in str(excinfo.value)


def test_violating_rows_are_left_exactly_as_they_are(legacy_db):
    """The trap: the live database already breaks the rule, so a naive CREATE
    UNIQUE INDEX fails on the operator's machine on every boot."""
    from database.migrations import ensure_clip_index_uniqueness

    _insert_clip(legacy_db, 214, 70, 0, "Lx1Eb12sWAs")
    _insert_clip(legacy_db, 232, 70, 0, "ab5jGMdDxHA")
    _insert_clip(legacy_db, 215, 70, 1, "Gp96VYc4WTE")

    report = ensure_clip_index_uniqueness(legacy_db)

    assert report.created is False
    assert UNIQUE_INDEX not in _index_names(legacy_db)
    assert (report.violations[0].job_id, report.violations[0].index,
            report.violations[0].count) == (70, 0, 2)
    with legacy_db.connect() as conn:
        rows = conn.execute(
            text('SELECT id, youtube_video_id FROM clips WHERE job_id=70 AND "index"=0 '
                 "ORDER BY id")).all()
    assert rows == [(214, "Lx1Eb12sWAs"), (232, "ab5jGMdDxHA")], "no rewrite, no delete"


def test_the_warning_names_the_pairs_and_the_human_step(legacy_db, caplog):
    import logging
    from database.migrations import ensure_clip_index_uniqueness

    _insert_clip(legacy_db, 214, 70, 0, "Lx1Eb12sWAs")
    _insert_clip(legacy_db, 232, 70, 0, "ab5jGMdDxHA")

    with caplog.at_level(logging.WARNING):
        ensure_clip_index_uniqueness(legacy_db)

    message = " ".join(r.getMessage() for r in caplog.records)
    assert "ux_clips_job_index" in message
    assert "job 70 index 0" in message
    assert "youtube" in message.lower() and "review" in message.lower()


def test_a_database_without_a_clips_table_is_not_an_error(legacy_db):
    from database.migrations import ensure_clip_index_uniqueness

    with legacy_db.begin() as conn:
        conn.execute(text("DROP TABLE clips"))

    report = ensure_clip_index_uniqueness(legacy_db)

    assert report.created is False and report.violations == []


def test_startup_migration_runs_it_and_keeps_the_index_idempotent(legacy_db):
    """Second boot on a database that already has the index must not fail."""
    from database.migrations import ensure_clip_index_uniqueness

    _insert_clip(legacy_db, 214, 70, 0, "Lx1Eb12sWAs")
    ensure_clip_index_uniqueness(legacy_db)

    assert ensure_clip_index_uniqueness(legacy_db).created is True


def test_init_database_installs_the_index_on_a_fresh_database():
    init_database()

    assert UNIQUE_INDEX in {
        ix["name"] for ix in sa_inspect(SessionLocal().bind).get_indexes("clips")
    }


# ---------------------------------------------------------------------------
# record_clip against the constraint
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _values(title):
    return {"start": 462.0, "end": 483.0, "title": title,
            "file_path": "jobs/88/clip_0/final_video.mp4"}


class _RacingSession:
    """Delegates to a real session and slips a rival INSERT in at the moment
    record_clip stages its own - the exact window the read-then-insert leaves."""

    def __init__(self, real, rival):
        self._real = real
        self._rival = rival
        self._fired = False

    def add(self, obj):
        if not self._fired:
            self._fired = True
            self._real.add(self._rival)
            self._real.commit()
        return self._real.add(obj)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_record_clip_survives_a_row_another_worker_inserted_first(db):
    """A 40-minute render must not die on the write that reports its result."""
    _job(db)
    rival = Clip(job_id=88, index=0, start=1.0, end=2.0, title="rival",
                 uploaded=True, youtube_video_id="ALREADY_ON_YOUTUBE",
                 upload_retry_count=0)
    racing = _RacingSession(db, rival)

    clip = worker.record_clip(racing, 88, 0, _values("this run"))

    assert clip.title == "this run", "the row still has to follow this run"
    assert clip.youtube_video_id == "ALREADY_ON_YOUTUBE", "and keep its publish identity"
    assert db.query(Clip).filter(Clip.job_id == 88, Clip.index == 0).count() == 1


def _job(db):
    db.add(Job(id=88, youtube_url="https://youtu.be/x", status="processing",
               target_clips=1, auto_upload=0))
    db.commit()
