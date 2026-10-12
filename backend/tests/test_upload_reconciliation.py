"""reconcile_uploads: the labels it puts on clips, and what --apply may change.

Two clips carry the id 'queued' - posted to an endpoint that no longer accepts
it, and the uploaded flag they set took them off every pending list forever. The
audit has to find those, tell them apart from clips that simply have nothing
rendered yet, and the repair has to be the least destructive thing that works.
"""
import os

import pytest
from sqlalchemy import text

from database.models import Clip, Job
from database.session import SessionLocal, init_database
from scripts import reconcile_uploads as rec


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _state(**kwargs) -> rec.ClipState:
    base = dict(id=1, job_id=1, index=0, uploaded=False, video_id=None,
                file_path=None, retry_count=0, job_status="completed")
    base.update(kwargs)
    return rec.ClipState(**base)


def _make_clip(db, root, *, index=0, status="completed", make_file=False,
               record_path=True, **clip_fields):
    """A job row, a clip row, and optionally the rendered file on disk.

    make_file records a consistent file_path unless record_path=False, so a test
    that only cares about the upload flag is not also triggering a path repair.
    """
    job = Job(youtube_url="https://youtu.be/x", status=status)
    db.add(job)
    db.flush()

    directory = os.path.join(root, "jobs", str(job.id), f"clip_{index}")
    os.makedirs(directory, exist_ok=True)
    if make_file:
        with open(os.path.join(directory, "final_video.mp4"), "wb") as f:
            f.write(b"0" * 10)
        if record_path and "file_path" not in clip_fields:
            clip_fields["file_path"] = os.path.join(
                "jobs", str(job.id), f"clip_{index}", "final_video.mp4")

    clip = Clip(job_id=job.id, index=index, start=0.0, end=5.0, **clip_fields)
    db.add(clip)
    db.commit()
    return clip


def _rendered(root, job_id, index, name="final_video.mp4") -> str:
    directory = os.path.join(root, "jobs", str(job_id), f"clip_{index}")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as f:
        f.write(b"0" * 10)
    return path


def _reload(db, clip_id):
    return db.execute(text("SELECT uploaded, youtube_video_id, file_path, upload_error "
                           "FROM clips WHERE id=:i"), {"i": clip_id}).one()


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------

def test_a_real_id_with_the_flag_is_published():
    report = rec.classify([_state(uploaded=True, video_id="Sy2BWsJnxb8")], "/", max_retries=3)
    assert report.clips[0].status == "published"


def test_the_queued_placeholder_is_not_published():
    report = rec.classify([_state(uploaded=True, video_id="queued")], "/", max_retries=3)
    assert report.clips[0].status == "placeholder_id"


def test_uploaded_without_any_id_is_reported_separately():
    report = rec.classify([_state(uploaded=True, video_id=None)], "/", max_retries=3)
    assert report.clips[0].status == "no_video_recorded"


def test_two_clips_sharing_one_id_are_flagged_as_duplicates():
    report = rec.classify([_state(id=1, uploaded=True, video_id="Sy2BWsJnxb8"),
                           _state(id=2, uploaded=True, video_id="Sy2BWsJnxb8")],
                          "/", max_retries=3)
    assert report.clips[0].status == "published"
    assert report.clips[1].status == "duplicate_id"


def test_an_id_without_the_upload_flag_is_its_own_problem():
    report = rec.classify([_state(uploaded=False, video_id="Sy2BWsJnxb8")], "/", max_retries=3)
    assert report.clips[0].status == "id_without_flag"


def test_a_pending_clip_with_a_file_is_uploadable(tmp_path):
    root = str(tmp_path)
    _rendered(root, 7, 0)
    report = rec.classify([_state(job_id=7, index=0)], root, max_retries=3)
    assert report.clips[0].status == "uploadable"


def test_a_pending_clip_with_no_file_has_nothing_to_upload(tmp_path):
    report = rec.classify([_state(job_id=7, index=0)], str(tmp_path), max_retries=3)
    assert report.clips[0].status == "nothing_to_upload"


def test_exhausted_retries_hide_a_clip_from_the_pending_endpoint(tmp_path):
    root = str(tmp_path)
    _rendered(root, 7, 0)
    clip = _state(job_id=7, index=0, retry_count=3)
    report = rec.classify([clip], root, max_retries=3)
    assert report.clips[0].status == "blocked_by_retries"


def test_a_stale_recorded_path_is_marked_for_repair(tmp_path):
    """The renderer moved final_with_sfx.mp4 to final_video.mp4; the row did not."""
    root = str(tmp_path)
    _rendered(root, 7, 0)
    clip = _state(job_id=7, index=0,
                  file_path=os.path.join("jobs", "7", "clip_0", "final_with_sfx.mp4"))
    report = rec.classify([clip], root, max_retries=3)
    assert report.clips[0].status == "uploadable"
    assert report.clips[0].path_needs_fix is True


def test_a_correct_recorded_path_is_not_marked_for_repair(tmp_path):
    root = str(tmp_path)
    _rendered(root, 7, 0)
    relative = os.path.join("jobs", "7", "clip_0", "final_video.mp4")

    report = rec.classify([_state(job_id=7, index=0, file_path=relative)], root, max_retries=3)
    assert report.clips[0].path_needs_fix is False


def test_a_missing_file_never_counts_as_a_path_fix(tmp_path):
    clip = _state(job_id=7, index=0, file_path=os.path.join("jobs", "7", "clip_0", "x.mp4"))
    report = rec.classify([clip], str(tmp_path), max_retries=3)
    assert report.clips[0].path_needs_fix is False


# ---------------------------------------------------------------------------
# verify_with_youtube()
# ---------------------------------------------------------------------------

def test_verification_relabels_an_id_youtube_does_not_have():
    report = rec.classify([_state(uploaded=True, video_id="Sy2BWsJnxb8")], "/", max_retries=3)
    rec.verify_with_youtube(report, lambda ids: {})
    assert report.clips[0].status == "id_not_on_youtube"
    assert "never uploaded" in report.clips[0].note


def test_verification_confirms_a_live_id():
    report = rec.classify([_state(uploaded=True, video_id="Sy2BWsJnxb8")], "/", max_retries=3)
    rec.verify_with_youtube(report, lambda ids: {
        "Sy2BWsJnxb8": {"privacy": "unlisted", "upload_status": "processed"}})
    assert report.clips[0].status == "published"
    assert "unlisted" in report.clips[0].note


def test_a_failed_verification_is_reported_not_raised():
    def broken(ids):
        raise PermissionError("YouTube is not authenticated")

    report = rec.classify([_state(uploaded=True, video_id="Sy2BWsJnxb8")], "/", max_retries=3)
    rec.verify_with_youtube(report, broken)
    assert report.clips[0].status == "published"
    assert any("not authenticated" in e for e in report.youtube_errors)


def test_verification_never_asks_about_placeholder_ids():
    calls = []

    def lookup(ids):
        calls.append(list(ids))
        return {}

    report = rec.classify([_state(uploaded=True, video_id="queued")], "/", max_retries=3)
    rec.verify_with_youtube(report, lookup)
    assert calls == []


# ---------------------------------------------------------------------------
# load_clips() and apply_repairs(), against a real database
# ---------------------------------------------------------------------------

def test_load_clips_reads_the_rows_and_their_job_status(db, tmp_path):
    root = str(tmp_path)
    placeholder = _make_clip(db, root, uploaded=True, youtube_video_id="queued", make_file=True)
    pending = _make_clip(db, root, index=1, uploaded=False, make_file=True)

    by_id = {c.id: c for c in rec.load_clips(db)}
    assert by_id[placeholder.id].video_id == "queued"
    assert by_id[pending.id].job_status == "completed"


def test_apply_rewrites_only_the_stale_path(db, tmp_path):
    root = str(tmp_path)
    clip = _make_clip(db, root, uploaded=True, youtube_video_id="Sy2BWsJnxb8",
                      file_path=os.path.join("jobs", "1", "clip_0", "gone.mp4"))
    _rendered(root, clip.job_id, 0)

    clips = rec.load_clips(db)
    rec.classify(clips, root, max_retries=3)
    target = [c for c in clips if c.id == clip.id]
    assert target[0].path_needs_fix is True

    paths, flags, errors = rec.apply_repairs(db, target, root, reset_placeholders=False)
    assert (paths, flags, errors) == (1, 0, [])

    row = _reload(db, clip.id)
    assert row.file_path.replace("\\", "/").endswith("clip_0/final_video.mp4")
    assert row.uploaded == 1, "a path repair never clears the upload flag"


def test_apply_fills_in_a_missing_path(db, tmp_path):
    """A clip row with no file_path still has a render on disk worth pointing at."""
    root = str(tmp_path)
    clip = _make_clip(db, root, uploaded=False, file_path=None, make_file=True,
                      record_path=False)

    clips = rec.load_clips(db)
    rec.classify(clips, root, max_retries=3)
    paths, flags, errors = rec.apply_repairs(db, clips, root, reset_placeholders=False)

    assert (paths, flags, errors) == (1, 0, [])
    assert _reload(db, clip.id).file_path.replace("\\", "/").endswith("final_video.mp4")


def test_apply_changes_nothing_without_the_reset_flag(db, tmp_path):
    root = str(tmp_path)
    clip = _make_clip(db, root, uploaded=True, youtube_video_id="queued", make_file=True)

    clips = rec.load_clips(db)
    rec.classify(clips, root, max_retries=3)
    paths, flags, errors = rec.apply_repairs(db, clips, root, reset_placeholders=False)

    assert (paths, flags, errors) == (0, 0, [])
    row = _reload(db, clip.id)
    assert row.uploaded == 1 and row.youtube_video_id == "queued"


def test_resetting_a_placeholder_returns_the_clip_to_the_pending_list(db, tmp_path):
    root = str(tmp_path)
    clip = _make_clip(db, root, uploaded=True, youtube_video_id="queued", make_file=True)

    clips = rec.load_clips(db)
    rec.classify(clips, root, max_retries=3)
    paths, flags, errors = rec.apply_repairs(db, clips, root, reset_placeholders=True)

    assert (paths, flags, errors) == (0, 1, [])
    row = _reload(db, clip.id)
    assert row.uploaded == 0
    assert row.youtube_video_id is None
    assert "reconcile" in row.upload_error


def test_reset_never_touches_a_clip_with_a_real_id(db, tmp_path):
    root = str(tmp_path)
    clip = _make_clip(db, root, uploaded=True, youtube_video_id="Sy2BWsJnxb8", make_file=True)

    clips = rec.load_clips(db)
    rec.classify(clips, root, max_retries=3)
    paths, flags, errors = rec.apply_repairs(db, clips, root, reset_placeholders=True)

    assert (paths, flags, errors) == (0, 0, [])
    assert _reload(db, clip.id).uploaded == 1


def test_load_clips_refuses_a_database_without_a_clips_table():
    """Fail closed: with no clip rows every upload looks missing."""
    class NoTables:
        def execute(self, *_args, **_kwargs):
            class Result:
                def all(self):
                    return []
            return Result()

    with pytest.raises(LookupError):
        rec.load_clips(NoTables())


# ---------------------------------------------------------------------------
# the report a human reads
# ---------------------------------------------------------------------------

def test_a_clean_database_says_so_instead_of_listing_everything():
    clips = [_state(id=1, uploaded=True, video_id="Sy2BWsJnxb8"),
             _state(id=2, uploaded=True, video_id="OTwJTM2wYJo")]
    report = rec.classify(clips, "/", max_retries=3)
    text = rec.format_report(report)

    assert "Nothing contradicts itself" in text
    assert "clip 1 " not in text


def test_contradictions_are_listed_and_plain_statistics_are_not(tmp_path):
    """placeholder_id is a lie in the database; "not uploaded yet" is not."""
    root = str(tmp_path)
    _rendered(root, 7, 1)
    clips = [_state(id=1, job_id=7, uploaded=True, video_id="queued"),
             _state(id=2, job_id=7, index=1)]  # uploadable, which is normal
    report = rec.classify(clips, root, max_retries=3)
    text = rec.format_report(report)

    assert "[placeholder_id]" in text
    assert "clip 2 " not in text
    assert "uploadable" in text  # still counted in the summary table


def test_the_stale_path_count_is_reported(tmp_path):
    root = str(tmp_path)
    _rendered(root, 7, 0)
    clip = _state(job_id=7, file_path=os.path.join("jobs", "7", "clip_0", "gone.mp4"))
    report = rec.classify([clip], root, max_retries=3)

    assert "1 clip(s) record a path that is not the rendered file" in \
        rec.format_report(report)
