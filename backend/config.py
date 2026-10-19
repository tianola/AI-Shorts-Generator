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
    OMNIROUTE_MODEL: str = Field(
        default="auto/pro-reasoning",
        description="OmniRoute model id for clip selection and metadata generation"
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

    # ====================
    # CLIP SELECTION
    # ====================
    NUM_CLIPS: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Number of clips to generate per video (short-form viral moments)"
    )
    MIN_CLIP_SECONDS: int = Field(
        default=20,
        ge=5,
        le=120,
        description="Minimum clip duration in seconds"
    )
    MAX_CLIP_SECONDS: int = Field(
        default=75,
        ge=10,
        le=180,
        description="Maximum clip duration in seconds"
    )

    # ====================
    # CAPTIONS
    # ====================
    CAPTION_GROUP_SIZE: int = Field(
        default=5,
        ge=1,
        le=8,
        description="Number of words per caption burst (4-5 words easy-to-read TikTok-style chunks)"
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
        default="qwen2.5-vl:3b",
        description="Ollama vision model id (image-capable). Small enough to run "
                    "on CPU here; a name it cannot pull is a soft-fail to the "
                    "next tier."
    )

    # ====================
    # BRANDING
    # ====================
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
        description="Visibility for automatic uploads: 'public', 'unlisted' or 'private'"
    )
    UPLOAD_MIN_GAP_SECONDS: int = Field(
        default=3600,
        ge=0,
        le=86400,
        description="Minimum spacing between publishes on the same platform. The "
                    "publish queue schedules the next clip this far after the last one "
                    "that actually went out, so the wait is a row in the database and "
                    "survives a worker restart. 0 disables pacing."
    )
    UPLOAD_GAP_JITTER_SECONDS: int = Field(
        default=600,
        ge=0,
        le=86400,
        description="Random extra delay added on top of UPLOAD_MIN_GAP_SECONDS, so "
                    "the cadence is not exactly repeatable."
    )
    PUBLISH_RETRY_MINUTES: int = Field(
        default=30,
        ge=1,
        description="How long a clip waits before an ordinary retry: a dead token, a "
                    "network fault, a missing file. The clip stays in the queue."
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