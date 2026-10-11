"""prune_storage: what it may delete, what it must not, and the dry-run default.

storage/jobs is 51 GB of mostly dead material, and a job directory is only dead
after its clips are safely elsewhere. A script that guesses wrong deletes the
output, so each rule is pinned here.
"""
import os
from datetime import datetime, timedelta, timezone

import pytest

from database.models import Clip, Job
from database.session import SessionLocal, init_database
from scripts import prune_storage as prune


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _make(path: str, size: int) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


def _utc_now() -> datetime:
    """Naive UTC, the way the models store it."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _paths(items):
    return {os.path.normpath(i.path) for i in items}


def _id(job_dir: str) -> int:
    return int(os.path.basename(job_dir))


@pytest.fixture
def tree(tmp_path, db):
    """Job directories covering every rule the script applies."""
    root = tmp_path / "jobs"
    now = _utc_now()

    def add_job(status, age_days):
        job = Job(youtube_url=f"https://youtu.be/{status}-{age_days}",
                  status=status, created_at=now - timedelta(days=age_days))
        db.add(job)
        db.flush()
        return str(root / str(job.id))

    # finished long ago, transcript written, one uploaded clip
    done = add_job("completed", 20)
    d = add_job("completed", 0)  # too new to prune
    running = add_job("processing", 30)
    failed = add_job("failed", 30)

    _make(os.path.join(done, "source.mp4"), 3000)
    _make(os.path.join(done, "audio.wav"), 1000)
    _make(os.path.join(done, "segments.json"), 10)
    _make(os.path.join(done, "words.json"), 10)
    uploaded_clip = _make(os.path.join(done, "clip_0", "final_video.mp4"), 500)
    db.add(Clip(job_id=_id(done), index=0, start=0, end=1, file_path=uploaded_clip,
                uploaded=True, youtube_video_id="abc123"))

    _make(os.path.join(d, "source.mp4"), 2000)
    _make(os.path.join(running, "source.mp4"), 5000)
    _make(os.path.join(running, "audio.wav"), 800)

    # a download that broke: fragments, plus an audio.wav with no transcript
    _make(os.path.join(failed, "source.f399.mp4"), 600)
    _make(os.path.join(failed, "source.mp4.part"), 300)
    _make(os.path.join(failed, "source.mp4"), 3000)
    _make(os.path.join(failed, "audio.wav"), 900)
    never_uploaded = _make(os.path.join(failed, "clip_0", "final_video.mp4"), 450)
    db.add(Clip(job_id=_id(failed), index=0, start=0, end=1,
                file_path=never_uploaded, uploaded=False))

    # directories with no job row: one nobody references, one a clip still does
    orphan = str(root / "424242")
    _make(os.path.join(orphan, "source.mp4"), 4000)
    _make(os.path.join(orphan, "audio.wav"), 700)
    referenced = str(root / "424243")
    _make(os.path.join(referenced, "source.mp4"), 600)
    db.add(Clip(job_id=424243, index=0, start=0, end=1, uploaded=False))

    db.commit()
    _make(str(root / "notes.txt"), 10)
    return {"root": str(root), "done": done, "recent": d, "running": running,
            "failed": failed, "orphan": orphan, "referenced": referenced}


def test_each_rule_finds_what_it_claims(tree):
    items, protected = prune.build_plan(tree["root"], older_than_days=7, keep_last=0)
    by_rule: dict = {}
    for item in items:
        by_rule.setdefault(item.rule, set()).add(os.path.normpath(item.path))

    assert by_rule["partial"] == {os.path.join(tree["failed"], "source.f399.mp4"),
                                  os.path.join(tree["failed"], "source.mp4.part")}
    # the orphan dir is pruned whole, so its wav is not also listed; the failed
    # job's wav has no transcript yet, so it is the only material worth keeping
    assert by_rule["audio"] == {os.path.join(tree["done"], "audio.wav")}
    assert by_rule["orphan"] == {os.path.normpath(tree["orphan"])}
    assert by_rule["source"] == {os.path.join(tree["done"], "source.mp4"),
                                 os.path.join(tree["failed"], "source.mp4")}
    assert _paths(protected) == {os.path.normpath(tree["referenced"])}


def test_active_and_recent_jobs_are_never_pruned(tree):
    items, _ = prune.build_plan(tree["root"], older_than_days=7, keep_last=0)
    paths = _paths(items)

    assert os.path.join(tree["running"], "source.mp4") not in paths
    assert os.path.join(tree["running"], "audio.wav") not in paths
    assert os.path.join(tree["recent"], "source.mp4") not in paths


def test_keep_last_spares_the_newest_finished_job(tree):
    items, _ = prune.build_plan(tree["root"], older_than_days=7, keep_last=1)

    assert os.path.join(tree["done"], "source.mp4") not in _paths(items)
    assert os.path.join(tree["failed"], "source.mp4") in _paths(items)


def test_only_uploaded_clips_are_candidates(tree):
    items, _ = prune.build_plan(tree["root"], older_than_days=7, keep_last=0,
                                include_uploaded_clips=True)
    paths = _paths(items)

    assert os.path.join(tree["done"], "clip_0", "final_video.mp4") in paths
    assert os.path.join(tree["failed"], "clip_0", "final_video.mp4") not in paths


def test_clips_are_left_alone_without_the_flag(tree):
    items, _ = prune.build_plan(tree["root"], older_than_days=7, keep_last=0)

    assert not any(i.rule == "uploaded-clip" for i in items)


def test_refuses_a_database_without_jobs(tmp_path):
    """Fail closed: with no rows to check, every directory looks orphaned."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    session = sessionmaker(bind=engine)()

    with pytest.raises(LookupError):
        prune.load_state(session)


def test_dry_run_deletes_nothing(tree, capsys):
    code = prune.main(["--jobs-dir", tree["root"], "--older-than-days", "7", "--keep-last", "0"])
    out = capsys.readouterr().out

    assert code == 0
    assert "Would delete" in out
    assert "protected:" in out
    assert os.path.isfile(os.path.join(tree["orphan"], "source.mp4"))
    assert os.path.isfile(os.path.join(tree["done"], "audio.wav"))


def test_apply_removes_only_the_planned_files(tree, capsys):
    code = prune.main(["--apply", "--jobs-dir", tree["root"], "--older-than-days", "7",
                       "--keep-last", "0"])
    capsys.readouterr()

    assert code == 0
    assert not os.path.exists(tree["orphan"])
    assert not os.path.exists(os.path.join(tree["done"], "source.mp4"))
    assert not os.path.exists(os.path.join(tree["done"], "audio.wav"))
    assert os.path.isfile(os.path.join(tree["failed"], "audio.wav"))  # no transcript yet
    assert not os.path.exists(os.path.join(tree["failed"], "source.f399.mp4"))
    assert os.path.isfile(os.path.join(tree["done"], "clip_0", "final_video.mp4"))
    assert os.path.isfile(os.path.join(tree["running"], "source.mp4"))
    assert os.path.isdir(tree["referenced"])
    assert os.path.isfile(os.path.join(tree["root"], "notes.txt"))
