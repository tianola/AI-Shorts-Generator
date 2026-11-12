"""
Database models for AI Shorts Generator.
SQLAlchemy ORM models with proper relationships.
"""
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, Float, JSON, ForeignKey, Index
from sqlalchemy.orm import relationship
from datetime import datetime
from database.base import Base
from database.migrations import CLIP_UNIQUE_INDEX

# Statuses a job sits at when no worker is working on it any more. The worker
# writes 'completed_with_errors' for a run where some clips rendered and some
# did not, so every place that used to compare against 'completed' has to know
# about both shapes or it will keep a finished job in the live set: the
# websocket poller never stops, the pruner never reclaims the source, and the
# Resume button has no path back.
TERMINAL_JOB_STATUSES = ("completed", "completed_with_errors", "failed")

class Job(Base):
    """Represents a video processing job."""
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    youtube_url = Column(String, nullable=False)
    style_prompt = Column(Text, nullable=True)
    status = Column(String, default="created")  # created|processing|completed|completed_with_errors|failed
    # Lease bookkeeping. A worker that died mid-job left the row at 'processing'
    # forever, because the poller only queries 'created' and never reclaimed it.
    claimed_by = Column(String, nullable=True)
    claimed_at = Column(DateTime, nullable=True)
    error_message = Column(Text, nullable=True)
    channel_id = Column(String, nullable=True)  # Future: multi-channel support
    whisper_model = Column(String, nullable=True, default="tiny")
    default_caption_style = Column(String, nullable=True, default="mrbeast")
    default_layout_mode = Column(String, nullable=True, default="auto")
    default_karaoke = Column(Boolean, nullable=True, default=True)
    default_motion_title = Column(Boolean, nullable=True, default=True)
    # Multilingual / translation options (v1: worker + API surface)
    transcription_task = Column(String, nullable=True, default="transcribe")
    source_language = Column(String, nullable=True, default=None)
    keep_native_transcript = Column(Boolean, nullable=True, default=False)
    # Content safety: smart blur of unsafe objects
    smart_blur_enabled = Column(Boolean, nullable=True, default=False)
    target_clips = Column(Integer, nullable=True, default=10)
    # The API fills this from settings.DEFAULT_AUTO_UPLOAD, so the operator's
    # .env decides. The column itself still defaults to False: a Job row built
    # directly (a script, a test) is local until somebody asks for publishing.
    auto_upload = Column(Boolean, nullable=True, default=False)
    # "public" | "unlisted" | "private"; NULL falls back to AUTO_UPLOAD_PRIVACY
    upload_privacy = Column(String, nullable=True, default=None)
    llm_translate = Column(Boolean, nullable=True, default=False)
    hook_style_list = Column(JSON, nullable=True)
    is_zero_view_diagnostic = Column(Boolean, nullable=True, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationship to clips
    clips = relationship("Clip", back_populates="job", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<Job(id={self.id}, url='{self.youtube_url[:50]}...', status='{self.status}')>"

class JobProgress(Base):
    """Represents the progress of a job."""
    __tablename__ = "job_progress"

    job_id = Column(Integer, ForeignKey("jobs.id"), primary_key=True)
    current_stage = Column(String, nullable=True)
    progress_percent = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    job = relationship("Job")

    def __repr__(self):
        return f"<JobProgress(job_id={self.job_id}, stage='{self.current_stage}', progress={self.progress_percent}%)>"

class Clip(Base):
    """Represents a generated clip from a job."""
    __tablename__ = "clips"

    # One row per clip slot, enforced by the database instead of by whoever got
    # there first. Without it a second worker on the same job re-creates the row
    # (job 70: 214/232, 215/233, 216/234 - the same shorts published twice) or
    # overwrites one (job 93: no index 0 at all, while clip_0/final_video.mp4
    # sits on disk unpublished). An existing database that already holds
    # duplicates cannot accept this index, so database.migrations creates it
    # conditionally and only ever reports the violations.
    __table_args__ = (
        Index(CLIP_UNIQUE_INDEX, "job_id", "index", unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    index = Column(Integer, nullable=False)  # Order within the job
    start = Column(Float, nullable=False)  # Start time in seconds
    end = Column(Float, nullable=False)  # End time in seconds
    title = Column(String, nullable=True)
    hook = Column(String, nullable=True)
    score = Column(Float, nullable=True)  # Virality score from AI selection (0-100)
    hook_score = Column(Float, nullable=True)
    coherence_score = Column(Float, nullable=True)
    connection_score = Column(Float, nullable=True)
    trend_score = Column(Float, nullable=True)
    # Individual signals behind `score` (llm / audio / keyword / density /
    # structural). The studio tooltip used to read per-signal columns that no
    # code ever wrote, so every clip displayed the same invented percentages.
    score_breakdown = Column(JSON, nullable=True)

    # Style and layout configurations
    caption_style = Column(String, nullable=True, default="mrbeast")
    layout_mode = Column(String, nullable=True, default="single_speaker")
    virality_explanation = Column(Text, nullable=True)
    # Precision trim of rendered clip
    trim_start = Column(Float, nullable=True)
    trim_end = Column(Float, nullable=True)
    # Translated transcript for this clip (English)
    translated_transcript = Column(Text, nullable=True)

    # File paths (relative to STORAGE_ROOT)
    file_path = Column(String, nullable=True)  # Path to final_video.mp4
    thumbnail_path = Column(String, nullable=True)  # Path to thumbnail.jpg

    # Metadata
    description = Column(Text, nullable=True)  # AI-generated description
    hashtags = Column(JSON, nullable=True)  # List of hashtags
    platform_metadata = Column(JSON, nullable=True)  # Platform-specific copy (TikTok, IG, YT)

    # Upload status
    uploaded = Column(Boolean, default=False)
    youtube_video_id = Column(String, nullable=True)
    # Records the TikTok publish. Without it the per-clip upload left no trace
    # and the on-completion sweep republished the same clip.
    tiktok_video_id = Column(String, nullable=True)
    upload_error = Column(Text, nullable=True)
    upload_retry_count = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)  # When it actually went out
    # The publish queue. When this clip may go out; NULL means it is not queued.
    # The worker claims a clip by moving this value, which is what stops two
    # workers uploading the same video, and it is why the spacing survives a
    # restart: the clock is this column, not a dict in a process.
    publish_due_at = Column(DateTime, nullable=True)
    # The release clock, and the only reason the schedule survives the PC being off.
    # YouTube is handed this instant as status.publishAt when the clip is uploaded
    # (private until then), so a clip that has already been uploaded publishes itself
    # whether or not this machine is awake. The queue reads the latest value here to
    # place the next one, exactly as it reads published_at for the upload clock.
    scheduled_publish_at = Column(DateTime, nullable=True)
    # The intent record. Written and committed before the file is handed to YouTube
    # and cleared as soon as the outcome is known, so a process that dies in the
    # window between the API returning a video id and the row being written leaves a
    # row that says "an upload started here at this instant and nobody recorded what
    # became of it" - which the queue stops on - instead of one that looks untouched
    # and gets a second copy of the same clip uploaded.
    publish_attempt_at = Column(DateTime, nullable=True)

    # The experiment record. A zero-view diagnostic job gives every clip of the
    # job a different hook style, but a style only means something next to the
    # number it earned, so both halves live here: which arm this clip shipped
    # with, and what the platform reported back for it.
    hook_style = Column(String, nullable=True)
    view_count = Column(Integer, nullable=True)
    like_count = Column(Integer, nullable=True)
    comment_count = Column(Integer, nullable=True)
    stats_synced_at = Column(DateTime, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationship to job
    job = relationship("Job", back_populates="clips")

    def __repr__(self):
        return f"<Clip(id={self.id}, job_id={self.job_id}, index={self.index}, uploaded={self.uploaded})>"