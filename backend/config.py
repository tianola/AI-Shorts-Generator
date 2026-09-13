"""
Configuration management using Pydantic BaseSettings.
All configuration is driven by environment variables.
"""
import os
from pathlib import Path
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

    # ====================
    # CLIP SELECTION
    # ====================
    NUM_CLIPS: int = Field(
        default=10,
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
        default=3,
        ge=1,
        le=6,
        description="Number of words per caption burst (short, easy-to-read TikTok-style chunks)"
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
        default=20,
        ge=0,
        le=51,
        description="FFmpeg CRF value (quality)"
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
        default=5000,
        ge=1,
        le=65535,
        description="Port for the FastAPI server"
    )

    # ====================
    # STORAGE
    # ====================
    STORAGE_ROOT: str = Field(
        default="/app/storage",
        description="Root path for all storage (videos, jobs, etc.)"
    )

    @field_validator('STORAGE_ROOT', mode='before')
    @classmethod
    def resolve_storage_root(cls, v):
            # Always return an absolute path based on the repo root (parent of backend/),
            # never depending on CWD. In Docker keep any explicit /app path.
            backend_dir = Path(__file__).resolve().parent          # .../backend
            project_root = backend_dir.parent                       # repo root
            if os.path.exists('/.dockerenv') and (not v or str(v).startswith('/app')):
                os.makedirs(v, exist_ok=True)
                return v
            local_storage = os.path.join(str(project_root), "storage")
            os.makedirs(local_storage, exist_ok=True)
            return local_storage

    # ====================
    # PATHS (computed)
    # ====================
    @property
    def JOBS_DIR(self) -> str:
        return os.path.join(self.STORAGE_ROOT, "jobs")

    @property
    def FONTS_DIR(self) -> str:
        return "/usr/share/fonts/truetype/montserrat"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra='ignore'
    )

# Global settings instance
settings = Settings()