"""
Worker process for AI Shorts Generator.
Polling-based job processor with comprehensive error handling and logging.
Uses async/await for Cloud API calls.
"""
import os
import time
import json
import asyncio
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from database.session import SessionLocal
from database.models import Job, Clip, JobProgress
from config import settings
from utils.logger import job_info, job_error, job_warning, job_debug
from services.youtube import download_youtube
from services.audio import extract_audio
from services.whisper import transcribe_audio_file
from services.clip_selector import select_best_clips
from services.smart_crop import smart_crop_video
from services.word_cropper import crop_words
from services.captions import create_captions
from services.render import burn_captions, mux_tts
from services.metadata import generate_metadata
from services.thumbnail import generate_thumbnail
from services.audio_polish import polish_audio
from services.broll import add_broll_overlay
from services.sfx import add_automatic_sfx

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
        "short.mp4", "vertical.mp4", "final_short.mp4", "captions.ass",
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
        return ""

    if isinstance(words, dict) and "words" in words:
        words = words["words"]

    clip_words = []
    for word in words:
        w_start = word.get("start", 0)
        w_end = word.get("end", 0)
        if w_start >= clip_start and w_end <= clip_end:
            clip_words.append(word.get("word", ""))

    return " ".join(clip_words)

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
        raise Exception(f"Job {job_id} not found")

    # Create job directory
    job_dir = Path(settings.JOBS_DIR) / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)

    def update_progress(stage: str, percent: int):
        progress = db.query(JobProgress).filter(JobProgress.job_id == job_id).first()
        if not progress:
            progress = JobProgress(job_id=job_id, current_stage=stage, progress_percent=percent)
            db.add(progress)
        else:
            progress.current_stage = stage
            progress.progress_percent = percent
        db.commit()
        # Publish to Redis (graceful if Redis is offline)
        try:
            import redis
            r = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)))
            r.publish(f"job_progress_{job_id}", json.dumps({"stage": stage, "percent": percent}))
        except Exception:
            pass

    job_info(job_id, f"Starting processing for job {job_id}")
    job_info(job_id, f"YouTube URL: {job.youtube_url}")

    try:
        # Update job status
        job.status = "processing"
        job.updated_at = datetime.utcnow()
        db.commit()

        # =====================
        # 1. DOWNLOAD VIDEO
        # =====================
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

        if os.path.exists(video_path) and cached_hash == _url_fingerprint(job.youtube_url):
            job_info(job_id, f"Reusing cached source for same URL: {video_path}")
        else:
            if os.path.exists(video_path):
                job_info(job_id, "URL changed or unknown — re-downloading source")
                os.remove(video_path)
            # Derived artifacts (audio/transcript/timing + old clip cuts) were made
            # from a previous source — drop them so the re-run renders fully fresh.
            _clear_derived_artifacts(job_dir, job_id)
            video_path = download_youtube(job.youtube_url, job_id)
            job_info(job_id, f"Downloaded: {video_path}")

        with open(fp_path, "w", encoding="utf-8") as f:
            f.write(_url_fingerprint(job.youtube_url))
        # =====================
        # 2. EXTRACT AUDIO
        # =====================
        job_info(job_id, "Step 2/8: Extracting audio...")
        update_progress("Extracting audio", 20)
        audio_path = os.path.join(str(job_dir), "audio.wav")


        if not os.path.exists(audio_path):
            audio_path = extract_audio(video_path)
            job_info(job_id, f"Audio extracted: {audio_path}")
        else:
            job_info(job_id, f"Audio already exists: {audio_path}")

        # =====================
        # 3. TRANSCRIBE
        # =====================
        job_info(job_id, "Step 3/8: Transcribing audio...")
        update_progress("Transcribing audio", 30)
        segments_file = os.path.join(str(job_dir), "segments.json")

        words_file = os.path.join(str(job_dir), "words.json")

        if not os.path.exists(segments_file) or not os.path.exists(words_file):
            # transcribe_audio_file is synchronous — call directly
            transcribe_audio_file(audio_path, str(job_dir), job.whisper_model)
            job_info(job_id, f"Transcript created: {segments_file}, {words_file}")
        else:
            job_info(job_id, f"Transcript already exists")

        # Load segments for duration
        with open(segments_file, "r", encoding="utf-8") as f:
            segments = json.load(f)

        video_duration = segments[-1]["end"] if segments else 0

        # =====================
        # 4. SELECT CLIPS
        # =====================
        job_info(job_id, "Step 4/8: Selecting best clips...")
        update_progress("Selecting clips", 40)
        clips = await select_best_clips(segments_file, settings.NUM_CLIPS)

        job_info(job_id, f"Selected {len(clips)} clips")

        # =====================
        # 5. PROCESS EACH CLIP
        # =====================
        clip_errors = []
        for index, clip in enumerate(clips):
            clip_start = clip["start"]
            clip_end = clip["end"]
            clip_id = None  # Will be set after DB insert

            job_info(job_id, f"Processing clip {index + 1}/{len(clips)} [{clip_start:.1f}s - {clip_end:.1f}s]")

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
                # 5b. VERTICAL CONVERSION
                # =====================
                vertical_path = os.path.join(str(clip_dir), "vertical.mp4")
                if not os.path.exists(vertical_path):
                    try:
                        smart_crop_video(short_path, vertical_path)
                        job_info(job_id, f"Vertical conversion: {vertical_path}")
                    except Exception as e:
                        job_warning(job_id, f"Vertical conversion failed, using original short: {str(e)}")
                        vertical_path = short_path
                        job_info(job_id, f"Vertical conversion fallback: {vertical_path}")
                else:
                    job_info(job_id, f"Vertical already exists: {vertical_path}")

                # =====================
                # 5c. CROP WORDS
                # =====================
                clip_words_file = os.path.join(str(clip_dir), "words.json")
                if not os.path.exists(clip_words_file):
                    crop_words(words_file, clip_start, clip_end, clip_words_file)
                    job_info(job_id, f"Words cropped: {clip_words_file}")
                else:
                    job_info(job_id, f"Words already cropped")

                # =====================
                # 5d. GENERATE CAPTIONS
                # =====================
                captions_path = os.path.join(str(clip_dir), "captions.ass")
                if not os.path.exists(captions_path):
                    create_captions(clip_words_file, captions_path)
                    job_info(job_id, f"Captions created: {captions_path}")
                else:
                    job_info(job_id, f"Captions already exist")

                # =====================
                # 5e. BURN CAPTIONS
                # =====================
                final_path = os.path.join(str(clip_dir), "final_short.mp4")
                if not os.path.exists(final_path):
                    burn_captions(vertical_path, captions_path, final_path)
                    job_info(job_id, f"Final video: {final_path}")
                else:
                    job_info(job_id, f"Final video already exists")

                broll_path = os.path.join(str(clip_dir), "broll_overlay.mp4")
                if not os.path.exists(broll_path):
                    try:
                        title_text = clip.get("title") or "Viral Moment"
                        if job.style_prompt:
                            title_text = f"{title_text} ({job.style_prompt})"
                        add_broll_overlay(final_path, broll_path, title=title_text)
                        final_path = broll_path
                        job_info(job_id, f"B-roll overlay added: {broll_path}")
                    except Exception as e:
                        job_warning(job_id, f"B-roll overlay failed: {str(e)}")

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
                try:
                    # New: professional motion-graphic title (renders a clean PNG,
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
                # 5g. ADD AUTOMATIC SFX
                # =====================
                sfx_path = os.path.join(str(clip_dir), "final_with_sfx.mp4")
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

                # =====================
                # 5h. GENERATE THUMBNAIL & STORYBOARD
                # =====================
                job_info(job_id, "Generating thumbnail & storyboard...")
                thumbnail_path = os.path.join(str(clip_dir), "thumbnail.jpg")
                thumbnail_result = generate_thumbnail(
                    final_path,
                    thumbnail_path,
                    final_title
                )

                try:
                    from services.storyboard import generate_storyboard
                    generate_storyboard(final_path, str(clip_dir))
                except Exception as e:
                    job_warning(job_id, f"Storyboard generation failed: {str(e)}")


                                # CLEANUP intermediary files
                final_video_path = os.path.join(str(clip_dir), "final_video.mp4")
                if os.path.exists(final_path):
                    import shutil
                    shutil.move(final_path, final_video_path)
                    final_path = final_video_path
                
                for f in ["short.mp4", "vertical.mp4", "captions.ass", "final_short.mp4", "final_with_title.mp4", "final_with_sfx.mp4", "final_with_tts.mp4"]:
                    fp = os.path.join(str(clip_dir), f)
                    if os.path.exists(fp):
                        try:
                            os.remove(fp)
                        except:
                            pass

                # =====================
                # 5h. SAVE CLIP TO DB
                # =====================
                relative_final_path = os.path.relpath(final_path, settings.STORAGE_ROOT)
                relative_thumbnail_path = os.path.relpath(thumbnail_path, settings.STORAGE_ROOT) if thumbnail_result else None

                clip_db = Clip(
                    job_id=job_id,
                    index=index,
                    start=clip_start,
                    end=clip_end,
                    title=final_title,
                    hook=clip.get("hook"),
                    score=clip.get("score") or clip.get("virality_score"),
                    hook_score=clip.get("hook_score"),
                    coherence_score=clip.get("coherence_score"),
                    connection_score=clip.get("connection_score"),
                    trend_score=clip.get("trend_score"),
                    file_path=relative_final_path,
                    thumbnail_path=relative_thumbnail_path,
                    description=metadata.get("description", ""),
                    hashtags=metadata.get("hashtags", ["#Shorts"]),
                    platform_metadata=metadata.get("platform_copy", {}),
                    uploaded=False,
                    upload_retry_count=0
                )

                db.add(clip_db)
                db.commit()
                db.refresh(clip_db)
                clip_id = clip_db.id

                job_info(job_id, f"Clip {index} saved to DB with ID {clip_id}")

            except Exception as e:
                err_msg = str(e)
                job_warning(job_id, f"Clip {index} failed: {err_msg}")
                clip_errors.append({"index": index, "start": clip_start, "end": clip_end, "error": err_msg})
                continue
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

    except Exception as e:
        # Mark job as failed
        job.status = "failed"
        job.error_message = str(e)
        job.updated_at = datetime.utcnow()
        db.commit()

        job_error(job_id, f"Job failed: {str(e)}")
        raise

    finally:
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
    max_jobs = 2
    poll_delay = 0.5

def process_jobs():
    """
    Main worker loop (polling fallback or arq dispatcher).
    """
    job_info(0, "Worker started. Polling for jobs every 5 seconds...")

    while True:
        db = SessionLocal()

        try:
            # Find next job to process
            job = (
                db.query(Job)
                .filter(Job.status == "created")
                .order_by(Job.created_at.asc())
                .first()
            )

            if not job:
                job_info(0, "No jobs waiting...")
                time.sleep(5)
                continue

            job_id = job.id
            db.close()  # Close DB before processing

            job_info(0, f"Found job {job_id}, starting processing...")

            # Process the job with error handling
            try:
                process_job(job_id)
            except Exception as e:
                job_error(job_id, f"Job processing failed: {str(e)}")
                # Error is already logged and DB updated in process_job
                # Continue to next job

        except Exception as e:
            job_error(0, f"Worker error: {str(e)}")
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