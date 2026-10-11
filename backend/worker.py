"""
Worker process for AI Shorts Generator.
Polling-based job processor with comprehensive error handling and logging.
Uses async/await for Cloud API calls.
"""
import sys
import os

# Safe stream redirection for windowless / background daemon execution
if sys.stdout is None:
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "storage", "logs")
        os.makedirs(log_dir, exist_ok=True)
        sys.stdout = open(os.path.join(log_dir, "worker.log"), "a", encoding="utf-8")
    except Exception:
        sys.stdout = open(os.devnull, "w")

if sys.stderr is None:
    try:
        log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "storage", "logs")
        os.makedirs(log_dir, exist_ok=True)
        sys.stderr = open(os.path.join(log_dir, "worker_error.log"), "a", encoding="utf-8")
    except Exception:
        sys.stderr = open(os.devnull, "w")

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
from loguru import logger
from utils.logger import job_info, job_error, job_warning, job_debug
from services.youtube import download_youtube
from services.audio import extract_audio
from services.whisper import transcribe_audio_file
from services.clip_selector import select_clips
from services.audio_energy_scorer import score_segments as score_audio_energy
from services.smart_crop import smart_crop_video
from services.word_cropper import crop_words
from services.captions import create_captions
from services.dimension_checker import check_dimensions, log_dimension_warning
from services.render import burn_captions, mux_tts
from services.metadata import generate_metadata
from services.thumbnail import generate_thumbnail
from services.sfx import add_automatic_sfx

_REDIS_CLIENT = None
_REDIS_CHECKED = False

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
        "final_with_sfx.mp4", "popup_card.png", "captions.ass",
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

    _last_prog_time = [0.0]
    _last_prog_pct = [-1]
    _last_prog_stage = [""]

    def update_progress(stage: str, percent: int):
        now = time.time()
        # Only write to DB if stage changed, percent changed, or at least 0.5s passed (or at 0/100)
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
                db.commit()
            except Exception:
                db.rollback()

            r = _get_redis_client()
            if r:
                try:
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

        is_upload = bool(job.youtube_url and job.youtube_url.startswith("upload://"))
        if is_upload and os.path.exists(video_path):
            job_info(job_id, f"Preserving uploaded source video: {video_path}")
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
        # =====================
        # 2. EXTRACT AUDIO
        # =====================
        job_info(job_id, "Step 2/8: Extracting audio...")
        update_progress("Extracting audio...", 21)
        audio_path = os.path.join(str(job_dir), "audio.wav")

        if not os.path.exists(audio_path):
            audio_path = extract_audio(video_path)
            job_info(job_id, f"Audio extracted: {audio_path}")
        else:
            job_info(job_id, f"Audio already exists: {audio_path}")
        update_progress("Audio ready", 24)

        # =====================
        # 3. TRANSCRIBE (multilingual / translate support)
        # =====================
        job_info(job_id, "Step 3/8: Transcribing audio...")
        update_progress("Transcribing audio (0%)...", 25)
        segments_file = os.path.join(str(job_dir), "segments.json")
        words_file = os.path.join(str(job_dir), "words.json")

        if not os.path.exists(segments_file) or not os.path.exists(words_file):
            # Multilingual: task=translate produces English transcripts;
            # keep_native_transcript preserves the original language files.
            transcribe_task = getattr(job, "transcription_task", None) or "transcribe"
            source_language = getattr(job, "source_language", None) or None
            keep_native = bool(getattr(job, "keep_native_transcript", False))
            transcribe_audio_file(
                audio_path,
                str(job_dir),
                job.whisper_model,
                task=transcribe_task,
                language=source_language,
                keep_native=keep_native,
                progress_callback=update_progress,
            )
            job_info(job_id, f"Transcript created: {segments_file}, {words_file}")
        else:
            job_info(job_id, f"Transcript already exists")
            update_progress("Transcript loaded", 50)

        # Load segments for duration
        with open(segments_file, "r", encoding="utf-8") as f:
            segments = json.load(f)

        if getattr(job, "llm_translate", False):
            # Check detected language from whisper metadata
            is_english = False
            meta_path = os.path.join(str(job_dir), "transcript_metadata.json")
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                        if meta.get("language_code", "").lower() == "en":
                            is_english = True
                except:
                    pass

            if is_english:
                job_info(job_id, "Video is already in English. Skipping LLM translation.")
            else:
                job_info(job_id, "⚙️ Auto-Translate enabled: Running LLM Translation to English...")
                update_progress("Translating to English (LLM)...", 51)
                try:
                    from services.whisper import _llm_translate_segments, _translate_linear_words, _write_transcript_files
                    native_words = []
                    if os.path.exists(words_file):
                        with open(words_file, "r", encoding="utf-8") as f:
                            native_words = json.load(f)
                    
                    translated_segments = await _llm_translate_segments(
                        segments,
                        target_language="English",
                        source_language=source_language,
                    )
                    translated_words = _translate_linear_words(
                        native_words,
                        segments,
                        translated_segments,
                    )
                    _write_transcript_files(
                        str(job_dir),
                        translated_segments,
                        translated_words,
                        task="translate",
                        language="en",
                        native_segments_list=segments,
                        native_words_list=native_words,
                    )
                    # Swap loaded files for downstream clipping
                    segments = translated_segments
                    job_info(job_id, "LLM Translation successful")
                except Exception as e:
                    job_warning(job_id, f"LLM Translation failed ({e}), falling back to native transcript")

        video_duration = segments[-1]["end"] if segments else 0

        # Check if source video metadata exists
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

        # =====================
        # 4. AUDIO ENERGY PRE-PASS (before clip selection)
        # =====================
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

        # =====================
        # 4b. SELECT CLIPS
        # =====================
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
                    # Customize hook title to highlight the style for testing
                    if style == "aggressive":
                        clip["title"] = f"[AGGRESSIVE] {clip.get('title', 'Viral Moment')}"
                        clip["hook"] = f"YOU WON'T BELIEVE WHAT HAPPENED NEXT — {clip.get('hook', 'SHOCKING')}"
                    elif style == "curiosity":
                        clip["title"] = f"[CURIOUS] The Secret No One Talks About"
                        clip["hook"] = f"Wait until you hear what they hid — {clip.get('hook', 'SECRET')}"
                    elif style == "story-driven":
                        clip["title"] = f"[STORY] The Full Story Behind This Moment"
                        clip["hook"] = f"This is why that happened — {clip.get('hook', 'STORY')}"
                    job_info(job_id, f"Clip {i + 1}: Hook style '{style}' applied")

        # =====================
        # 5. PROCESS EACH CLIP
        # =====================
        clip_errors = []
        total_clips = len(clips)
        for index, clip in enumerate(clips):
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
                if layout_choice in ("auto", "adaptive", None, ""):
                    try:
                        from services.smart_crop import detect_adaptive_layout
                        resolved_layout = detect_adaptive_layout(short_path)
                        job_info(job_id, f"Auto-detected adaptive layout for clip {clip_idx}: '{resolved_layout}'")
                    except Exception as e:
                        job_info(job_id, f"Adaptive layout detection fallback: {e}")
                        resolved_layout = "single_speaker"
                else:
                    resolved_layout = layout_choice

                caption_style = getattr(job, "default_caption_style", None) or clip.get("caption_style") or "mrbeast"

                # 5c. Crop Words
                clip_words_file = os.path.join(str(clip_dir), "words.json")
                if not os.path.exists(clip_words_file):
                    crop_words(words_file, clip_start, clip_end, clip_words_file)
                    job_info(job_id, f"Words cropped: {clip_words_file}")
                else:
                    job_info(job_id, f"Words already cropped")

                # 5d. Generate ASS Captions with Selected Preset & Emojis
                captions_path = os.path.join(str(clip_dir), "captions.ass")
                if not os.path.exists(captions_path):
                    create_captions(clip_words_file, captions_path, style_name=caption_style)
                    job_info(job_id, f"Captions created with '{caption_style}' style: {captions_path}")
                else:
                    job_info(job_id, f"Captions already exist")

                final_path = os.path.join(str(clip_dir), "final_short.mp4")

                # ---- transcript (used for SFX selection + virality) ----
                clip_transcript = get_clip_transcript(words_file, clip_start, clip_end)

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

                        vertical_frag = build_vertical_filter(short_path, mode=resolved_layout)
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
                        job_info(job_id, f"Final video already exists")

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
                except Exception as e:
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

                clip_db = Clip(
                    job_id=job_id,
                    index=index,
                    start=clip_start,
                    end=clip_end,
                    title=final_title,
                    hook=clip.get("hook"),
                    score=v_stats.get("virality_score_100", 85.0),
                    hook_score=clip.get("hook_score"),
                    coherence_score=clip.get("coherence_score"),
                    connection_score=clip.get("connection_score"),
                    trend_score=clip.get("trend_score"),
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
                if getattr(job, "auto_upload", True):
                    # YouTube Immediate Upload
                    try:
                        from services.youtube_upload import get_youtube_auth_status, upload_clip_to_youtube
                        yt_status = get_youtube_auth_status()
                        if yt_status.get("authenticated"):
                            job_info(job_id, f"⚡ [Auto-Upload] Uploading Clip {index + 1}/{total_clips} directly to YouTube Shorts...")
                            yt_res = upload_clip_to_youtube(
                                video_path=final_path,
                                title=final_title,
                                description=metadata.get("description", ""),
                                hashtags=metadata.get("hashtags", ["#Shorts"]),
                                source_title=source_title,
                                privacy_status="public",
                                job_id=job_id
                            )
                            clip_db.uploaded = True
                            clip_db.youtube_video_id = yt_res.get("video_id")
                            clip_db.published_at = datetime.utcnow()
                            clip_db.upload_error = None
                            db.commit()
                            db.refresh(clip_db)
                            job_info(job_id, f"🎉 Clip {index + 1}/{total_clips} Live on YouTube: {yt_res.get('shorts_url')}")
                    except Exception as yt_err:
                        job_warning(job_id, f"Per-clip YouTube auto-upload skipped/failed for Clip #{index + 1}: {yt_err}")
                        clip_db.upload_error = str(yt_err)
                        db.commit()

                    # TikTok Immediate Upload
                    try:
                        from services.tiktok_upload import get_tiktok_auth_status, upload_clip_to_tiktok
                        tt_status = get_tiktok_auth_status()
                        if tt_status.get("authenticated"):
                            job_info(job_id, f"⚡ [Auto-Upload] Publishing Clip {index + 1}/{total_clips} directly to TikTok...")
                            tt_res = upload_clip_to_tiktok(
                                video_path=final_path,
                                title=final_title,
                                description=metadata.get("description", ""),
                                hashtags=metadata.get("hashtags", ["#Shorts"]),
                                source_title=source_title,
                                privacy_level="PUBLIC_TO_EVERYONE"
                            )
                            job_info(job_id, f"🎉 Clip {index + 1}/{total_clips} Published to TikTok: {tt_res.get('tiktok_url')}")
                    except Exception as tt_err:
                        job_warning(job_id, f"Per-clip TikTok auto-upload skipped/failed for Clip #{index + 1}: {tt_err}")

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

            # =========================================================
            # 7. AUTO-UPLOAD ON COMPLETION (YouTube & TikTok)
            # =========================================================
            if getattr(job, "auto_upload", False):
                try:
                    job_info(job_id, f"Auto-upload triggered for Job #{job_id}...")
                    update_progress("⚡ Auto-uploading clips to YouTube & TikTok...", 100)

                    # Auto upload to YouTube if authenticated
                    try:
                        from services.youtube_upload import upload_all_clips_to_youtube_for_job, get_auth_status
                        yt_status = get_auth_status()
                        if yt_status.get("authenticated"):
                            job_info(job_id, f"Auto-uploading to YouTube Shorts...")
                            upload_all_clips_to_youtube_for_job(job_id, db, privacy_status="public")
                            job_info(job_id, f"YouTube Shorts auto-upload complete for Job #{job_id}")
                    except Exception as yt_err:
                        job_warning(job_id, f"YouTube auto-upload failed: {yt_err}")

                    # Auto upload to TikTok
                    try:
                        from services.tiktok_upload import upload_all_clips_to_tiktok_for_job
                        job_info(job_id, f"Auto-uploading to TikTok...")
                        upload_all_clips_to_tiktok_for_job(job_id, db, privacy_level="PUBLIC_TO_EVERYONE")
                        job_info(job_id, f"TikTok auto-upload complete for Job #{job_id}")
                    except Exception as tt_err:
                        job_warning(job_id, f"TikTok auto-upload failed: {tt_err}")

                except Exception as auto_err:
                    job_warning(job_id, f"Auto-upload execution encountered an error: {auto_err}")

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
    logger.info("Worker started. Polling for jobs...")

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