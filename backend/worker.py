"""
Worker process for AI Shorts Generator.
Polling-based job processor with comprehensive error handling and logging.
Uses async/await for Cloud API calls.
"""
import os

# Must run before any import that pulls in loguru: see utils/daemon_streams.py
from utils.daemon_streams import redirect_daemon_streams

redirect_daemon_streams("worker")

import time
import json
import asyncio
import socket
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session
from database.session import SessionLocal, init_database
from database.models import Job, Clip, JobProgress
from config import settings
from loguru import logger
from utils.logger import job_info, job_error, job_warning, job_debug
from utils.env_check import log_media_tools, missing_media_tools
from utils.disk import has_headroom, reclaim
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
from services.metadata import generate_metadata
from services.thumbnail import generate_thumbnail
from services.sfx import add_automatic_sfx

_REDIS_CLIENT = None
_REDIS_CHECKED = False

WORKER_ID = f"{socket.gethostname()}:{os.getpid()}"


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

def _clear_derived_artifacts(job_dir, job_id):
    """Remove per-job files that derive from the source video, so a re-downloaded
    source is never paired with stale audio/transcript/timing or old clip cuts
    (the "wrong subtitles from an old video" bug)."""
    import shutil
    stale_files = [
        "audio.wav", "words.json", "segments.json", "transcript.txt",
        "short.mp4", "vertical.mp4", "final_short.mp4", "final_with_title.mp4",
        "final_with_sfx.mp4", "popup_card.png", "captions.ass", "diarization.json",
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

    if not os.path.exists(segments_file) or not os.path.exists(words_file):
        transcribe_audio_file(
            audio_path,
            str(job_dir),
            job.whisper_model,
            task=transcribe_task,
            language=source_language,
            keep_native=keep_native,
            progress_callback=update_progress,
        )
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


async def stage_diarize(job_id: int, job_dir: Path, video_path: str, update_progress: Callable[[str, int], None]) -> list:
    """Who speaks when across the source, written to diarization.json.

    Soft by design: a missing HF token, a gated model, or an undecodable
    source leaves the job with no speaker turns and a logged warning — every
    later stage reads 'no diarization' as the old behavior, not as an error.
    """
    diarization_file = os.path.join(str(job_dir), "diarization.json")
    if os.path.exists(diarization_file):
        try:
            with open(diarization_file, "r", encoding="utf-8") as f:
                turns = (json.load(f) or {}).get("turns") or []
            job_info(job_id, f"Diarization already on disk: {len(turns)} speaker turns (resume)")
            return turns
        except Exception as e:
            job_warning(job_id, f"Could not read existing diarization.json ({e}); re-running diarization")

    if not settings.DIARIZATION_ENABLED:
        job_info(job_id, "Step 3b/8: Speaker diarization disabled (DIARIZATION_ENABLED=false), skipping")
        return []

    job_info(job_id, "Step 3b/8: Speaker diarization (pyannote, CPU)...")
    update_progress("Identifying speakers...", 51)
    try:
        from services.diarization import try_diarize
        turns = try_diarize(video_path)
    except Exception as e:
        # try_diarize is soft-fail by contract; the stage must be too.
        turns = None
        job_warning(job_id, f"Diarization stage error ({e}); continuing without speaker turns")
    if turns is None:
        job_warning(job_id, "Speaker diarization unavailable; layouts fall back to motion/object heuristics")
        return []
    try:
        with open(diarization_file, "w", encoding="utf-8") as f:
            json.dump({"model": settings.DIARIZATION_MODEL, "turns": turns}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        job_warning(job_id, f"Diarization computed but could not be written to disk: {e}")
    job_info(job_id, f"Diarization: {len(turns)} speaker turns")
    return turns


async def stage_scene_attention(job_id: int, clip_dir, short_path: str, clip_words_file: str,
                                 clip_transcript: str) -> Optional[dict]:
    """What each scene of this clip is about, for the reframe decision.

    The keyword tier is local and always cheap; the text tier is one gateway call
    per clip, and the vision tier is off unless it is explicitly switched on. The
    whole stage is skipped when SCENE_ATTENTION_ENABLED is false, and any failure
    — a dead gateway, an unreadable transcript — degrades to None with a log line,
    leaving the crop on the person heuristics it already used.
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
        segments = score_audio_energy(str(video_path), segments)
        job_info(job_id, f"Audio energy scored for {len(segments)} segments")
        # Persist enriched segments so select_clips can read audio_energy_score
        with open(segments_file, "w", encoding="utf-8") as f:
            json.dump(segments, f, ensure_ascii=False, indent=2)
    except Exception as _ae:
        job_warning(job_id, f"Audio energy pre-pass failed ({_ae}), continuing with neutral 0.5")
    return segments


async def stage_select_clips(job_id: int, job: Job, segments_file: str, source_title: Optional[str], update_progress: Callable[[str, int], None]) -> list:
    """Pick the clips to render and, for a diagnostic job, vary their hooks.
    Returns the clip dicts the render loop consumes.
    """
    job_info(job_id, f"Step 4b/8: Selecting best clips (source title: {source_title or 'N/A'})...")
    update_progress("Analyzing virality & selecting top clips...", 52)
    
    num_target_clips = job.target_clips if (job and getattr(job, "target_clips", None)) else settings.NUM_CLIPS
    clips = await select_clips(segments_file, num_target_clips, video_title=source_title)
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


async def process_clip(db: Session, job: Job, job_id: int, job_dir: Path, index: int, clip: dict, total_clips: int, clip_errors: list, video_path: str, words_file: str, source_title: Optional[str], source_channel: Optional[str], upload_privacy: str, tiktok_privacy: str, update_progress: Callable[[str, int], None], lease_stop: Optional[threading.Event] = None) -> None:
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
        if not os.path.exists(short_path):
            cut_clip(video_path, clip_start, clip_end, short_path)
            job_info(job_id, f"Clip cut: {short_path}")
        else:
            job_info(job_id, f"Clip already exists: {short_path}")

        # =====================
        # 5b-5e. RENDER: SINGLE-PASS UNIFIED (Task D) OR SEQUENTIAL FALLBACK
        # =====================
        # User's chosen preset on the job ALWAYS takes precedence to keep all clips consistent
        layout_choice = getattr(job, "default_layout_mode", None) or clip.get("layout") or "auto"
        caption_style = getattr(job, "default_caption_style", None) or clip.get("caption_style") or "mrbeast"

        # 5c. Crop Words
        clip_words_file = os.path.join(str(clip_dir), "words.json")
        if not os.path.exists(clip_words_file):
            crop_words(words_file, clip_start, clip_end, clip_words_file)
            job_info(job_id, f"Words cropped: {clip_words_file}")
        else:
            job_info(job_id, "Words already cropped")

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
        captions_path = os.path.join(str(clip_dir), "captions.ass")
        if not os.path.exists(captions_path):
            create_captions(clip_words_file, captions_path, style_name=caption_style)
            job_info(job_id, f"Captions created with '{caption_style}' style: {captions_path}")
        else:
            job_info(job_id, "Captions already exist")

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

        # ---- Render: single-pass unified OR sequential fallback ----
        _used_single_pass = False
        if getattr(settings, "SINGLE_PASS_RENDER", False):
            try:
                from services.vertical import build_vertical_filter
                from services.sfx import pick_sfx_for_clip, _find_sfx_file
                from services.render import render_single_pass as _rsp

                vertical_frag = build_vertical_filter(short_path, mode=resolved_layout,
                                                      diarization=clip_turns or None, attention=clip_attention)
                sfx_list = pick_sfx_for_clip(clip.get("title", "") or "", clip_transcript, job.style_prompt or "")
                sfx_files = []
                sfx_ts = []
                for name in sfx_list:
                    p = _find_sfx_file(name)
                    if p:
                        sfx_files.append(p)
                        sfx_ts.append(0.3 if len(sfx_files) == 1 else max(1.5, (clip_end - clip_start) / 2.0))
                _title = clip.get("title") or "Viral Moment"
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
            if not os.path.exists(vertical_path):
                try:
                    smart_crop_video(short_path, vertical_path, mode=resolved_layout)
                    job_info(job_id, f"Vertical conversion ({resolved_layout}): {vertical_path}")
                except Exception as e:
                    job_warning(job_id, f"Vertical conversion failed, using original short: {str(e)}")
                    vertical_path = short_path
                    job_info(job_id, f"Vertical conversion fallback: {vertical_path}")
            else:
                job_info(job_id, f"Vertical already exists: {vertical_path}")

            if not os.path.exists(final_path):
                burn_captions(vertical_path, captions_path, final_path)
                job_info(job_id, f"Final video (sequential): {final_path}")
            else:
                job_info(job_id, "Final video already exists")

        # single-pass path: set vertical_path to final for virality call
        if _used_single_pass:
            vertical_path = final_path

        # =====================
        # 5f. GENERATE METADATA
        # =====================
        job_info(job_id, "Generating metadata...")
        clip_transcript = get_clip_transcript(words_file, clip_start, clip_end)
        metadata = await generate_metadata(
            clip_transcript,
            clip.get("title", ""),
            clip.get("hook", "")
        )

        # Compute Active Expression & Multi-modal Virality Analysis
        try:
            from services.virality_detector import compute_composite_virality
            v_stats = compute_composite_virality(clip.get("virality_score", 0.8), clip_transcript, vertical_path)
            metadata["virality_breakdown"] = v_stats
            job_info(job_id, f"Clip Virality Score: {v_stats['virality_score_100']}/100 (Visual: {v_stats['visual_intensity']})")
        except Exception:
            v_stats = {"virality_score_100": 85.0, "virality_score": 0.85}

        # Generate Platform-Specific Copy (TikTok, Instagram Reels, YouTube Shorts)
        from services.copy_generator import generate_platform_copies
        platform_copy = await generate_platform_copies(
            clip_transcript,
            metadata.get("title") or clip.get("title", "Viral Moment")
        )
        metadata["platform_copy"] = platform_copy

        # Save metadata
        metadata_path = os.path.join(str(clip_dir), "metadata.json")
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

        job_info(job_id, f"Metadata generated: {metadata_path}")

        # Add top TikTok title overlay ONCE at the end of clip processing
        final_title = metadata.get("title") or clip.get("title") or "Viral Moment"
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
        keep_files = {"final_video.mp4", "thumbnail.jpg", "metadata.json", "storyboard.jpg", "words.json"}
        for entry in os.listdir(str(clip_dir)):
            if entry not in keep_files and (entry.endswith(".mp4") or entry.endswith(".ass") or entry.endswith(".wav")):
                fp = os.path.join(str(clip_dir), entry)
                try:
                    os.remove(fp)
                except Exception:
                    pass

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
        # render stages skip on existing files). Reuse that row: a fresh Clip()
        # has no youtube_video_id, so the re-upload guards below would never fire
        # and a short already on YouTube would be published a second time.
        clip_db = db.query(Clip).filter(Clip.job_id == job_id, Clip.index == index).first()
        if clip_db is None:
            clip_db = Clip(
                job_id=job_id,
                index=index,
                start=clip_start,
                end=clip_end,
                title=final_title,
                hook=clip.get("hook"),
                # The A/B arm, so a later view-count sync can attribute the result.
                hook_style=clip.get("hook_style"),
                score=v_stats.get("virality_score_100", 85.0),
                hook_score=clip.get("hook_score"),
                coherence_score=clip.get("coherence_score"),
                connection_score=clip.get("connection_score"),
                trend_score=clip.get("trend_score"),
                score_breakdown={
                    "llm": clip.get("llm_score_raw"),
                    "audio": clip.get("audio_energy_score"),
                    "keyword": clip.get("keyword_score"),
                    "density": clip.get("semantic_density"),
                    "structural": clip.get("structural_score"),
                },
                file_path=relative_final_path,
                thumbnail_path=relative_thumbnail_path,
                description=metadata.get("description", ""),
                hashtags=metadata.get("hashtags", ["#Shorts"]),
                platform_metadata=metadata.get("platform_copy", {}),
                caption_style=caption_style,
                layout_mode=resolved_layout,
                uploaded=False,
                upload_retry_count=0
            )

            db.add(clip_db)
            db.commit()
            db.refresh(clip_db)
        clip_id = clip_db.id

        job_info(job_id, f"Clip {index + 1}/{total_clips} saved to DB with ID {clip_id}")

        # =========================================================
        # 5i. IMMEDIATE PER-CLIP AUTO-UPLOAD (YouTube & TikTok)
        # Uploads progressively as each clip renders (prevents batch bursts)
        # =========================================================
        if getattr(job, "auto_upload", False):
            # YouTube Immediate Upload
            try:
                from services.youtube_upload import get_youtube_auth_status, upload_clip_to_youtube
                from utils.upload_pacing import await_for_slot
                yt_status = get_youtube_auth_status()
                if yt_status.get("authenticated"):
                    if clip_db.youtube_video_id:
                        job_info(job_id, f"Clip {index + 1} already on YouTube ({clip_db.youtube_video_id}), skipping re-upload")
                    else:
                        waited = await await_for_slot("youtube", stop=lease_stop)
                        if lease_stop is not None and lease_stop.is_set():
                            job_warning(job_id, f"Lease lost while pacing Clip {index + 1}; leaving it unpublished rather than racing another worker")
                        else:
                            if waited:
                                job_info(job_id, f"⏳ Clip {index + 1} rendered; waited {int(waited)} s since the last publish to keep the channel's cadence human")
                                update_progress(f"⏳ Clip {index + 1}/{total_clips} ready — spacing the publish ({int(waited)} s)...", current_clip_pct)
                            job_info(job_id, f"⚡ [Auto-Upload] Uploading Clip {index + 1}/{total_clips} to YouTube Shorts as '{upload_privacy}'...")
                            yt_res = upload_clip_to_youtube(
                                video_path=final_path,
                                title=final_title,
                                description=metadata.get("description", ""),
                                hashtags=metadata.get("hashtags", ["#Shorts"]),
                                source_title=source_title,
                                privacy_status=upload_privacy,
                                job_id=job_id
                            )
                            clip_db.uploaded = True
                            clip_db.youtube_video_id = yt_res.get("video_id")
                            clip_db.published_at = datetime.utcnow()
                            clip_db.upload_error = None
                            db.commit()
                            db.refresh(clip_db)
                            job_info(job_id, f"🎉 Clip {index + 1}/{total_clips} on YouTube: {yt_res.get('shorts_url')}")
            except Exception as yt_err:
                job_warning(job_id, f"Per-clip YouTube auto-upload skipped/failed for Clip #{index + 1}: {yt_err}")
                clip_db.upload_error = str(yt_err)
                db.commit()

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


async def stage_publish_all_clips(job_id: int, db: Session, job: Job, upload_privacy: str, tiktok_privacy: str, update_progress: Callable[[str, int], None], lease_stop: Optional[threading.Event] = None) -> None:
    """Publish every rendered clip when the job asked for it on completion.
    Runs only for a job where every clip rendered: a partially failed job is
    left for a human to review, never swept into a channel automatically.
    """
    if getattr(job, "auto_upload", False):
        try:
            job_info(job_id, f"Auto-upload triggered for Job #{job_id}...")
            update_progress("⚡ Auto-uploading clips to YouTube & TikTok...", 100)

            # Auto upload to YouTube if authenticated
            try:
                from services.youtube_upload import upload_all_clips_for_job, get_youtube_auth_status
                yt_status = get_youtube_auth_status()
                if yt_status.get("authenticated"):
                    job_info(job_id, "Auto-uploading to YouTube Shorts...")
                    upload_all_clips_for_job(job_id, db, privacy_status=upload_privacy,
                                             pace=True, stop=lease_stop)
                    job_info(job_id, f"YouTube Shorts auto-upload complete for Job #{job_id}")
            except Exception as yt_err:
                job_warning(job_id, f"YouTube auto-upload failed: {yt_err}")

            # Auto upload to TikTok
            try:
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
        # 3b. SPEAKER DIARIZATION (who talks when, for the reframe decisions)
        # =====================
        await stage_diarize(job_id, job_dir, video_path, update_progress)
        # =====================
        # 4. AUDIO ENERGY PRE-PASS (before clip selection)
        # =====================
        segments = await stage_score_audio_energy(job_id, video_path, segments,
                                                  segments_file, update_progress)
        # =====================
        # 4b. SELECT CLIPS
        # =====================
        clips = await stage_select_clips(job_id, job, segments_file, source_title,
                                         update_progress)

        # =====================
        # 5. PROCESS EACH CLIP
        # =====================
        clip_errors = []
        total_clips = len(clips)
        for index, clip in enumerate(clips):
            await process_clip(db, job, job_id, job_dir, index, clip, total_clips,
                               clip_errors, video_path, words_file, source_title,
                               source_channel, upload_privacy, tiktok_privacy,
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
            job.status = "completed"
            job.error_message = f"{len(clip_errors)}/{len(clips)} clips failed: {', '.join(c['error'] for c in clip_errors[:3])}"
            update_progress("Completed", 100)
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
            await stage_publish_all_clips(job_id, db, job, upload_privacy,
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

    last_reclaim = 0.0

    while True:
        db = SessionLocal()

        try:
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