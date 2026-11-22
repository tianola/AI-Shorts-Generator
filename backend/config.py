"""
Configuration management using Pydantic BaseSettings.
All configuration is driven by environment variables.
"""
import os
from pathlib import Path
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # ====================
    # LLM PROVIDER CONFIGURATION
    # ====================
    AI_PROVIDER: str = Field(
        default="omniroute",
        description="LLM backend: 'omniroute' or 'ollama'"
    )
    OLLAMA_URL: str = Field(
        default="http://host.docker.internal:11434",
        description="Ollama API base URL"
    )
    OLLAMA_MODEL: str = Field(
        default="qwen2.5-coder:14b",
        description="AI model for clip selection"
    )
    METADATA_OLLAMA_MODEL: str = Field(
        default="qwen2.5-coder:14b",
        description="AI model for metadata generation"
    )
    OMNIROUTE_BASE_URL: str = Field(
        default="http://localhost:20128/v1",
        description="OpenAI-compatible OmniRoute base URL"
    )
    OMNIROUTE_API_KEY: Optional[str] = Field(
        default=None,
        description="OmniRoute API key"
    )
    # free/shorts: the free-only priority combo built for this job on the local
    # OmniRoute (2026-11-16), from a benchmark of 38 models on job 111's real
    # prompts. Order: Claude Opus/Sonnet via Antigravity (best picks seen; weekly
    # quota), agnes-3.0-flash, ernie-5.5-flash, nemotron-3-ultra, gemma-4, the
    # pinned gemini ids, laguna, codestral. Pinned single models went dead together
    # (429 until 11-21) and the selector shipped its grid; a priority combo falls
    # through server-side. Not an `auto/*` combo: auto/best-free timed out on the
    # same prompt, and an `auto/*` resolution once answered with a thinking block.
    OMNIROUTE_MODEL: str = Field(
        default="free/shorts",
        description="OmniRoute model id for clip selection and metadata generation"
    )
    # Ordered, comma-separated OmniRoute ids tried after OMNIROUTE_MODEL when a leg
    # times out twice or answers with no JSON at all. Free models go dead without
    # warning - quota spent, upstream down - and with nowhere to go the router fell
    # through to an Ollama that is not running, so the selector shipped its grid.
    # free/best: the general free-only chain (34 members); on job 111 it returned
    # 5 model-picked clips in 70 s. Tried only when free/shorts fails as a whole.
    OMNIROUTE_MODEL_FALLBACKS: str = Field(
        default="free/best",
        description="Comma-separated OmniRoute model ids to try after OMNIROUTE_MODEL"
    )
    # Claude is used for clip selection only, and only when a key is set; with no
    # key the selector stays on the OmniRoute chain above.
    ANTHROPIC_API_KEY: Optional[str] = Field(
        default=None,
        description="Anthropic API key. When set, clip selection can use Claude."
    )
    CLAUDE_SELECTION_MODEL: str = Field(
        default="claude-opus-5-5",
        description="Claude model id for clip selection"
    )
    TRANSCRIPTION_PROVIDER: str = Field(
        default="groq",
        description="Transcription backend: 'groq' or 'local'"
    )
    GROQ_BASE_URL: str = Field(
        default="https://api.groq.com/openai/v1",
        description="Groq OpenAI-compatible API base URL"
    )
    GROQ_API_KEY: Optional[str] = Field(
        default=None,
        description="Groq API key for fast transcription"
    )
    GROQ_MODEL: str = Field(
        default="whisper-large-v3-turbo",
        description="Groq transcription model"
    )
    # faster-whisper transcription model size (local fallback)
    WHISPER_MODEL_SIZE: str = Field(
        default="tiny",
        description="Transcription model size (faster-whisper): tiny, base, small, medium, large"
    )
    WHISPER_PT_MODELS_DIR: str = Field(
        default=r"E:\01-AI-PROJECTS\whisper-models",
        description="Directory holding OpenAI-whisper .pt checkpoints for the "
                    "'local_pt_<name>' transcription backend"
    )
    # Misheard names in captions ("THE TRADERS ATE" for the Traitorous Eight on
    # job 112) are corrected per clip by a model reading the words in context;
    # whisper small/medium made the same mistakes. On job 112's clip words only
    # free/best found the name: agnes-3.0-flash (free/shorts' live member) and
    # gemma answered in prose, codestral/laguna echoed every word.
    CAPTION_FIX_ENABLED: bool = Field(
        default=True,
        description="Correct misheard names in each clip's words before captioning"
    )
    CAPTION_FIX_MODEL: str = Field(
        default="free/best",
        description="OmniRoute model or combo for caption name correction"
    )

    # ====================
    # FALLBACK LLM PROVIDERS
    # ====================
    TOGETHER_API_KEY: Optional[str] = Field(
        default=None,
        description="Together AI API key (free tier: $25/mo, Llama 3.1 405B, Qwen)"
    )
    TOGETHER_BASE_URL: str = Field(
        default="https://api.together.xyz/v1",
        description="Together AI OpenAI-compatible base URL"
    )
    TOGETHER_MODEL: str = Field(
        default="meta-llama/Llama-3.1-405B-Instruct-Turbo",
        description="Together AI model for clip selection fallback"
    )
    GOOGLE_AI_STUDIO_KEY: Optional[str] = Field(
        default=None,
        description="Google AI Studio (Gemini) API key"
    )
    GOOGLE_AI_STUDIO_MODEL: str = Field(
        default="gemini-flash-latest",
        description="Google AI Studio model for vision tasks"
    )
    HF_ROUTER_BASE_URL: str = Field(
        default="https://router.huggingface.co/v1",
        description="HuggingFace Inference Router base URL (OpenAI-compatible)"
    )
    HF_ROUTER_MODEL: str = Field(
        default="Qwen/Qwen3-VL-4B-Instruct:featherless-ai",
        description="HuggingFace Router model for VLM tasks (Qwen3-VL via Featherless AI)"
    )

    # ====================
    # SPEAKER DIARIZATION
    # ====================
    HF_KEY: Optional[str] = Field(
        default=None,
        description="HuggingFace access token for gated model downloads "
                    "(pyannote speaker diarization). The model pages must also "
                    "be accepted on huggingface.co with this account."
    )
    DIARIZATION_ENABLED: bool = Field(
        default=True,
        description="Run job-level speaker diarization after transcription. "
                    "Soft-fails to single-speaker behaviour when the token or "
                    "models are unavailable."
    )
    DIARIZATION_MODEL: str = Field(
        default="pyannote/speaker-diarization-3.1",
        description="pyannote pipeline checkpoint for speaker diarization"
    )
    DIARIZATION_CLIP_MARGIN_SECONDS: float = Field(
        default=3.0,
        ge=0.0,
        le=60.0,
        description="Seconds of padding added before/after each selected clip "
                    "window when diarizing. Diarization runs only over the clip "
                    "windows (not the whole source) to keep long videos fast; the "
                    "margin captures speakers who start talking just before a cut."
    )

    # ====================
    # CLIP SELECTION
    # ====================
    NUM_CLIPS: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Clips to fall back to when the model gives nothing usable. Normal runs "
                    "are automatic: the selector keeps every moment that passes its judge, "
                    "up to MAX_AUTO_CLIPS."
    )
    MAX_AUTO_CLIPS: int = Field(
        default=60,
        ge=1,
        le=200,
        description="Most clips one video can yield when the selector chooses the number itself."
    )
    MIN_CLIP_SECONDS: int = Field(
        default=30,
        ge=5,
        le=120,
        description="Minimum clip duration in seconds"
    )
    MAX_CLIP_SECONDS: int = Field(
        default=60,
        ge=10,
        le=180,
        description="Maximum clip duration in seconds. A ceiling, not a target: the "
                    "window ends where the last sentence ends, and at 30 s half of "
                    "job 113's clips were cut mid-sentence to fit."
    )

    # ====================
    # CAPTIONS
    # ====================
    CAPTION_GROUP_SIZE: int = Field(
        default=3,
        ge=1,
        le=8,
        description="Max words per caption burst. Three large words read at a glance; "
                    "five had to be set small to fit the frame."
    )
    CAPTION_EMOJI_MODE: str = Field(
        default="sticker",
        description="How caption emoji are drawn. 'sticker' lays a colour PNG over the video "
                    "(libass cannot draw colour emoji); 'text' writes the emoji into the "
                    "subtitle text (shows as a box or flat shape); 'off' draws none."
    )
    EMOJI_FONT_PATH: Optional[str] = Field(
        default=None,
        description="Colour emoji font used to make the stickers. Empty: look for Segoe UI Emoji "
                    "(Windows) or Noto Color Emoji (Linux)."
    )

    # ====================
    # EDITING EFFECTS
    # ====================
    EFFECTS_ENABLED: bool = Field(
        default=True,
        description="Apply the visual effects planned in each clip's edit timeline (zoom punch, "
                    "camera shake, flash, red tint). Off renders clips plain."
    )
    EFFECTS_DISABLED_KINDS: str = Field(
        default="",
        description="Comma list of effect kinds to leave out even when planned, e.g. 'flash,tint_red'."
    )

    # ====================
    # VERTICAL CONVERSION
    # ====================
    VERTICAL_MODE: str = Field(
        default="face_tracked",
        description="Vertical conversion mode: 'static' or 'face_tracked'"
    )
    @field_validator('VERTICAL_MODE')
    def validate_vertical_mode(cls, v):
        if v not in ['static', 'face_tracked']:
            raise ValueError("VERTICAL_MODE must be 'static' or 'face_tracked'")
        return v

    @field_validator('AI_PROVIDER')
    def validate_ai_provider(cls, v):
        if v not in ['omniroute', 'ollama']:
            raise ValueError("AI_PROVIDER must be 'omniroute' or 'ollama'")
        return v.lower()

    @field_validator('TRANSCRIPTION_PROVIDER')
    def validate_transcription_provider(cls, v):
        if v not in ['groq', 'local']:
            raise ValueError("TRANSCRIPTION_PROVIDER must be 'groq' or 'local'")
        return v.lower()

    # ====================
    # FFMPEG SETTINGS
    # ====================
    FFMPEG_PRESET: str = Field(
        default="veryfast",
        description="FFmpeg encoding preset"
    )
    FFMPEG_CRF: int = Field(
        default=18,
        ge=0,
        le=51,
        description="FFmpeg CRF value (visually lossless quality)"
    )
    FFMPEG_THREADS: int = Field(
        default=0,
        ge=0,
        le=64,
        description="x264 worker threads for the final render (-threads N). 0 lets ffmpeg use all logical cores; lower it only if encode RAM is the limit, since the cap costs ~2x wall time on a 16-thread machine."
    )
    FFMPEG_SCALE_FILTER: str = Field(
        default="scale=trunc(iw/2)*2:trunc(ih/2)*2",
        description="FFmpeg vf scale filter applied before encoding to lower resolution."
    )
    SINGLE_PASS_RENDER: bool = Field(
        default=True,
        description="Render each clip in one unified ffmpeg pass (vertical + captions + title + SFX) instead of 5-6 sequential re-encodes"
    )

    # ====================
    # SCENE / ATTENTION UNDERSTANDING
    # ====================
    TEMPORAL_MAX_SAMPLES: int = Field(
        default=240,
        ge=20,
        le=20000,
        description="Cap on YOLO frames sampled by temporal localization. Short "
                    "sources keep the fine 2s interval; long sources widen it to "
                    "hold this many samples, since the per-frame YOLO sweep (not "
                    "scene detection) dominates cost on hour-long inputs. Raise "
                    "for finer event coverage, lower for faster long-source runs."
    )
    SCENE_ATTENTION_ENABLED: bool = Field(
        default=True,
        description="Per-clip scene/attention understanding (which object the "
                    "speech is about, what matters in each scene) that can "
                    "steer the crop focus. The local tier always runs; the "
                    "text-LLM tier soft-fails to it."
    )
    SCENE_ATTENTION_VLM: bool = Field(
        default=False,
        description="Send sampled keyframes to a vision model through OmniRoute "
                    "for scene understanding. Off by default: it uploads video "
                    "frames to the gateway."
    )
    SCENE_ATTENTION_VLM_MODEL: str = Field(
        default="",
        description="OmniRoute model id for the VLM tier (any image-capable "
                    "model). Empty disables the VLM tier even when "
                    "SCENE_ATTENTION_VLM is on."
    )
    LOCAL_VLM_ENABLED: bool = Field(
        default=False,
        description="Add a LOCAL Ollama vision tier to scene understanding and "
                    "prefer it over the remote Gemini/OmniRoute tiers. Runs on "
                    "this machine (CPU) with no account or API cost; off until a "
                    "model is pulled and it is proven on real frames."
    )
    LOCAL_VLM_URL: str = Field(
        default="http://localhost:11434",
        description="Ollama base URL for the local vision tier (native; the text "
                    "router's OLLAMA_URL is the Docker host)."
    )
    LOCAL_VLM_MODEL: str = Field(
        default="moondream",
        description="Ollama vision model id. moondream is the default because it "
                    "runs on CPU here; qwen2.5vl:3b follows instructions better "
                    "but crashes the Ollama runner on this CUDA-less box. A name "
                    "it cannot pull is a soft-fail to the next tier."
    )

    # ====================
    # BRANDING
    # ====================
    WATERMARK_ENABLED: bool = Field(
        default=False,
        description="Draw the @handle pill + source-credit badge on every clip. "
                    "Off by default: a burned-in watermark is a Shorts distribution "
                    "penalty and a 'reused content' tell."
    )
    WATERMARK_HANDLE: str = Field(
        default="@thomastheo",
        description="Watermark handle shown on every clip (e.g. @thomastheo)"
    )
    MANDATORY_HASHTAGS: str = Field(
        default="#thomastheo,#mrclip",
        description="Comma-separated hashtags always appended to every clip's metadata"
    )

    # ====================
    # UPLOAD SETTINGS
    # ====================
    MAX_UPLOAD_RETRIES: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Maximum number of upload retries"
    )
    AUTO_UPLOAD_PRIVACY: str = Field(
        default="unlisted",
        description="Visibility for uploads that go out the moment they are made: "
                    "the manual button and the batch sweep. The publish queue ignores "
                    "it, because YouTube only accepts a scheduled release on a private "
                    "video - those clips sit private until their publishAt."
    )
    DEFAULT_AUTO_UPLOAD: bool = Field(
        default=True,
        description="Whether a job created without an explicit auto_upload flag "
                    "queues its finished clips for publishing. The queue uploads "
                    "them UPLOAD_MIN_GAP_SECONDS apart and hands YouTube a release "
                    "time PUBLISH_SCHEDULE_GAP_SECONDS apart, so this only decides "
                    "whether the queue is used at all. A request can still say "
                    "auto_upload=false to keep a job local."
    )
    UPLOAD_MIN_GAP_SECONDS: int = Field(
        default=3600,
        ge=0,
        le=86400,
        description="Minimum spacing between uploads to the same platform. This is "
                    "the upload clock: it keeps this machine from handing YouTube a "
                    "burst of videos, which is all the local queue is left to decide "
                    "now that the release cadence lives in publishAt. The publish "
                    "queue schedules the next upload this far after the last one that "
                    "actually went out, so the wait is a row in the database and "
                    "survives a worker restart. 0 disables pacing."
    )
    PUBLISH_SCHEDULE_GAP_SECONDS: int = Field(
        default=43200,
        ge=60,
        le=86400,
        description="Spacing between the release times YouTube is given for "
                    "consecutive clips. This is the cadence the channel's audience "
                    "sees, and the platform enforces it: each clip is uploaded "
                    "private with status.publishAt this far after the previous "
                    "release, so the schedule outlives the machine that made it."
    )
    PUBLISH_SCHEDULE_LEAD_SECONDS: int = Field(
        default=900,
        ge=60,
        le=86400,
        description="Smallest gap between an upload and its release. A publishAt "
                    "already in the past makes YouTube publish the instant the "
                    "upload lands, so this is the window to inspect a clip before "
                    "the channel's audience can."
    )
    UPLOAD_GAP_JITTER_SECONDS: int = Field(
        default=600,
        ge=0,
        le=86400,
        description="Random extra delay added on top of UPLOAD_MIN_GAP_SECONDS, so "
                    "the cadence is not exactly repeatable."
    )
    UPLOAD_DAILY_BUDGET: int = Field(
        default=8,
        ge=0,
        le=100,
        description="How many uploads to YouTube the queue will attempt inside a "
                    "rolling 24 hours. This is the channel's own ceiling, not the "
                    "project quota, and YouTube never publishes the number: measured "
                    "here it is about ten, so the queue stops short of it instead of "
                    "learning it from an uploadLimitExceeded. It is spent at upload, "
                    "not at release, so a clip parked with publishAt costs a slot the "
                    "moment its bytes land. 0 disables the guard."
    )
    PUBLISH_UPLOAD_LEAD_SECONDS: int = Field(
        default=21600,
        ge=60,
        le=604800,
        description="How far ahead of its release time a clip may be uploaded. "
                    "Uploads are paced by the release clock, not by how fast the "
                    "render finished: a clip becomes due this long before its slot, "
                    "so the machine never spends upload budget on videos the channel "
                    "has not reached yet, and an outage of this length still leaves "
                    "the next release uploaded and scheduled."
    )
    PUBLISH_RELEASE_WINDOW_UTC: str = Field(
        default="6-22",
        description="Hours of the day (UTC) a release slot may land in, as "
                    "'start-end'. YouTube does not punish an odd hour, a channel that "
                    "publishes at 04:17 every day simply reads as a machine; slots "
                    "outside the window move to its next open hour. Empty disables it."
    )
    PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS: int = Field(
        default=86400,
        ge=0,
        le=2592000,
        description="Smallest spacing between two releases built from the same source "
                    "video. Fifteen clips of one stream published over two days is the "
                    "most recognisable repost-farm shape there is, and it concentrates "
                    "a copyright claim on one day. Jobs from different sources still "
                    "interleave on the shared cadence."
    )
    PUBLISH_RETRY_MINUTES: int = Field(
        default=30,
        ge=1,
        description="How long a clip waits before an ordinary retry: a dead token, a "
                    "network fault, a missing file. The clip stays in the queue."
    )
    SHORTS_CTA: str = Field(
        default="",
        description="Closing line of a Short's description. Empty means the clip's own "
                    "description ends it, which is what a person writes. The line this "
                    "replaced was hardcoded on every video since the beginning "
                    "('Like, Share & Subscribe for more daily viral clips!') - the same "
                    "sentence on hundreds of uploads is a spam signature, and it lied "
                    "about the cadence now that the channel releases twice a day."
    )
    PUBLISH_LIMIT_RETRY_HOURS: int = Field(
        default=6,
        ge=1,
        description="How long a clip waits when YouTube answers uploadLimitExceeded. "
                    "That is the channel's upload budget, not a fault in the clip, so "
                    "it is not charged as a retry attempt."
    )

    @field_validator('AUTO_UPLOAD_PRIVACY')
    def validate_upload_privacy(cls, v):
        allowed = ['public', 'unlisted', 'private']
        if v.lower() not in allowed:
            raise ValueError(f"AUTO_UPLOAD_PRIVACY must be one of {allowed}")
        return v.lower()

    # ====================
    # TIKTOK CONTENT POSTING API (v2)
    # ====================
    # The app credentials come from TikTok for Developers; the browser session_id
    # in storage/secrets/tiktok_token.json cannot post anything.
    TIKTOK_CLIENT_KEY: str = Field(default="", description="TikTok app client key (env only, or storage/secrets/tiktok_client.json)")
    TIKTOK_CLIENT_SECRET: str = Field(default="", description="TikTok app client secret (env only, or storage/secrets/tiktok_client.json)")
    TIKTOK_REDIRECT_URI: str = Field(
        default="http://localhost:5001/api/tiktok/oauth2callback",
        description="Must match an allowed redirect URI on the TikTok app."
    )
    TIKTOK_SCOPES: str = Field(
        default="user.info.basic,video.upload",
        description="Scopes the TikTok app has actually been granted, comma separated. "
                    "video.upload is what a Content Posting app gets by default "
                    "('Upload to TikTok'); video.publish only appears once the app is "
                    "approved for Direct Post, and asking for a scope the app does not "
                    "have makes the consent screen refuse the connection."
    )
    TIKTOK_POST_MODE: str = Field(
        default="UPLOAD",
        description="UPLOAD drops the clip in the account's inbox for a manual tap "
                    "(the safe default, and the only mode a video.upload app can use). "
                    "DRAFT saves to the account's drafts and DIRECT_PUBLISH posts "
                    "outright — both need the app to hold video.publish."
    )

    @field_validator('TIKTOK_POST_MODE')
    def validate_tiktok_post_mode(cls, v):
        allowed = ['DRAFT', 'DIRECT_PUBLISH', 'UPLOAD']
        if v.upper() not in allowed:
            raise ValueError(f"TIKTOK_POST_MODE must be one of {allowed}")
        return v.upper()

    # ====================
    # WORKER
    # ====================
    JOB_LEASE_MINUTES: int = Field(
        default=90,
        ge=1,
        le=1440,
        description="Minutes after which a job still marked 'processing' with no "
                    "heartbeat is assumed abandoned and returned to the queue."
    )
    WORKER_STALL_TIMEOUT_MINUTES: int = Field(
        default=30,
        ge=5,
        le=720,
        description="Minutes a job may run with no progress signal AND no process "
                    "CPU (see WORKER_STALL_MIN_CPU_SECONDS) before the watchdog "
                    "declares the main thread wedged: it fails that job and exits so "
                    "the NSSM service restarts a clean worker. A long but busy stage "
                    "(e.g. diarization on an hour of audio) is spared because it "
                    "keeps burning CPU."
    )
    WORKER_STALL_MIN_CPU_SECONDS: int = Field(
        default=120,
        ge=1,
        le=3600,
        description="Process CPU seconds that must have accrued since the last "
                    "progress tick for the watchdog to consider the worker alive. "
                    "Below this over the full stall window means the main thread is "
                    "blocked (a real wedge), not crunching a slow compute stage."
    )
    WORKER_MAX_JOBS: int = Field(
        default=2,
        ge=1,
        le=16,
        description="Jobs one worker runs at a time (arq concurrency). Rendering is "
                    "ffmpeg-bound and an uncapped encode already uses every core, so "
                    "raise this only together with a FFMPEG_THREADS cap."
    )

    # ====================
    # STOCK FOOTAGE (Pexels)
    # ====================
    PEXELS_API_KEY: Optional[str] = Field(
        default=None,
        description="Pexels API key for stock B-Roll video downloads"
    )
    PIXABAY_API_KEY: Optional[str] = Field(
        default=None,
        description="Pixabay API key: a second stock video source for Narrate Studio"
    )

    # ====================
    # TEXT-TO-SPEECH (ElevenLabs)
    # ====================
    TTS_ENABLED: bool = Field(
        default=False,
        description="Enable ElevenLabs TTS integration"
    )
    ELEVENLABS_API_KEY: Optional[str] = Field(
        default=None,
        description="ElevenLabs API key (set via environment variable)"
    )
    ELEVENLABS_VOICE_ID: Optional[str] = Field(
        default="CwhRBWXzGAHq8TQ4Fs17",
        description="Default ElevenLabs voice id to use for TTS"
    )

    # ====================
    # API SERVER
    # ====================
    API_HOST: str = Field(
        default="0.0.0.0",
        description="Host interface for the FastAPI server"
    )
    API_PORT: int = Field(
        default=5001,
        ge=1,
        le=65535,
        description="Port for the FastAPI server"
    )
    CORS_ORIGINS: str = Field(
        default="http://localhost:5001,http://127.0.0.1:5001",
        description="Comma-separated browser origins allowed to call the API. "
                    "Use '*' to open it up (credentials are then disabled automatically)."
    )

    @property
    def cors_origin_list(self) -> list:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    # ====================
    # LOGGING
    # ====================
    LOG_LEVEL: str = Field(
        default="INFO",
        description="Level written to the rotating storage/logs/app.log sink."
    )
    LOG_CONSOLE_LEVEL: str = Field(
        default="WARNING",
        description="Level written to stderr. In windowless daemon mode stderr is "
                    "redirected to storage/logs/<name>_error.log, which is rotated "
                    "by size but still holds every warning the run produced."
    )

    # ====================
    # STORAGE
    # ====================
    STORAGE_ROOT: str = Field(
        default="/app/storage",
        description="Root path for all storage (videos, jobs, etc.). "
                    "Left unset outside Docker it resolves to <repo>/storage."
    )

    @field_validator('STORAGE_ROOT', mode='before')
    @classmethod
    def resolve_storage_root(cls, v):
        # Honour an explicit operator value; only substitute the repo-local
        # storage directory for the packaged "/app" default on a dev machine.
        # Silently rewriting every configured path made an external volume
        # impossible to point at.
        backend_dir = Path(__file__).resolve().parent          # .../backend
        project_root = backend_dir.parent                       # repo root
        configured = str(v).strip() if v else ""

        in_docker = os.path.exists('/.dockerenv')
        is_packaged_default = configured in ("", "/app/storage", "/app")
        if in_docker or not is_packaged_default:
            resolved = configured or "/app/storage"
        else:
            resolved = os.path.join(str(project_root), "storage")

        resolved = os.path.abspath(os.path.expanduser(resolved))
        os.makedirs(resolved, exist_ok=True)
        return resolved

    MIN_FREE_DISK_GB: float = Field(
        default=10.0,
        ge=0,
        description="Leave jobs queued while the storage volume has less than this "
                    "much free. A source video can be tens of gigabytes, and a "
                    "download that fills the drive also corrupts the SQLite "
                    "database that sits on it."
    )
    DELETE_AUDIO_AFTER_TRANSCRIPTION: bool = Field(
        default=True,
        description="audio.wav is only read by the transcriber; words.json and "
                    "segments.json carry the result onwards. A 3-hour source "
                    "leaves over 1 GB of wav behind for no reason."
    )

    # ====================
    # PATHS (computed)
    # ====================
    @property
    def JOBS_DIR(self) -> str:
        return os.path.join(self.STORAGE_ROOT, "jobs")

    @property
    def LOGS_DIR(self) -> str:
        return os.path.join(self.STORAGE_ROOT, "logs")

    # ====================
    # YOUTUBE DOWNLOAD (cookies + JS challenge)
    # ====================
    # Path to a Netscape-format cookies.txt for YouTube (exported from the
    # browser). Stored OUTSIDE the repo so credentials never hit git.
    YT_COOKIES_FILE: Optional[str] = Field(
        default=None,
        description="Absolute path to yt-dlp cookies.txt (Netscape format) for YouTube downloads"
    )
    # Newer YouTube builds require solving a JS "n" challenge via deno/node.
    # yt-dlp downloads the solver script from GitHub (EJS project) on demand.
    YT_REMOTE_COMPONENTS: str = Field(
        default="ejs:github",
        description="yt-dlp remote components for JS challenge solving (ejs:github or ejs:npm)"
    )
    YT_NODE_PATH: str = Field(
        default="",
        description="Absolute path to a node binary for yt-dlp's EJS n-solver. "
                    "Empty = auto-detect (PATH, then the standard Windows install). "
                    "Only needed because yt-dlp enables deno by default and this "
                    "box has node, not deno."
    )

    FONTS_DIR: str = Field(
        default_factory=lambda: (
            "C:/Windows/Fonts" if os.name == "nt"
            else "/usr/share/fonts/truetype/montserrat"
        ),
        description="Directory searched last for caption/title fonts"
    )

    @property
    def fonts_search_paths(self) -> list:
        """Font directories in priority order: bundled assets first, then OS."""
        return [
            os.path.join(self.STORAGE_ROOT, "assets", "fonts"),
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts"),
            self.FONTS_DIR,
        ]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra='ignore'
    )

# Global settings instance
settings = Settings()