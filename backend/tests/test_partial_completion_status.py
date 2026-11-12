"""A job where 2 of 3 clips rendered is not a success, and is not nothing either.

The completion branch had two halves that disagreed. `status="completed"` was
written for a partially failed job too (worker.py, the `elif clip_errors:`
branch), so the Studio UI read green; and `stage_publish_all_clips` was only
reachable from the zero-errors branch, so the survivors of a partial run never
got the on-completion sweep. The operator saw a green job with a third of its
clips missing.

The fix splits the two concerns on purpose:

* the *label* becomes `completed_with_errors`: not green, and not one of the
  statuses the automatic paths treat as finished, so nothing downstream can
  read a half-rendered job as a clean one;
* the survivors still get *queued* - a pure database write of
  `publish_due_at` / `scheduled_publish_at`. Queueing is not publishing, and an
  auto-upload job already queued each clip the moment it rendered. What a
  partial job does NOT get is the completion sweep, which is the half that hands
  files to TikTok directly.

Every stage but the completion branch is stubbed here, and every upload is a
counter; nothing in this file can reach a channel.
"""
import os

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import text

import worker
from database.models import Clip, Job, JobProgress
from database.session import SessionLocal, init_database


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(JobProgress).delete()
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _queue_job(db, auto_upload=True, target_clips=3):
    job = Job(youtube_url="https://youtu.be/x", status="created",
              auto_upload=1 if auto_upload else 0, target_clips=target_clips,
              upload_privacy="unlisted")
    db.add(job)
    db.commit()
    return job.id


def _job_row(job_id):
    probe = SessionLocal()
    try:
        return probe.execute(
            text("SELECT status, error_message FROM jobs WHERE id=:jid"),
            {"jid": job_id}).one()
    finally:
        probe.close()


def _queued_count(job_id, indices=None):
    probe = SessionLocal()
    try:
        sql = ('SELECT COUNT(*) FROM clips WHERE job_id=:j '
               'AND publish_due_at IS NOT NULL')
        if indices is not None:
            sql += ' AND "index" IN (' + ",".join(str(i) for i in indices) + ")"
        return probe.execute(text(sql), {"j": job_id}).scalar()
    finally:
        probe.close()


@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    """Every stage stubbed except the completion branch itself.

    ``succeeds`` is the set of clip indices that render; the others append a
    ``clip_errors`` entry the way the real per-clip body does when ffmpeg dies.
    ``sweep`` counts the on-completion sweep, so a test can prove the partial
    path never reaches the code that publishes.
    """
    state = {"sweep": 0, "succeeds": {0, 2}, "total": 3}

    async def download(job_id, job, job_dir, update_progress):
        return str(tmp_path / "source.mp4")

    async def extract(job_id, job_dir, video_path, update_progress):
        return str(tmp_path / "audio.wav")

    async def transcribe(job_id, job, job_dir, audio_path, update_progress):
        return ([{"start": 0.0, "end": 5.0}], "segments.json", "words.json",
                "Source title", "Source channel")

    async def energy(job_id, video_path, segments, segments_file, update_progress):
        return segments

    async def temporal(job_id, video_path, segments, job_dir, update_progress):
        return []

    async def select(job_id, job, segments_file, source_title, update_progress,
                     temporal_events=None):
        return [{"start": 10.0 * i, "end": 10.0 * i + 20.0, "hook": f"h{i}"}
                for i in range(state["total"])]

    async def diarize(job_id, job_dir, video_path, clips, update_progress):
        return None

    async def process_clip(db, job, job_id, job_dir, index, clip, total_clips,
                           clip_errors, video_path, words_file, source_title,
                           source_channel, tiktok_privacy, update_progress,
                           lease_stop=None):
        if index not in state["succeeds"]:
            clip_errors.append({"index": index, "start": clip["start"],
                                "end": clip["end"], "error": "ffmpeg died"})
            return
        worker.record_clip(db, job_id, index, {
            "start": clip["start"], "end": clip["end"], "title": f"clip {index}",
            "file_path": f"jobs/{job_id}/clip_{index}/final_video.mp4",
        })

    async def counting_sweep(*args, **kwargs):
        state["sweep"] += 1

    for name, fn in {
        "stage_download_source": download,
        "stage_extract_audio": extract,
        "stage_transcribe": transcribe,
        "stage_score_audio_energy": energy,
        "stage_temporal_localization": temporal,
        "stage_select_clips": select,
        "stage_diarize": diarize,
        "process_clip": process_clip,
        "stage_publish_all_clips": counting_sweep,
        "_lease_heartbeat": lambda *a, **k: None,
    }.items():
        monkeypatch.setattr(worker, name, fn)

    return state


@pytest.mark.asyncio
async def test_partial_job_does_not_claim_to_be_completed(db, pipeline):
    job_id = _queue_job(db)

    await worker.process_job_async(job_id)

    status, error = _job_row(job_id)
    assert status == "completed_with_errors", f"got {status!r}"
    assert "1/3" in (error or ""), error


@pytest.mark.asyncio
async def test_surviving_clips_of_a_partial_job_reach_the_publish_queue(db, pipeline):
    job_id = _queue_job(db, auto_upload=True)

    await worker.process_job_async(job_id)

    assert _queued_count(job_id, indices=(0, 2)) == 2, "both survivors queued"
    assert _queued_count(job_id) == 2, "the failed clip has no row to queue"


@pytest.mark.asyncio
async def test_a_partial_job_is_never_handed_to_the_completion_sweep(db, pipeline):
    """The sweep is the half that publishes: TikTok uploads run inside it."""
    job_id = _queue_job(db, auto_upload=True)

    await worker.process_job_async(job_id)

    assert pipeline["sweep"] == 0


@pytest.mark.asyncio
async def test_a_clean_job_still_reads_completed_and_still_sweeps(db, pipeline):
    pipeline["succeeds"] = {0, 1, 2}
    job_id = _queue_job(db, auto_upload=True)

    await worker.process_job_async(job_id)

    status, error = _job_row(job_id)
    assert status == "completed"
    assert error is None
    assert pipeline["sweep"] == 1


@pytest.mark.asyncio
async def test_a_partial_job_without_auto_upload_queues_nothing(db, pipeline):
    """auto_upload=False is the operator saying no; nothing may be parked."""
    job_id = _queue_job(db, auto_upload=False)

    await worker.process_job_async(job_id)

    assert _queued_count(job_id) == 0
