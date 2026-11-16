"""
Worker process for AI Shorts Generator.
Polling-based job processor with comprehensive error handling and logging.
Uses async/await for Cloud API calls.
"""
import os
import unicodedata

# Must run before any import that pulls in loguru: see utils/daemon_streams.py
from utils.daemon_streams import redirect_daemon_streams

redirect_daemon_streams("worker")

import time
import json
import asyncio
import socket
import random
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import func, or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from database.session import SessionLocal, init_database
from database.models import Job, Clip, JobProgress
from config import settings
from loguru import logger
from utils.logger import job_info, job_error, job_warning, job_debug
from utils.env_check import log_media_tools, missing_media_tools
from utils.disk import has_headroom, reclaim
from utils import derived_cache
from utils import clip_trim
from utils import upload_pacing
from services.youtube import download_youtube
from services.audio import extract_audio
from services.whisper import transcribe_audio_file
from services.clip_selector import select_clips
from services.audio_energy_scorer import score_segments as score_audio_energy
from services.smart_crop import smart_crop_video
from services.word_cropper import crop_words
from services.captions import create_captions
from services.dimension_checker import check_dimensions, log_dimension_warning
from services.render import burn_captions
from services.metadata import derive_title_from_text, generate_metadata
from services.thumbnail import generate_thumbnail
from services.sfx import add_automatic_sfx

_REDIS_CLIENT = None
_REDIS_CHECKED = False

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"

# --- stall watchdog state ---------------------------------------------------
# The main poll loop runs process_job() synchronously; if a stage blocks forever
# (a wedged network call, a lock, a subprocess that never returns) the loop never
# returns to poll, and the lease heartbeat keeps the job looking alive - so the
# queue freezes silently (the job-99 failure). _MAIN_TICK is bumped on every real
# progress signal and _CURRENT_JOB names the in-flight job; the watchdog thread
# reads both and, when a job has produced no signal past the timeout, fails that
# job and exits the process so NSSM relaunches a clean worker for the rest.
_MAIN_TICK = [0.0]
_MAIN_PROC = [0.0]
_CURRENT_JOB = [None]


def _touch_main_tick() -> None:
    _MAIN_TICK[0] = time.time()
    _MAIN_PROC[0] = time.process_time()


# An upload-pacing gap is a bounded sleep that can legally outlast the stall
# window (UPLOAD_MIN_GAP_SECONDS + jitter = 4200 s against 30 min), and it burns
# no CPU - which is precisely the signature the watchdog kills on. Hand the
# pacer our liveness signal so each chunk that wakes up reports itself; a wait
# that never wakes still never ticks, so a real wedge keeps getting caught.
upload_pacing.set_liveness_hook(_touch_main_tick)


def _should_stall_exit(current_job_id, last_tick: float, now: float,
                       timeout_seconds: float, cpu_seconds: float = 0.0,
                       min_cpu_seconds: float = 120.0) -> bool:
    """True when a job is in flight, has no progress tick within the timeout,
    AND the process burned negligible CPU over that same window.

    Idle polling (no current job) is healthy and never counts as a stall.

    The CPU test is what keeps a legitimately-slow compute stage alive: pyannote
    diarization on an hour of audio, or YOLO temporal localization, run for far
    longer than the timeout emitting only a single start tick, yet keep crunching
    CPU. A genuine wedge (the job-99 failure) is a blocked main thread that
    produces neither ticks nor CPU - that is the only case we kill on.
    """
    if current_job_id is None:
        return False
    if (now - last_tick) <= timeout_seconds:
        return False
    return cpu_seconds < min_cpu_seconds


def _stall_watchdog(stop: threading.Event) -> None:
    """Fail + exit when the main thread is wedged on one job past the timeout."""
    mins = int(settings.WORKER_STALL_TIMEOUT_MINUTES)
    timeout_seconds = max(60, mins * 60)
    min_cpu_seconds = max(1.0, float(settings.WORKER_STALL_MIN_CPU_SECONDS))
    while not stop.wait(30):
        jid = _CURRENT_JOB[0]
        cpu_since_tick = time.process_time() - _MAIN_PROC[0]
        if not _should_stall_exit(jid, _MAIN_TICK[0], time.time(),
                                  timeout_seconds, cpu_since_tick, min_cpu_seconds):
            continue
        logger.error(
            f"Watchdog: job {jid} made no progress and used <{min_cpu_seconds:.0f}s "
            f"CPU for >{mins}m (main thread wedged); failing it and exiting so the "
            f"service restarts")
        try:
            db = SessionLocal()
            db.execute(
                text("UPDATE jobs SET status='failed', "
                     "error_message=COALESCE(error_message,'')||:reason "
                     "WHERE id=:jid AND status='processing'"),
                {"reason": f" [stalled >{mins}m, watchdog]", "jid": jid})
            db.commit()
            db.close()
        except Exception as e:
            logger.error(f"Watchdog: could not fail job {jid} ({e}); exiting anyway")
        # Hard exit: the stuck main thread cannot be joined, and NSSM's
        # AppExit=Restart turns the process death into a fresh worker.
        os._exit(1)


def claim_job(db, job_id: int) -> bool:
    """Compare-and-swap a queued job onto this worker.

    The polling loop and the arq worker (WORKER_MAX_JOBS) can both look at the same
    row, and the old code set status='processing' unconditionally, so a job could
    be rendered and published twice. The WHERE guard makes exactly one caller win.
    """
    now = datetime.utcnow()
    result = db.execute(
        text(
            "UPDATE jobs SET status='processing', claimed_by=:wid, "
            "claimed_at=:now, updated_at=:now "
            "WHERE id=:jid AND status IN ('created','queued')"
        ),
        {"wid": WORKER_ID, "now": now, "jid": job_id},
    )
    db.commit()
    return result.rowcount == 1


def reclaim_expired_leases(db) -> int:
    """Return abandoned jobs to the queue.

    A job that was 'processing' when the worker was killed stayed invisible
    forever: the poller only selects status='created'. A live job refreshes
    claimed_at (touch_lease, plus update_progress at stage boundaries), so an
    expired lease means nobody is working on it any more.
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.JOB_LEASE_MINUTES)
    result = db.execute(
        text(
            "UPDATE jobs SET status='created', claimed_by=NULL, claimed_at=NULL, "
            "error_message=COALESCE(error_message,'') || ' [lease expired, requeued]' "
            "WHERE status='processing' AND "
            "(claimed_at IS NULL OR claimed_at < :cutoff)"
        ),
        {"cutoff": cutoff},
    )
    db.commit()
    return result.rowcount

def touch_lease(db, job_id: int) -> int:
    """Refresh this worker's lease on a running job.

    Returns the rows touched: 0 means the lease no longer names this worker, so
    the job was requeued (or finished) and the heartbeat has nothing left to do.
    """
    touched = db.execute(
        text("UPDATE jobs SET claimed_at=:now WHERE id=:jid AND claimed_by=:wid"),
        {"now": datetime.utcnow(), "jid": job_id, "wid": WORKER_ID},
    ).rowcount
    db.commit()
    return touched


# What a clip row may say about itself. Anything outside this set is identity
# (id, job_id, index) or publish state, and the publish state is exactly why an
# existing row is reused instead of replaced.
CLIP_RECORD_FIELDS = (
    "start", "end", "title", "hook", "hook_style", "score",
    "hook_score", "coherence_score", "connection_score", "trend_score",
    "score_breakdown", "file_path", "thumbnail_path", "description",
    "hashtags", "platform_metadata", "caption_style", "layout_mode",
)


def record_clip(db, job_id: int, index: int, values: dict) -> Clip:
    """Insert or re-record the row for one rendered clip, and return it.

    Reusing the row a previous run left behind is not an optimization, it is the
    double-publish guard: a fresh Clip() has no youtube_video_id, so the upload
    step reads an unpublished clip and ships a short already on YouTube a second
    time. Reuse therefore keeps every column outside CLIP_RECORD_FIELDS.

    Reuse is not the same as ignoring the run. Writing only on insert left job
    110 re-rendered onto ten new windows while its rows still reported the old
    grid start, end and title - a row describing a file it no longer points at.

    The read above is not a lock, so two workers can both see "no row". The
    unique index on clips(job_id, index) (database.migrations) decides the race
    in the database, and the IntegrityError it raises here is handled as "the
    row exists now, write into it" rather than as a failed render.
    """
    unknown = sorted(set(values) - set(CLIP_RECORD_FIELDS))
    if unknown:
        raise ValueError(
            "record_clip only writes CLIP_RECORD_FIELDS, got "
            + ", ".join(unknown)
        )

    clip_db = (
        db.query(Clip)
        .filter(Clip.job_id == job_id, Clip.index == index)
        .first()
    )
    if clip_db is None:
        clip_db = Clip(
            job_id=job_id, index=index, uploaded=False, upload_retry_count=0,
            **values
        )
        db.add(clip_db)
    else:
        for field, value in values.items():
            setattr(clip_db, field, value)
    try:
        db.commit()
    except IntegrityError:
        # The read-then-write above has a window, and on two workers on one job
        # (see api/jobs.restart_job's old lease hole) somebody else wins it: the
        # unique index on clips(job_id, index) rejects our INSERT. That is the
        # constraint doing its job, not an error to raise - the render is forty
        # minutes old at this point and the row we wanted already exists. Fall
        # through the same reuse path the pre-read would have taken.
        db.rollback()
        raced = (
            db.query(Clip)
            .filter(Clip.job_id == job_id, Clip.index == index)
            .first()
        )
        if raced is None:
            # Not our slot: some other constraint (or a genuinely broken row)
            # failed. Nothing here can fix that silently.
            raise
        for field, value in values.items():
            setattr(raced, field, value)
        db.commit()
        clip_db = raced
        job_debug(job_id, f"Clip slot ({job_id}, {index}) was taken by another "
                          f"worker mid-write; reused row {clip_db.id}")
    db.refresh(clip_db)
    return clip_db

def _get_redis_client():
    global _REDIS_CLIENT, _REDIS_CHECKED
    if _REDIS_CHECKED:
        return _REDIS_CLIENT
    _REDIS_CHECKED = True
    try:
        import redis
        client = redis.Redis(
            host=os.getenv("REDIS_HOST", "localhost"),
            port=int(os.getenv("REDIS_PORT", 6379)),
            socket_connect_timeout=0.08,
            socket_timeout=0.08,
        )
        client.ping()
        _REDIS_CLIENT = client
    except Exception:
        _REDIS_CLIENT = None
    return _REDIS_CLIENT

def _url_fingerprint(url: str) -> str:
    """Return a short stable hash of a source URL for resume verification."""
    import hashlib
    return hashlib.sha256((url or "").strip().encode("utf-8")).hexdigest()[:16]


def ensure_derived(job_id: int, artifact_path: str, kind: str,
                   build: Callable[[], object], inputs: dict,
                   legacy_current: bool = False) -> bool:
    """Produce ``artifact_path`` unless it already comes from these exact inputs.

    Returns True when the artifact was (re)built, so the caller keeps its own
    log wording. ``inputs`` is what the build consumed — the clip window, the
    caption style, the source fingerprint, the identity of the file this
    artifact was derived from — and it is recorded in ``<dir>/artifact.json``
    next to the artifact.

    ``os.path.exists`` was the old test and it is not evidence: the per-clip
    files live in a directory named by the clip *index*, so a re-run whose
    selector picked other windows found ``clip_0/words.json`` sitting there from
    the previous window and burned its text into a freshly cut picture (job 110:
    a frame at t=3175 s carrying the caption "own. Something the legacy" from
    t=0 s). An artifact now only counts as current while its key matches, and
    anything the key cannot answer (no entry, unreadable manifest, different
    kind) is rebuilt.

    The entry is dropped before the rebuild on purpose: a build that dies
    half-way (an ffmpeg failure, the stall watchdog's ``os._exit``) must not
    leave behind a key that still vouches for the truncated file.
    """
    if derived_cache.is_current(artifact_path, kind, inputs, legacy_current=legacy_current):
        return False

    derived_cache.forget(artifact_path)
    build()
    if not derived_cache.record(artifact_path, kind, inputs):
        job_warning(job_id, f"Could not record the cache key for {artifact_path}; "
                            "it will be rebuilt on the next run")
    return True


def _clear_derived_artifacts(job_dir, job_id):
    """Remove per-job files that derive from the source video, so a re-downloaded
    source is never paired with stale audio/transcript/timing or old clip cuts
    (the "wrong subtitles from an old video" bug)."""
    import shutil
    stale_files = [
        "audio.wav", "words.json", "segments.json", "transcript.txt",
        "short.mp4", "vertical.mp4", "final_short.mp4", "final_with_title.mp4",
        "final_with_sfx.mp4", "popup_card.png", "captions.ass", "diarization.json",
        # The cache keys for the files above. Leaving this behind would let the
        # next run vouch for a transcript that no longer exists under it.
        derived_cache.MANIFEST_NAME,
    ]
    for name in stale_files:
        p = os.path.join(str(job_dir), name)
        if os.path.exists(p):
            try:
                os.remove(p)
                job_info(job_id, f"Removed stale derived file: {p}")
            except Exception as e:
                job_warning(job_id, f"Could not remove {p}: {e}")

    # Old clip cuts/captions were made from a previous source — drop them too,
    # otherwise the per-clip "already exists" skips would re-render stale content.
    for entry in os.listdir(str(job_dir)):
        if entry.startswith("clip_"):
            clip_dir = os.path.join(str(job_dir), entry)
            if os.path.isdir(clip_dir):
                try:
                    shutil.rmtree(clip_dir)
                    job_info(job_id, f"Removed stale clip dir: {clip_dir}")
                except Exception as e:
                    job_warning(job_id, f"Could not remove {clip_dir}: {e}")

# What the render step leaves beside final_video.mp4. The trimmed cut is on this
# list because it is a deliverable derived from the render, not an intermediate
# toward it: pruning it would delete the only file matching the row's
# trim_start/trim_end, and the next resume would repoint the row at the long cut.
RENDER_KEEP_NAMES = ("final_video.mp4", clip_trim.TRIMMED_ARTIFACT_NAME,
                     "thumbnail.jpg", "metadata.json", "storyboard.jpg", "words.json")


def _prune_render_intermediates(clip_dir) -> None:
    """Delete the stage-by-stage videos, subtitle and audio files one render left."""
    for entry in os.listdir(str(clip_dir)):
        if entry in RENDER_KEEP_NAMES:
            continue
        if entry.endswith(".mp4") or entry.endswith(".ass") or entry.endswith(".wav"):
            try:
                os.remove(os.path.join(str(clip_dir), entry))
            except OSError:
                pass


def _reapply_recorded_trim(db, job_id, clip_db, clip_dir, clip_start, clip_end,
                           index) -> bool:
    """Re-cut the operator's stored trim out of whatever render exists now.

    A resume skips the render stages and re-records a row that already exists, so
    without this the row keeps naming the untrimmed file and the queue publishes
    the cut the operator removed from the timeline. A recorded trim that no longer
    fits the new render is reported, not hidden: publishing the long version is
    the failure being fixed, so the row stays on the render it describes and the
    job log says why.

    Returns True only when the row was repointed at a fresh trimmed artifact.
    """
    from services.trim import TrimValidationError

    if clip_db.trim_start is None or clip_db.trim_end is None:
        return False
    try:
        trimmed = clip_trim.sync_stored_trim(
            clip_dir, (clip_start, clip_end), clip_db.trim_start, clip_db.trim_end)
    except TrimValidationError as exc:
        job_warning(job_id, f"Clip #{index + 1} will publish untrimmed: its recorded "
                            f"{clip_db.trim_start}-{clip_db.trim_end}s cut does not fit this "
                            f"render ({exc})")
        return False
    if not trimmed:
        return False
    clip_db.file_path = os.path.relpath(trimmed, settings.STORAGE_ROOT)
    db.commit()
    job_info(job_id, f"Clip #{index + 1} re-applied its recorded trim "
                      f"{clip_db.trim_start}-{clip_db.trim_end}s")
    return True


def _drop_transcription_input(job_dir, job_id):
    """Delete the extracted audio once a transcript exists on disk.

    words.json and segments.json are what every later stage reads; the wav is
    only ever an input to the transcriber. A 3-hour source leaves over 1 GB of
    it sitting in the job directory for the rest of its life.
    """
    if not settings.DELETE_AUDIO_AFTER_TRANSCRIPTION:
        return 0

    transcript = os.path.join(str(job_dir), "segments.json")
    words = os.path.join(str(job_dir), "words.json")
    if not (os.path.exists(transcript) and os.path.getsize(transcript) > 2
            and os.path.exists(words) and os.path.getsize(words) > 2):
        # Without a complete transcript the audio is still the only copy of the
        # source material, so a resumed run would have nothing to work from.
        return 0

    leftovers = ["audio.wav", "audio_groq.mp3"]
    paths = [os.path.join(str(job_dir), name) for name in leftovers]
    freed = reclaim(paths)
    if freed:
        job_info(job_id, f"Removed transcription input, freed {freed / 1024**2:.1f} MB")
    return freed


def load_clip_diarization(job_dir, clip_start: float, clip_end: float) -> list:
    """Speaker turns inside one clip, re-zeroed to clip time. [] when none."""
    if not settings.DIARIZATION_ENABLED:
        # The switch owns the whole run: a diarization.json from when it was on
        # must not reach the layouts either (stage_diarize returns [] above, but
        # this file is what the per-clip render actually reads).
        return []
    try:
        path = os.path.join(str(job_dir), "diarization.json")
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        from services.diarization import slice_turns
        return slice_turns(data.get("turns") or [], clip_start, clip_end)
    except Exception:
        return []


CLIP_FADE_IN = 0.04
CLIP_FADE_OUT = 0.25


def clip_audio_fades(duration: float) -> str:
    """afade chain for a cut of `duration` seconds: a click-free in and a soft out."""
    fade_out = min(CLIP_FADE_OUT, max(0.0, duration / 4))
    return (f"afade=t=in:st=0:d={CLIP_FADE_IN},"
            f"afade=t=out:st={max(0.0, duration - fade_out):.3f}:d={fade_out:.3f}")


def cut_clip(
    source_video: str,
    start: float,
    end: float,
    output: str
) -> str:
    """
    Cut a clip from a source video.

    Args:
        source_video: Path to source video
        start: Start time in seconds
        end: End time in seconds
        output: Output file path

    Returns:
        Path to output file
    """
    duration = end - start

    cmd = [
        "ffmpeg",
        "-y",
        "-ss", str(start),
        "-i", source_video,
        "-t", str(duration),
        "-c:v", "libx264",
        "-g", "60",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        # A hard cut on a waveform clicks and sounds chopped even when it falls
        # in a pause; a few tens of milliseconds of fade is inaudible as a fade.
        "-af", clip_audio_fades(duration),
        "-c:a", "aac",
        "-b:a", "128k",
        output
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        # Verify output file has non-zero duration
        probe_cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            output
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True)
        try:
            duration = float(probe.stdout.strip()) if probe.stdout.strip() else 0.0
        except Exception:
            duration = 0.0
        if duration <= 0.0:
            raise Exception("Clip has zero duration or is invalid")
        return output
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        raise Exception(f"Clip cutting failed: {error_msg}")

def get_clip_transcript(
    words_file: str,
    clip_start: float,
    clip_end: float
) -> str:
    """
    Get the transcript text for a specific clip.

    Args:
        words_file: Path to words.json
        clip_start: Clip start time
        clip_end: Clip end time

    Returns:
        Transcript text for the clip
    """
    try:
        with open(words_file, "r", encoding="utf-8") as f:
            words = json.load(f)
    except Exception as e:
        # An empty transcript means the clip renders without captions, so the
        # reason (missing words.json, truncated JSON) has to be visible.
        logger.warning(f"Could not read transcript '{words_file}': {e}")
        return ""

    if isinstance(words, dict) and "words" in words:
        words = words["words"]

    clip_words = []
    for word in words:
        w_start = word.get("start", 0)
        w_end = word.get("end", 0)
        # Overlap test, not containment: a word that straddles clip_start or
        # clip_end was previously dropped, cutting the first/last word off the
        # transcript used for SFX picking and virality scoring.
        if w_end > clip_start and w_start < clip_end:
            clip_words.append(word.get("word", ""))

    return " ".join(clip_words)

async def stage_download_source(job_id: int, job: Job, job_dir: Path, update_progress: Callable[[str, int], None]) -> str:
    """Resolve the source video for this job, downloading it if needed.
    A cached source.mp4 is reused only when the stored URL fingerprint
    matches, so a restart with a new URL cannot silently re-render the
    previous job's footage.
    Returns the path to the source video.
    """
    job_info(job_id, "Step 1/8: Downloading video...")
    update_progress("Downloading video", 10)
    video_path = os.path.join(str(job_dir), "source.mp4")
    fp_path = os.path.join(str(job_dir), "source.urlhash")

    # Resume a cached source ONLY when the URL fingerprint matches.
    # Otherwise the worker would keep re-rendering the previous job's
    # video (e.g. the "cat" source) no matter what URL the user entered.
    cached_hash = ""
    if os.path.exists(fp_path):
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                cached_hash = f.read().strip()
        except Exception:
            cached_hash = ""

    is_upload = bool(job.youtube_url and job.youtube_url.startswith("upload://"))
    if is_upload and os.path.exists(video_path):
        job_info(job_id, f"Preserving uploaded source video: {video_path}")
    elif is_upload:
        # The source came from a browser upload, so there is no URL to
        # re-fetch. Falling through to download_youtube() here produced
        # 'Unsupported url scheme: "upload"' on every restart/rerun.
        raise Exception(
            f"Uploaded source file is missing ({video_path}). "
            "Restart/re-render is not available for uploaded videos — "
            "create a new job and upload the file again."
        )
    elif os.path.exists(video_path) and cached_hash == _url_fingerprint(job.youtube_url):
        job_info(job_id, f"Reusing cached source for same URL: {video_path}")
    else:
        if os.path.exists(video_path):
            job_info(job_id, "URL changed or unknown — re-downloading source")
            try:
                os.remove(video_path)
            except Exception as e:
                job_warning(job_id, f"Could not remove old source video ({e}); will overwrite")
        # Derived artifacts (audio/transcript/timing + old clip cuts) were made
        # from a previous source — drop them so the re-run renders fully fresh.
        _clear_derived_artifacts(job_dir, job_id)
        video_path = download_youtube(job.youtube_url, job_id, progress_callback=update_progress)
        job_info(job_id, f"Downloaded: {video_path}")

    with open(fp_path, "w", encoding="utf-8") as f:
        f.write(_url_fingerprint(job.youtube_url))
    return video_path


async def stage_extract_audio(job_id: int, job_dir: Path, video_path: str, update_progress: Callable[[str, int], None]) -> str:
    """Split the audio track the transcript is built from.
    Returns the path to the extracted audio.
    """
    job_info(job_id, "Step 2/8: Extracting audio...")
    update_progress("Extracting audio...", 21)
    audio_path = os.path.join(str(job_dir), "audio.wav")

    if not os.path.exists(audio_path):
        audio_path = extract_audio(video_path)
        job_info(job_id, f"Audio extracted: {audio_path}")
    else:
        job_info(job_id, f"Audio already exists: {audio_path}")
    update_progress("Audio ready", 24)
    return audio_path


async def stage_transcribe(job_id: int, job: Job, job_dir: Path, audio_path: str, update_progress: Callable[[str, int], None]) -> tuple:
    """Build the transcript, optionally translate it, read the source metadata.
    Whisper is skipped when segments.json/words.json are already there, which is
    what makes a resumed or re-rendered job cheap.  Returns (segments,
    segments_file, words_file, source_title, source_channel).
    """
    job_info(job_id, "Step 3/8: Transcribing audio...")
    update_progress("Transcribing audio (0%)...", 25)
    segments_file = os.path.join(str(job_dir), "segments.json")
    words_file = os.path.join(str(job_dir), "words.json")

    # Auto-Translate is the only multilingual knob the UI sends, and Whisper's own
    # translate task is what actually produces English captions, so map the flag onto
    # it. The post-transcript LLM pass that used to sit here could never run - its
    # helpers do not exist in services.whisper and its "already English?" check read
    # transcript_metadata.json while whisper writes transcript_info.json - but the
    # ImportError it raised was swallowed as "LLM Translation failed".
    transcribe_task = getattr(job, "transcription_task", None) or (
        "translate" if getattr(job, "llm_translate", False) else "transcribe")
    source_language = getattr(job, "source_language", None) or None
    keep_native = bool(getattr(job, "keep_native_transcript", False))

    # The transcript is keyed by the source it was heard from and by every
    # setting that changes what the model returns. Rebuilding it costs a full
    # Whisper pass on a multi-hour source, so a transcript that predates this
    # manifest is accepted on existence alone (legacy_current) — the only way
    # the file can exist while the source changed is the re-download branch,
    # and that branch deletes both files before it re-fetches.
    transcript_inputs = {
        "source": derived_cache.source_fingerprint(job_dir),
        "model": getattr(job, "whisper_model", None),
        "task": transcribe_task,
        "language": source_language,
        "keep_native": keep_native,
    }
    transcript_on_disk = os.path.exists(segments_file) and os.path.exists(words_file)
    cached_transcript = transcript_on_disk and all(
        derived_cache.is_current(path, "transcript", transcript_inputs, legacy_current=True)
        for path in (segments_file, words_file))

    if not cached_transcript:
        transcribe_audio_file(
            audio_path,
            str(job_dir),
            job.whisper_model,
            task=transcribe_task,
            language=source_language,
            keep_native=keep_native,
            progress_callback=update_progress,
        )
        derived_cache.record(segments_file, "transcript", transcript_inputs)
        derived_cache.record(words_file, "transcript", transcript_inputs)
        job_info(job_id, f"Transcript created (task={transcribe_task}): {segments_file}, {words_file}")
    else:
        job_info(job_id, "Transcript already exists")
        update_progress("Transcript loaded", 50)

    # Load segments for duration
    with open(segments_file, "r", encoding="utf-8") as f:
        segments = json.load(f)

    _drop_transcription_input(job_dir, job_id)

    source_title = None
    source_channel = None
    meta_file = job_dir / "meta.json"
    if meta_file.exists():
        try:
            with open(meta_file, "r", encoding="utf-8") as mf:
                meta_data = json.load(mf)
                source_title = meta_data.get("title")
                source_channel = meta_data.get("channel") or meta_data.get("uploader")
        except Exception:
            pass
    return segments, segments_file, words_file, source_title, source_channel


async def stage_diarize(job_id: int, job_dir: Path, video_path: str, clips: list,
                        update_progress: Callable[[str, int], None]) -> list:
    """Who speaks when across the selected clip windows, written to diarization.json.

    Diarization is consumed only per clip (``load_clip_diarization`` slices turns
    to each clip's window), so it runs AFTER selection and over just those windows
    rather than the whole source — the difference between minutes and ~40+ minutes
    on a long video.

    Soft by design: a missing HF token, a gated model, or an undecodable source
    leaves the job with no speaker turns and a logged warning — every later stage
    reads 'no diarization' as the old behavior, not as an error.
    """
    # The switch first. A diarization.json left by an older run with the switch
    # on is evidence about that run, not an instruction for this one, and the
    # resume used to sit above the gate — so a job with DIARIZATION_ENABLED=false
    # still fed cached speaker turns to the layouts.
    if not settings.DIARIZATION_ENABLED:
        job_info(job_id, "Speaker diarization disabled (DIARIZATION_ENABLED=false), skipping")
        return []

    diarization_file = os.path.join(str(job_dir), "diarization.json")

    windows = [{"start": float(c["start"]), "end": float(c["end"])}
               for c in (clips or []) if c.get("start") is not None and c.get("end") is not None]
    if not windows:
        job_info(job_id, "Speaker diarization skipped: no clip windows to analyze")
        return []

    # Diarization runs over the selected windows only, and pyannote numbers the
    # voices it finds per call (SPEAKER_00 of one call is not SPEAKER_00 of the
    # next), so turns computed for other windows are not reusable at all. The
    # resume is therefore keyed by the exact window set plus the source and the
    # model that produced it.
    diarization_inputs = {
        "source": derived_cache.source_fingerprint(job_dir, video_path),
        "windows": windows,
        "model": settings.DIARIZATION_MODEL,
        "margin": settings.DIARIZATION_CLIP_MARGIN_SECONDS,
    }
    if derived_cache.is_current(diarization_file, "diarization", diarization_inputs):
        try:
            with open(diarization_file, "r", encoding="utf-8") as f:
                turns = (json.load(f) or {}).get("turns") or []
            job_info(job_id, f"Diarization already on disk for these {len(windows)} clip "
                             f"windows: {len(turns)} speaker turns (resume)")
            return turns
        except Exception as e:
            job_warning(job_id, f"Could not read existing diarization.json ({e}); re-running diarization")

    if os.path.exists(diarization_file):
        # Built for a different selection (or unreadable): drop it and its key so
        # no clip slices turns that were never computed over its own window.
        try:
            os.remove(diarization_file)
            job_info(job_id, "Diarization is for different clip windows — re-running")
        except Exception as e:
            job_warning(job_id, f"Could not remove stale diarization.json ({e})")
        derived_cache.forget(diarization_file)

    total = sum(w["end"] - w["start"] for w in windows)
    job_info(job_id, f"Speaker diarization over {len(windows)} clip windows "
                     f"(~{total:.0f}s of audio, pyannote, CPU)...")
    update_progress("Identifying speakers (per clip)...", 58)
    try:
        from services.diarization import try_diarize_windows
        turns = try_diarize_windows(video_path, windows,
                                    margin=settings.DIARIZATION_CLIP_MARGIN_SECONDS)
    except Exception as e:
        # try_diarize_windows is soft-fail by contract; the stage must be too.
        turns = None
        job_warning(job_id, f"Diarization stage error ({e}); continuing without speaker turns")
    if turns is None:
        job_warning(job_id, "Speaker diarization unavailable; layouts fall back to motion/object heuristics")
        return []
    try:
        with open(diarization_file, "w", encoding="utf-8") as f:
            json.dump({"model": settings.DIARIZATION_MODEL, "turns": turns}, f, ensure_ascii=False, indent=2)
        derived_cache.record(diarization_file, "diarization", diarization_inputs)
    except Exception as e:
        job_warning(job_id, f"Diarization computed but could not be written to disk: {e}")
    job_info(job_id, f"Diarization: {len(turns)} speaker turns across clip windows")
    return turns


async def _fix_caption_names(job_dir, clip_words_file: str) -> int:
    """Correct misheard names in one clip's words, with the source's title and tags as context."""
    from services.caption_fix import fix_clip_words
    title, tags = "", []
    try:
        with open(os.path.join(str(job_dir), "meta.json"), "r", encoding="utf-8") as f:
            meta = json.load(f)
        title = meta.get("title") or ""
        tags = meta.get("tags") or []
    except Exception:
        pass
    return await fix_clip_words(clip_words_file, title=title, tags=tags)


async def stage_scene_attention(job_id: int, clip_dir, short_path: str, clip_words_file: str,
                                 clip_transcript: str) -> Optional[dict]:
    """What each scene of this clip is about, for the reframe decision.

    The keyword tier is local and always cheap; the text tier is one gateway call
    per clip, and the vision tier is off unless it is explicitly switched on. The
    whole stage is skipped when SCENE_ATTENTION_ENABLED is false, and any failure
    — a dead gateway, an unreadable transcript — degrades to None with a log line,
    leaving the crop on the person heuristics it already used.

    When the regular scene attention returns 0 scenes (YOLO detected no objects),
    falls back to VLM-based scene understanding which can identify buildings,
    construction, fields, etc. that COCO classes miss.
    """
    if not getattr(settings, "SCENE_ATTENTION_ENABLED", True):
        return None
    try:
        from services import reframe_plan
        from services.scene_attention import build_attention
        analysis = reframe_plan.analyse_clip(short_path)
        attention = await build_attention(analysis, clip_words_file, clip_transcript or "")
    except Exception as e:
        job_warning(job_id, f"Scene attention unavailable ({e}); cropping stays on the person heuristics")
        return None
    if not attention:
        return None
    for note in attention.get("notes") or []:
        job_info(job_id, f"Scene attention: {note}")
    scenes = attention.get("scenes") or []

    # VLM FALLBACK: When regular scene attention found 0 scenes (no YOLO objects),
    # try VLM-based understanding which can identify buildings, vehicles, etc.
    if not scenes:
        job_info(job_id, "Scene attention found 0 scenes, trying VLM fallback...")
        try:
            from services import reframe_plan
            from services.vlm_scene_attention import build_vlm_attention
            analysis = reframe_plan.analyse_clip(short_path)
            vlm_attention = await build_vlm_attention(
                analysis, clip_words_file, clip_transcript or "", video_path=short_path
            )
            if vlm_attention and vlm_attention.get("scenes"):
                job_info(job_id, f"VLM fallback found {len(vlm_attention['scenes'])} scenes")
                for note in vlm_attention.get("notes") or []:
                    job_info(job_id, f"VLM scene attention: {note}")
                attention = vlm_attention
                scenes = attention.get("scenes") or []
        except Exception as e:
            job_warning(job_id, f"VLM scene attention fallback failed ({e}); using person heuristics")

    if scenes:
        job_info(job_id, "Scene attention focuses: " + "; ".join(
            f"{scene.get('target')} {float(scene.get('start') or 0.0):.1f}-{float(scene.get('end') or 0.0):.1f}s"
            for scene in scenes))
    try:
        with open(os.path.join(str(clip_dir), "attention.json"), "w", encoding="utf-8") as f:
            json.dump(attention, f, ensure_ascii=False, indent=2)
    except Exception as e:
        job_warning(job_id, f"Scene attention computed but could not be written to disk: {e}")
    return attention


async def stage_score_audio_energy(job_id: int, video_path: str, segments: list, segments_file: str, update_progress: Callable[[str, int], None]) -> list:
    """Rank each segment by loudness so clip selection has a signal beyond text.
    Fails soft: a broken RMS pass leaves the neutral 0.5 score rather than
    stopping the job.  Returns the segments, enriched when the pass worked.
    """
    job_info(job_id, "Step 4a/8: Audio energy pre-pass (FFmpeg RMS analysis)...")
    update_progress("Analyzing audio energy...", 51)
    try:
        energy_status: dict = {}
        segments = score_audio_energy(str(video_path), segments, status_out=energy_status)
        job_info(job_id, f"Audio energy scored for {len(segments)} segments")
        if energy_status.get("partial"):
            job_warning(job_id, f"Audio energy band incomplete: "
                        f"{energy_status.get('windows_read')}/{energy_status.get('windows_attempted')} "
                        f"windows readable, {energy_status.get('unmeasured_segments')} segments carry "
                        f"the measured mean (coverage {energy_status.get('coverage')})")
        elif energy_status.get("flat_band"):
            job_info(job_id, "Audio energy band is flat but measured - audio_dynamics will be near 0")
        # Persist enriched segments so select_clips can read audio_energy_score
        with open(segments_file, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
    except Exception as _ae:
        job_warning(job_id, f"Audio energy pre-pass failed ({_ae}), continuing with neutral 0.5")
    return segments


async def stage_temporal_localization(job_id: int, video_path: str, segments: list, job_dir: Path, update_progress: Callable[[str, int], None]) -> list:
    """Build a visual event timeline for the full video.

    Runs YOLO sampling + scene-change detection to find interesting moments
    (object appearances, person entries, scene cuts).  The timeline feeds
    into clip selection so the LLM knows *what happens* visually, not just
    *what is said*.  Soft-fails: returns [] if sampling crashes.
    """
    job_info(job_id, "Step 4a/8: Temporal event localization (YOLO + scene detection)...")
    update_progress("Analyzing visual events across video...", 51)
    events_file = os.path.join(str(job_dir), "temporal_events.json")
    try:
        from services.temporal_localization import localise_video, save_events
        events = localise_video(str(video_path), segments)
        save_events(events, events_file)
        job_info(job_id, f"Temporal localization: {len(events)} events found")
    except Exception as e:
        job_warning(job_id, f"Temporal localization failed ({e}), continuing without visual events")
        events = []
    return events


async def stage_select_clips(job_id: int, job: Job, segments_file: str, source_title: Optional[str], update_progress: Callable[[str, int], None], temporal_events: Optional[list] = None) -> list:
    """Pick the clips to render and, for a diagnostic job, vary their hooks.
    Returns the clip dicts the render loop consumes.
    """
    job_info(job_id, f"Step 4b/8: Selecting best clips (source title: {source_title or 'N/A'})...")
    update_progress("Analyzing virality & selecting top clips...", 52)
    
    num_target_clips = job.target_clips if (job and getattr(job, "target_clips", None)) else settings.NUM_CLIPS
    clips = await select_clips(segments_file, num_target_clips, video_title=source_title, temporal_events=temporal_events)
    job_info(job_id, f"Selected {len(clips)} clips")
    update_progress(f"Selected {len(clips)} clips", 56)
    
    # Zero-view diagnostic: assign varied hook styles to each clip
    is_diagnostic = getattr(job, "is_zero_view_diagnostic", False)
    hook_style_list = getattr(job, "hook_style_list", None)
    if is_diagnostic and hook_style_list and isinstance(hook_style_list, list):
        job_info(job_id, f"Applying zero-view diagnostic hook styles: {hook_style_list}")
        for i, clip in enumerate(clips):
            if i < len(hook_style_list):
                style = hook_style_list[i]
                clip["hook_style"] = style
                # Customize hook title to highlight the style for testing
                if style == "aggressive":
                    clip["title"] = f"[AGGRESSIVE] {clip.get('title', 'Viral Moment')}"
                    clip["hook"] = f"YOU WON'T BELIEVE WHAT HAPPENED NEXT — {clip.get('hook', 'SHOCKING')}"
                elif style == "curiosity":
                    clip["title"] = "[CURIOUS] The Secret No One Talks About"
                    clip["hook"] = f"Wait until you hear what they hid — {clip.get('hook', 'SECRET')}"
                elif style == "story-driven":
                    clip["title"] = "[STORY] The Full Story Behind This Moment"
                    clip["hook"] = f"This is why that happened — {clip.get('hook', 'STORY')}"
                job_info(job_id, f"Clip {i + 1}: Hook style '{style}' applied")
    return clips


async def process_clip(db: Session, job: Job, job_id: int, job_dir: Path, index: int, clip: dict, total_clips: int, clip_errors: list, video_path: str, words_file: str, source_title: Optional[str], source_channel: Optional[str], tiktok_privacy: str, update_progress: Callable[[str, int], None], lease_stop: Optional[threading.Event] = None) -> None:
    """Render one clip end to end: cut, crop, captions, effects, metadata, upload.
    Every failure is recorded in clip_errors instead of raised, because one bad
    segment must not throw away the other clips a job already rendered. The
    caller decides whether enough of them survived to call the job completed.

    ``lease_stop`` lets an upload-pacing wait abort: a job another worker
    already reclaimed must not keep publishing.
    """
    clip_start = clip["start"]
    clip_end = clip["end"]
    clip_id = None  # Will be set after DB insert

    current_clip_pct = 56 + int((index / max(total_clips, 1)) * 40)
    update_progress(f"Rendering Clip {index + 1}/{total_clips}: Reframing & Subtitles...", current_clip_pct)
    job_info(job_id, f"Processing clip {index + 1}/{total_clips} [{clip_start:.1f}s - {clip_end:.1f}s]")

    try:
        # Create clip directory
        clip_dir = job_dir / f"clip_{index}"
        clip_dir.mkdir(parents=True, exist_ok=True)

        # =====================
        # 5a. CUT CLIP
        # =====================
        short_path = os.path.join(str(clip_dir), "short.mp4")
        clip_window = [float(clip_start), float(clip_end)]
        if ensure_derived(job_id, short_path, "clip_cut",
                          lambda: cut_clip(video_path, clip_start, clip_end, short_path),
                          {"source": derived_cache.source_fingerprint(job_dir, video_path),
                           "window": clip_window,
                           "audio": clip_audio_fades(clip_window[1] - clip_window[0])}):
            job_info(job_id, f"Clip cut: {short_path}")
        else:
            job_info(job_id, f"Clip already exists for [{clip_window[0]:.1f}s - "
                             f"{clip_window[1]:.1f}s]: {short_path}")

        # =====================
        # 5b-5e. RENDER: SINGLE-PASS UNIFIED (Task D) OR SEQUENTIAL FALLBACK
        # =====================
        # User's chosen preset on the job ALWAYS takes precedence to keep all clips consistent
        layout_choice = getattr(job, "default_layout_mode", None) or clip.get("layout") or "auto"
        caption_style = getattr(job, "default_caption_style", None) or clip.get("caption_style") or "mrbeast"

        # 5c. Crop Words
        # Keyed by the window it was sliced for and by the transcript file it was
        # sliced from. words.json is the one per-clip file the end-of-render
        # cleanup keeps, which is exactly why an existence check here silently
        # reused the previous selection's text (job 110).
        clip_words_file = os.path.join(str(clip_dir), "words.json")
        name_fix_task = None
        name_fix_model = (settings.CAPTION_FIX_MODEL or "default") \
            if getattr(settings, "CAPTION_FIX_ENABLED", False) else None
        words_key = {"window": clip_window,
                     "transcript": derived_cache.input_fingerprint(words_file)}
        if name_fix_model:
            # Part of the key so words cropped before the correction existed are
            # re-cropped and corrected instead of being reused as they were.
            words_key["name_fix"] = name_fix_model
        if ensure_derived(job_id, clip_words_file, "clip_words",
                          lambda: crop_words(words_file, clip_start, clip_end, clip_words_file),
                          words_key):
            job_info(job_id, f"Words cropped: {clip_words_file}")
            # Runs alongside scene attention (a ~25 s model call of its own) and is
            # awaited just before the captions are built from these words.
            if name_fix_model:
                name_fix_task = asyncio.create_task(_fix_caption_names(job_dir, clip_words_file))
        else:
            job_info(job_id, f"Words already cropped for [{clip_window[0]:.1f}s - "
                             f"{clip_window[1]:.1f}s]: {clip_words_file}")

        # ---- transcript (used for SFX selection + virality) ----
        clip_transcript = get_clip_transcript(words_file, clip_start, clip_end)

        # ---- speaker diarization for this clip ([] when the source had none) ----
        clip_turns = load_clip_diarization(job_dir, clip_start, clip_end)
        if clip_turns:
            job_info(job_id, f"Diarization: {len(clip_turns)} speaker turns in clip {index + 1}")

        # ---- scene attention: what the clip is about, moment to moment ----
        # Worth a model call only when the crop will actually follow something:
        # a preset static/fit_blur/split_screen never reads the focus trajectory.
        clip_attention = None
        if settings.SCENE_ATTENTION_ENABLED and (
                layout_choice in ("auto", "adaptive", None, "")
                or layout_choice in ("single_speaker", "face_tracked")):
            clip_attention = await stage_scene_attention(
                job_id, clip_dir, short_path, clip_words_file, clip_transcript)

        # Layout resolution runs after the voices and the scene read are known, so
        # the reframe decision can follow who is talking and what they describe.
        if layout_choice in ("auto", "adaptive", None, ""):
            try:
                from services.smart_crop import detect_adaptive_layout
                resolved_layout = detect_adaptive_layout(short_path, diarization=clip_turns or None,
                                                          attention=clip_attention)
                job_info(job_id, f"Auto-detected adaptive layout for clip {index}: '{resolved_layout}'")
            except Exception as e:
                job_info(job_id, f"Adaptive layout detection fallback: {e}")
                resolved_layout = "single_speaker"
        else:
            resolved_layout = layout_choice

        # 5d. Generate ASS Captions with Selected Preset & Emojis
        # The style is part of the key, so switching presets on a re-render can
        # not leave an old-styled .ass behind; the cropped-words identity is in it
        # too, so a window change that rebuilt words.json rebuilds the captions
        # even if the style and the index are untouched.
        captions_path = os.path.join(str(clip_dir), "captions.ass")
        if name_fix_task is not None:
            try:
                # free/best took 14-85 s per clip on job 112; past this the clip
                # keeps Whisper's words rather than holding up the render.
                fixed = await asyncio.wait_for(name_fix_task, timeout=120)
                if fixed:
                    job_info(job_id, f"Caption names corrected: {fixed} word(s) in clip {index + 1}")
            except Exception as e:
                job_warning(job_id, f"Caption name correction skipped ({e})")
        if ensure_derived(job_id, captions_path, "captions",
                          lambda: create_captions(clip_words_file, captions_path,
                                                  style_name=caption_style),
                          {"window": clip_window,
                           "style": caption_style,
                           "words": derived_cache.input_fingerprint(clip_words_file)}):
            job_info(job_id, f"Captions created with '{caption_style}' style: {captions_path}")
        else:
            job_info(job_id, f"Captions already exist for '{caption_style}' style")

        final_path = os.path.join(str(clip_dir), "final_short.mp4")

        # ---- Content safety: smart blur (optional) ----
        smart_blur_frag = None
        if getattr(job, "smart_blur_enabled", False):
            try:
                from services.focus_tracking import get_smart_blur_filter_for_video
                smart_blur_frag = get_smart_blur_filter_for_video(short_path)
                if smart_blur_frag:
                    job_info(job_id, "Smart blur: unsafe objects detected, blurring tracked regions")
                else:
                    job_info(job_id, "Smart blur: no unsafe objects detected")
            except Exception as e:
                job_warning(job_id, f"Smart blur detection failed ({e}); continuing without blur")

        # ---- Contextual Visual Popups Preparation (Tone-gated Intelligent Injection) ----
        popup_png = None
        popup_start = None
        popup_duration = 2.0
        popup_position = "upper_third"

        popup_cues = clip.get("popup_cues", [])
        if not popup_cues and clip.get("broll_query"):
            popup_cues = [{
                "query": clip.get("broll_query"),
                "start_offset": float(clip.get("broll_start", 2.5)),
                "duration": float(clip.get("broll_duration", 2.0)),
                "position": "upper_third",
            }]

        if clip.get("popup_eligible", True) and popup_cues:
            try:
                from services.smart_visuals import prepare_clip_popup
                clip_dur = max(0.1, clip_end - clip_start)
                popup_data = await prepare_clip_popup(
                    clip_transcript,
                    popup_cues,
                    str(clip_dir),
                    clip_duration=clip_dur,
                )
                if popup_data:
                    popup_png = popup_data["card_path"]
                    popup_start = popup_data["start"]
                    popup_duration = popup_data["duration"]
                    popup_position = popup_data.get("position", "upper_third")
                    job_info(job_id, f"Contextual visual popup prepared for '{popup_data['query']}' (tone: {popup_data.get('tone')})")
            except Exception as e:
                job_warning(job_id, f"Visual popup preparation failed ({e}); continuing without popup")

        # ---- Headline first, then the render ----
        # Single-pass mode composites the title PNG *inside* the encode, so the
        # text passed there is what the viewer reads forever. Generating the
        # metadata after the render left job 105 with the selector's draft
        # burned into every frame while the database held the real title.
        job_info(job_id, "Generating metadata...")
        clip_transcript = get_clip_transcript(words_file, clip_start, clip_end)
        # A model pick keeps the selector's headline: it was written with the whole
        # video in view and ranked on it. The diagnostic arms carry a bracketed
        # test label in the title, which must not be burned in as-is.
        from services.clip_selector import MODEL_SELECTION_PATHS
        metadata = await generate_metadata(
            clip_transcript,
            clip.get("title", ""),
            clip.get("hook", ""),
            keep_title=(clip.get("selection_path") in MODEL_SELECTION_PATHS
                        and not clip.get("hook_style")),
        )
        final_title = metadata.get("title") or clip.get("title") or ""

        # ---- Render: single-pass unified OR sequential fallback ----
        _used_single_pass = False
        if getattr(settings, "SINGLE_PASS_RENDER", False):
            try:
                from services.vertical import build_vertical_filter
                from services.sfx import pick_sfx_for_clip, _find_sfx_file
                from services.render import render_single_pass as _rsp

                vertical_frag = build_vertical_filter(short_path, mode=resolved_layout,
                                                      diarization=clip_turns or None, attention=clip_attention)
                sfx_list = pick_sfx_for_clip(final_title or "", clip_transcript, job.style_prompt or "")
                sfx_files = []
                sfx_ts = []
                for name in sfx_list:
                    p = _find_sfx_file(name)
                    if p:
                        sfx_files.append(p)
                        sfx_ts.append(0.3 if len(sfx_files) == 1 else max(1.5, (clip_end - clip_start) / 2.0))
                _title = final_title or derive_title_from_text(clip_transcript)
                # short_path is already cut from clip_start to clip_end, so relative duration is 0..duration
                clip_dur = max(0.1, clip_end - clip_start)
                _rsp(
                    short_path, 0.0, clip_dur, final_path,
                    vertical_frag=vertical_frag,
                    captions_path=captions_path,
                    title_text=_title,
                    sfx_files=sfx_files or None,
                    sfx_timestamps=sfx_ts or None,
                    mode=resolved_layout,
                    smart_blur_frag=smart_blur_frag,
                    popup_png=popup_png,
                    popup_start=popup_start,
                    popup_duration=popup_duration,
                    popup_position=popup_position,
                    source_channel=source_channel,
                )
                _used_single_pass = True
                job_info(job_id, f"Single-pass render complete: {final_path}")
            except Exception as e:
                job_warning(job_id, f"Single-pass render failed ({e}); falling back to sequential")

        # ---- Sequential fallback ----
        if not _used_single_pass:
            vertical_path = os.path.join(str(clip_dir), "vertical.mp4")
            try:
                if ensure_derived(job_id, vertical_path, "vertical",
                                  lambda: smart_crop_video(short_path, vertical_path,
                                                           mode=resolved_layout),
                                  {"short": derived_cache.input_fingerprint(short_path),
                                   "layout": resolved_layout}):
                    job_info(job_id, f"Vertical conversion ({resolved_layout}): {vertical_path}")
                else:
                    job_info(job_id, f"Vertical already exists: {vertical_path}")
            except Exception as e:
                job_warning(job_id, f"Vertical conversion failed, using original short: {str(e)}")
                vertical_path = short_path
                job_info(job_id, f"Vertical conversion fallback: {vertical_path}")

            if ensure_derived(job_id, final_path, "burned",
                              lambda: burn_captions(vertical_path, captions_path, final_path),
                              {"vertical": derived_cache.input_fingerprint(vertical_path),
                               "captions": derived_cache.input_fingerprint(captions_path)}):
                job_info(job_id, f"Final video (sequential): {final_path}")
            else:
                job_info(job_id, "Final video already exists")

        # single-pass path: set vertical_path to final for virality call
        if _used_single_pass:
            vertical_path = final_path

        # =====================
        # 5f. METADATA ENRICHMENT
        # (title/description/hashtags were generated before the render, so the
        #  burned-in headline and the published one are the same text)
        # =====================
        # Compute Active Expression & Multi-modal Virality Analysis.
        # resolve_virality_stats never raises: if the visual/keyword pass fails
        # it falls back to the selector's own score (flagged, logged) instead of
        # the old hardcoded 85.0 that sat above the real band and hid the error.
        from services.virality_detector import resolve_virality_stats
        v_stats = resolve_virality_stats(clip.get("virality_score"), clip_transcript, vertical_path)
        metadata["virality_breakdown"] = v_stats
        job_info(job_id, f"Clip Virality Score: {v_stats['virality_score_100']}/100 "
                         f"(Visual: {v_stats.get('visual_intensity', 'n/a')})")

        # Generate Platform-Specific Copy (TikTok, Instagram Reels, YouTube Shorts)
        from services.copy_generator import generate_platform_copies
        platform_copy = await generate_platform_copies(
            clip_transcript,
            final_title
        )
        metadata["platform_copy"] = platform_copy

        # Save metadata
        metadata_path = os.path.join(str(clip_dir), "metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        job_info(job_id, f"Metadata generated: {metadata_path}")

        # Add top TikTok title overlay ONCE at the end of clip processing
        # (final_title was resolved before the render, together with the headline
        # that single-pass mode already composited)
        title_overlay_path = os.path.join(str(clip_dir), "final_with_title.mp4")
        # In single-pass mode, the title PNG was already composited in the
        # single ffmpeg pass — skip the separate motion-title re-encode.
        _skip_title_sfx = getattr(settings, "SINGLE_PASS_RENDER", False) and _used_single_pass
        if not _skip_title_sfx:
            try:
                # Professional motion-graphic title (renders a clean PNG,
                # no grey box). Falls back to the old drawtext title on failure.
                from services.motion_title import add_motion_title
                try:
                    add_motion_title(final_path, final_title, title_overlay_path)
                except Exception:
                    from services.render import add_title_overlay
                    add_title_overlay(final_path, final_title, title_overlay_path)
                final_path = title_overlay_path
                job_info(job_id, f"Title overlay added: {title_overlay_path}")
            except Exception as e:
                job_warning(job_id, f"Title overlay failed: {str(e)}")
        else:
            job_info(job_id, "Skipping separate motion-title (already composited in single-pass render)")
        # 5g. ADD AUTOMATIC SFX (skip in single-pass mode; already mixed)
        # =====================
        sfx_path = os.path.join(str(clip_dir), "final_with_sfx.mp4")
        if not _skip_title_sfx:
            try:
                clip_transcript = get_clip_transcript(words_file, clip_start, clip_end)
                add_automatic_sfx(
                    final_path,
                    sfx_path,
                    title=final_title,
                    transcript=clip_transcript,
                    style_prompt=job.style_prompt or ""
                )
                final_path = sfx_path
                job_info(job_id, f"Automatic SFX added: {sfx_path}")
            except Exception as e:
                job_warning(job_id, f"Automatic SFX failed: {str(e)}")
        else:
            job_info(job_id, "Skipping separate SFX pass (already mixed in single-pass render)")


        # =====================
        # 5h. GENERATE THUMBNAIL & STORYBOARD
        # =====================
        job_info(job_id, "Generating thumbnail & storyboard...")
        thumbnail_path = os.path.join(str(clip_dir), "thumbnail.jpg")
        thumbnail_result = generate_thumbnail(
            final_path,
            thumbnail_path,
            final_title,
            clip_index=index,
            job_id=job_id,
        )

        try:
            from services.storyboard import generate_storyboard
            generate_storyboard(final_path, str(clip_dir))
        except Exception as e:
            job_warning(job_id, f"Storyboard generation failed: {str(e)}")


        # CLEANUP intermediary files — keep ONLY 1 final deliverable (final_video.mp4)
        final_video_path = os.path.join(str(clip_dir), "final_video.mp4")
        if os.path.exists(final_path) and os.path.abspath(final_path) != os.path.abspath(final_video_path):
            import shutil
            try:
                shutil.move(final_path, final_video_path)
            except Exception:
                pass
        final_path = final_video_path

        # Remove all intermediate video/audio/ass files, keeping only final_video.mp4
        _prune_render_intermediates(clip_dir)

        # =====================
        # 5g-extra. DIMENSION CHECK (pre-upload 9:16 validation)
        # Detect clips with non-9:16 dimensions before they reach YouTube.
        # =====================
        try:
            _dims = check_dimensions(final_path)
            if not _dims.get("is_valid_916"):
                log_dimension_warning(final_path, _dims)
            else:
                job_info(job_id, f"Dimension check OK for Clip #{index + 1}: {_dims['width']}x{_dims['height']} ({_dims['aspect_ratio']:.3f})")
        except Exception as _dim_err:
            job_warning(job_id, f"Dimension check failed for Clip #{index + 1}: {_dim_err}")

        # =====================
        # 5g'. ANTI-COPYRIGHT TRANSFORM
        # Speed +2%, pitch +1.5 semitones, hue+EQ tweak
        # Defeats Content ID frame/waveform fingerprinting
        # =====================
        try:
            from services.anti_copyright import apply_anti_fingerprint
            job_info(job_id, f"Applying anti-copyright fingerprint transform to Clip #{index + 1}...")
            apply_anti_fingerprint(final_path)  # in-place overwrite
            job_info(job_id, f"Anti-copyright transform done for Clip #{index + 1}")
        except Exception as _ac_err:
            job_warning(job_id, f"Anti-copyright transform failed (using original): {_ac_err}")

        # =====================
        # 5h. SAVE CLIP TO DB
        # =====================
        relative_final_path = os.path.relpath(final_path, settings.STORAGE_ROOT)
        relative_thumbnail_path = os.path.relpath(thumbnail_path, settings.STORAGE_ROOT) if thumbnail_result else None

        # A reclaimed job re-enters this stage for clips it already recorded (the
        # render stages skip on existing files). record_clip reuses that row so a
        # short already on YouTube keeps its youtube_video_id, and rewrites the
        # descriptive columns so the row says what this run actually cut.
        clip_db = record_clip(db, job_id, index, {
            "start": clip_start,
            "end": clip_end,
            "title": final_title,
            "hook": clip.get("hook"),
            # The A/B arm, so a later view-count sync can attribute the result.
            "hook_style": clip.get("hook_style"),
            "score": v_stats["virality_score_100"],
            "hook_score": clip.get("hook_score"),
            "coherence_score": clip.get("coherence_score"),
            "connection_score": clip.get("connection_score"),
            "trend_score": clip.get("trend_score"),
            "score_breakdown": {
                "llm": clip.get("llm_score_raw"),
                "audio": clip.get("audio_energy_score"),
                "keyword": clip.get("keyword_score"),
                "density": clip.get("semantic_density"),
                "structural": clip.get("structural_score"),
                # Where the window came from and what the second pass said about
                # it. The publish queue reads selection_path: only a model pick
                # goes out unattended.
                "selection_path": clip.get("selection_path"),
                "judge": clip.get("judge_score"),
                "judge_reason": clip.get("judge_reason"),
                "why": clip.get("why"),
            },
            "file_path": relative_final_path,
            "thumbnail_path": relative_thumbnail_path,
            "description": metadata.get("description", ""),
            "hashtags": metadata.get("hashtags", ["#Shorts"]),
            "platform_metadata": metadata.get("platform_copy", {}),
            "caption_style": caption_style,
            "layout_mode": resolved_layout,
        })
        clip_id = clip_db.id

        # The operator's manual trim lives in trim_start/trim_end, not in
        # final_video.mp4 (utils/clip_trim.py keeps it as a keyed sibling), so a
        # row re-recorded by a resumed run still describes a cut nobody applied
        # here. Without this the one path that publishes automatically is the one
        # path that can ship a short the operator cut out of the timeline.
        _reapply_recorded_trim(db, job_id, clip_db, clip_dir, clip_start, clip_end, index)

        job_info(job_id, f"Clip {index + 1}/{total_clips} saved to DB with ID {clip_id}")

        # =========================================================
        # 5i. AUTO-UPLOAD: YouTube joins the queue, TikTok publishes here
        # The YouTube clip is parked in the database rather than uploaded the
        # second it renders. Two clocks live in that row: publish_due_at is when
        # this machine uploads, scheduled_publish_at is the release YouTube is
        # handed as publishAt, so the cadence the channel is seen with holds even
        # while this PC is off.
        # =========================================================
        if getattr(job, "auto_upload", False) and _needs_review_before_publish(clip_db):
            job_warning(job_id, f"Clip {index + 1} was not chosen by a model "
                                f"(selection path: {clip.get('selection_path')}); it is rendered "
                                f"but not published automatically. Review it and press upload.")
        elif getattr(job, "auto_upload", False):
            try:
                if clip_db.youtube_video_id:
                    job_info(job_id, f"Clip {index + 1} already on YouTube ({clip_db.youtube_video_id}), skipping")
                else:
                    enqueue_clips_for_publish(db, job_id)
                    db.refresh(clip_db)
                    job_info(job_id, f"Clip {index + 1}/{total_clips} queued for YouTube, "
                                     f"upload {clip_db.publish_due_at:%Y-%m-%d %H:%M} UTC, "
                                     f"release {clip_db.scheduled_publish_at:%Y-%m-%d %H:%M} UTC")
            except Exception as yt_err:
                job_warning(job_id, f"Could not queue Clip #{index + 1} for YouTube: {yt_err}")

            # TikTok Immediate Upload
            try:
                from services.tiktok_upload import get_tiktok_auth_status, upload_clip_to_tiktok
                from utils.upload_pacing import await_for_slot
                tt_status = get_tiktok_auth_status()
                if tt_status.get("authenticated"):
                    # The upload used to leave no trace, so the
                    # on-completion sweep republished every clip.
                    if clip_db.tiktok_video_id:
                        job_info(job_id, f"Clip {index + 1} already on TikTok ({clip_db.tiktok_video_id}), skipping re-upload")
                    else:
                        waited = await await_for_slot("tiktok", stop=lease_stop)
                        if lease_stop is not None and lease_stop.is_set():
                            job_warning(job_id, f"Lease lost while pacing TikTok publish for Clip {index + 1}; leaving it unpublished")
                        else:
                            if waited:
                                job_info(job_id, f"⏳ Clip {index + 1}: waited {int(waited)} s since the last TikTok publish")
                            job_info(job_id, f"⚡ [Auto-Upload] Publishing Clip {index + 1}/{total_clips} to TikTok as '{tiktok_privacy}'...")
                            tt_res = upload_clip_to_tiktok(
                                video_path=final_path,
                                title=final_title,
                                description=metadata.get("description", ""),
                                hashtags=metadata.get("hashtags", ["#Shorts"]),
                                source_title=source_title,
                                privacy_level=tiktok_privacy
                            )
                            clip_db.tiktok_video_id = tt_res.get("video_id") or tt_res.get("tiktok_url")
                            db.commit()
                            db.refresh(clip_db)
                            where = tt_res.get("tiktok_url") or f"{tt_res.get('status')} ({tt_res.get('post_mode')})"
                            job_info(job_id, f"🎉 Clip {index + 1}/{total_clips} Published to TikTok: {where}")
            except Exception as tt_err:
                job_warning(job_id, f"Per-clip TikTok auto-upload skipped/failed for Clip #{index + 1}: {tt_err}")

    except Exception as e:
        err_msg = str(e)
        job_warning(job_id, f"Clip {index} failed: {err_msg}")
        clip_errors.append({"index": index, "start": clip_start, "end": clip_end, "error": err_msg})
        return


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
    """True for a clip no model chose, which must not publish unattended."""
    breakdown = clip.score_breakdown if isinstance(clip.score_breakdown, dict) else {}
    return breakdown.get("selection_path") in UNREVIEWED_SELECTION_PATHS


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


def _lease_heartbeat(lease_stop: threading.Event, job_id: int) -> None:
    """Refresh the lease while work is running.

    update_progress only fires at stage boundaries, and a single stage
    (transcribing a long source) can sit silent for longer than
    JOB_LEASE_MINUTES - without this the reclaim would requeue a live job.
    """
    while not lease_stop.wait(60):
        hb = None
        try:
            hb = SessionLocal()
            refreshed = touch_lease(hb, job_id)
            # Zero rows means the lease no longer names us: reclaim expired it
            # and another worker can now claim the same job while this one is
            # still rendering it. That has to be visible in the log.
            if refreshed == 0:
                # Not an error: this worker may have finished and marked the job
                # completed/failed itself. Log once, then get out of the way.
                if not lease_stop.is_set():
                    lease_stop.set()
                    job_warning(job_id, f"Lease no longer names {WORKER_ID}; a reclaim may "
                                        "have requeued this job while it was still running")
        except Exception as e:
            # A failed heartbeat is not an anomaly worth a stack trace, but a
            # run of them ends the lease, so say so instead of passing.
            job_debug(job_id, f"Heartbeat write failed: {e}")
        finally:
            if hb is not None:
                hb.close()


def make_progress_writer(db: Session, job: Job, job_id: int) -> Callable[[str, int], None]:
    """Return the progress writer this job reports through.
    Writes are throttled - only on a real change, or at most every 0.5s - and
    mirrored to Redis so the websocket does not have to poll the database.
    Bumping job.claimed_at on every write is what keeps the lease alive.
    """
    _last_prog_time = [0.0]
    _last_prog_pct = [-1]
    _last_prog_stage = [""]

    def update_progress(stage: str, percent: int):
        now = time.time()
        # Any stage boundary is proof the main thread is alive: the watchdog reads
        # this to tell a healthy long stage from a wedge.
        _touch_main_tick()
        # Throttle: write when the value changed, otherwise at most every 0.5s.
        if percent != _last_prog_pct[0] or stage != _last_prog_stage[0] or (now - _last_prog_time[0]) >= 0.5:
            _last_prog_time[0] = now
            _last_prog_pct[0] = percent
            _last_prog_stage[0] = stage
            try:
                progress = db.query(JobProgress).filter(JobProgress.job_id == job_id).first()
                if not progress:
                    progress = JobProgress(job_id=job_id, current_stage=stage, progress_percent=percent)
                    db.add(progress)
                else:
                    progress.current_stage = stage
                    progress.progress_percent = percent
                job.claimed_at = datetime.utcnow()
                db.commit()
            except Exception:
                db.rollback()

            r = _get_redis_client()
            if r:
                try:
                    r.publish(f"job_progress_{job_id}", json.dumps({"stage": stage, "percent": percent}))
                except Exception:
                    pass

    return update_progress


async def process_job_async(job_id: int):
    """
    Process a single job through the entire pipeline (async version).

    Args:
        job_id: The job ID to process

    Raises:
        Exception: If any step fails (caught by caller)
    """
    db = SessionLocal()
    job = db.query(Job).filter(Job.id == job_id).first()

    if not job:
        db.close()
        raise Exception(f"Job {job_id} not found")

    if not claim_job(db, job_id):
        db.close()
        job_info(job_id, f"Job {job_id} was not claimable (another worker owns it or it is not queued); skipping")
        return
    db.refresh(job)

    # Create job directory
    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    lease_stop = threading.Event()

    threading.Thread(target=_lease_heartbeat, args=(lease_stop, job_id), daemon=True).start()

    # Tell the watchdog this job is now in flight, and start its liveness clock.
    _CURRENT_JOB[0] = job_id
    _touch_main_tick()

    update_progress = make_progress_writer(db, job, job_id)

    job_info(job_id, f"Starting processing for job {job_id}")
    job_info(job_id, f"YouTube URL: {job.youtube_url}")

    # Visibility for every automatic upload. Hardcoding "public" meant an
    # auto-upload job went straight to a public feed with no way to review it.
    upload_privacy = (getattr(job, "upload_privacy", None)
                      or getattr(settings, "AUTO_UPLOAD_PRIVACY", "unlisted"))
    from services.tiktok_upload import tiktok_privacy_for
    tiktok_privacy = tiktok_privacy_for(upload_privacy)

    try:
        # =====================
        # 1. DOWNLOAD VIDEO
        # =====================
        video_path = await stage_download_source(job_id, job, job_dir, update_progress)
        # =====================
        # 2. EXTRACT AUDIO
        # =====================
        audio_path = await stage_extract_audio(job_id, job_dir, video_path, update_progress)

        # =====================
        # 3. TRANSCRIBE (multilingual / translate support)
        # =====================
        segments, segments_file, words_file, source_title, source_channel = \
            await stage_transcribe(job_id, job, job_dir, audio_path, update_progress)
        # =====================
        # 4. AUDIO ENERGY PRE-PASS (before clip selection)
        # =====================
        segments = await stage_score_audio_energy(job_id, video_path, segments,
                                                  segments_file, update_progress)
        # =====================
        # 4a. TEMPORAL EVENT LOCALIZATION (visual timeline for clip selection)
        # =====================
        temporal_events = await stage_temporal_localization(job_id, video_path, segments,
                                                            job_dir, update_progress)
        # =====================
        # 4b. SELECT CLIPS
        # =====================
        clips = await stage_select_clips(job_id, job, segments_file, source_title,
                                         update_progress, temporal_events=temporal_events)

        # =====================
        # 4c. SPEAKER DIARIZATION — only over the selected clip windows
        # =====================
        # Runs after selection because its only consumer is the per-clip reframe
        # (load_clip_diarization). Analysing the whole source wasted 40+ min/hour-long video.
        await stage_diarize(job_id, job_dir, video_path, clips, update_progress)

        # =====================
        # 5. PROCESS EACH CLIP
        # =====================
        clip_errors = []
        total_clips = len(clips)
        for index, clip in enumerate(clips):
            await process_clip(db, job, job_id, job_dir, index, clip, total_clips,
                               clip_errors, video_path, words_file, source_title,
                               source_channel, tiktok_privacy,
                               update_progress, lease_stop=lease_stop)
        # =====================
        # 6. COMPLETE JOB
        # =====================
        if clip_errors and len(clip_errors) >= len(clips):
            # Every clip failed — mark the job failed so the UI/n8n surfaces it
            job.status = "failed"
            job.error_message = f"All {len(clips)} clips failed. First error: {clip_errors[0]['error']}"
            job.updated_at = datetime.utcnow()
            db.commit()
            job_error(job_id, job.error_message)
        elif clip_errors:
            # Not a success. Calling this "completed" is what made a job with a
            # third of its clips missing read green in the Studio UI, and every
            # automatic path that keys off that status (api/clips.py's
            # pending-upload filter, api/realtime.py's terminal set, the pruner)
            # then treated a half-rendered job as a finished one.
            job.status = "completed_with_errors"
            job.error_message = f"{len(clip_errors)}/{len(clips)} clips failed: {', '.join(c['error'] for c in clip_errors[:3])}"

            # The clips that did render still go to the queue. This is a
            # database write, not a publish: the upload pass is what ships, and
            # it only ever reaches a row the operator opted in by setting
            # auto_upload on the job - the same thing the per-clip step already
            # did for every clip that rendered. The completion sweep is NOT run
            # here, because that half publishes to TikTok on the spot, and a
            # partial job is not something to push out unreviewed.
            if getattr(job, "auto_upload", False):
                try:
                    queued = enqueue_clips_for_publish(db, job_id)
                    job_info(job_id, f"Job {job_id}: queued {queued} surviving clip(s) "
                                     f"for YouTube despite {len(clip_errors)} clip failure(s)")
                except Exception as q_err:
                    job_warning(job_id, f"Could not queue the surviving clips: {q_err}")

            update_progress("Completed with errors", 100)
            job.updated_at = datetime.utcnow()
            db.commit()
            job_info(job_id, f"Job {job_id} completed with {len(clip_errors)} clip failure(s)")
        else:
            job.status = "completed"
            update_progress("Completed", 100)
            job.updated_at = datetime.utcnow()
            db.commit()
            job_info(job_id, f"Job {job_id} completed successfully")

            # =========================================================
            # 7. AUTO-UPLOAD ON COMPLETION (YouTube & TikTok)
            # =========================================================
            await stage_publish_all_clips(job_id, db, job,
                                          tiktok_privacy, update_progress,
                                          lease_stop=lease_stop)

    except Exception as e:
        # Mark job as failed
        job.status = "failed"
        job.error_message = str(e)
        job.updated_at = datetime.utcnow()
        db.commit()

        job_error(job_id, f"Job failed: {str(e)}")
        raise

    finally:
        lease_stop.set()
        # No job in flight again: the watchdog must not judge idle time as a stall.
        _CURRENT_JOB[0] = None
        db.close()

def process_job(job_id: int):
    """Synchronous wrapper for process_job_async."""
    asyncio.run(process_job_async(job_id))

from arq.connections import RedisSettings

async def arq_process_job(ctx, job_id: int):
    """Arq task queue worker entrypoint."""
    job_info(job_id, f"Processing job {job_id} via arq task queue")
    await process_job_async(job_id)

class WorkerSettings:
    """Arq worker configuration."""
    functions = [arq_process_job]
    redis_settings = RedisSettings(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379))
    )
    max_jobs = settings.WORKER_MAX_JOBS
    poll_delay = 0.5

def process_jobs():
    """
    Main worker loop (polling fallback or arq dispatcher).
    """
    logger.info("Worker started. Polling for jobs...")
    # The worker used to assume the API had booted first, because only
    # main.py's lifespan ran init_database(). Started on its own - or just
    # a few seconds ahead of the API - every query fails on a column the
    # models declare but the file does not have yet, and the loop logs that
    # error forever. The sync is additive and idempotent, so owning a copy
    # of it costs nothing.
    init_database()
    missing = log_media_tools()

    # Backstop against a wedged main thread: if process_job ever blocks with no
    # progress past the timeout, fail the job and exit so NSSM restarts us.
    threading.Thread(target=_stall_watchdog, args=(threading.Event(),), daemon=True).start()

    last_reclaim = 0.0

    while True:
        db = SessionLocal()

        try:
            # The publish queue drains on its own clock, ahead of the guards below:
            # a clip whose hour has come goes out even when this worker cannot start
            # a new render, because uploading needs neither ffmpeg nor free disk.
            try:
                publish_queued_clips(db)
            except Exception as queue_err:
                logger.error(f"YouTube publish queue failed: {queue_err}")
            if missing:
                # Claiming a job we cannot render would only burn it into
                # 'failed', so leave jobs queued until the tools appear.
                if not missing_media_tools():
                    missing = log_media_tools()
                time.sleep(5)
                continue
            if not has_headroom():
                # Same reasoning: a download that runs out of space mid-flight
                # leaves a truncated source.mp4 that every later stage then
                # fails on, so the queue waits for room instead.
                time.sleep(15)
                continue
            # A worker that died mid-job left the row at 'processing' forever and
            # the query below only looks at 'created', so it was never retried.
            if time.time() - last_reclaim >= 60:
                last_reclaim = time.time()
                recovered = reclaim_expired_leases(db)
                if recovered:
                    logger.warning(f"Requeued {recovered} job(s) with an expired lease")

            # Find next job to process
            job = (
                db.query(Job)
                .filter(Job.status == "created")
                .order_by(Job.created_at.asc())
                .first()
            )

            if not job:
                time.sleep(3)
                continue

            job_id = job.id
            db.close()  # Close DB before processing

            job_info(job_id, f"Found job {job_id}, starting processing...")

            # Process the job with error handling
            try:
                process_job(job_id)
            except Exception as e:
                job_error(job_id, f"Job processing failed: {str(e)}")
                # Error is already logged and DB updated in process_job
                # Continue to next job

        except Exception as e:
            logger.error(f"Worker polling error: {str(e)}")
            # Don't crash the worker, just log and continue

        finally:
            if 'db' in locals():
                try:
                    db.close()
                except:
                    pass

        # Small delay to prevent tight loop
        time.sleep(0.1)

if __name__ == "__main__":
    process_jobs()