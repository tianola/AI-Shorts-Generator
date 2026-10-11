"""
Database session management.
SQLAlchemy session factory with proper configuration and safe column auto-migrations.
"""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings
from database.base import Base
import os


def get_engine():
    db_url = os.getenv("TEST_DATABASE_URL")
    if not db_url:
        db_path = os.path.abspath(os.path.join(settings.STORAGE_ROOT, "app.db"))
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        db_url = f"sqlite:///{db_path}"
        return create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            echo=False
        )
    else:
        # Use StaticPool for in-memory tests to share connection
        return create_engine(
            db_url,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=False
        )


engine = get_engine()

# Session factory
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)


def init_database():
    """Initialize database tables and apply lightweight column migrations."""
    Base.metadata.create_all(bind=engine)

    # Safe auto-migration for SQLite
    with engine.connect() as conn:
        # Inspect columns for 'clips'
        try:
            res = conn.execute(text("PRAGMA table_info(clips)"))
            existing_cols = [row[1] for row in res.fetchall()]
            if "caption_style" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN caption_style TEXT DEFAULT 'hormozi'"))
            if "layout_mode" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN layout_mode TEXT DEFAULT 'single_speaker'"))
            if "virality_explanation" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN virality_explanation TEXT"))
            conn.commit()
        except Exception:
            pass

        # Inspect columns for 'jobs'
        try:
            res = conn.execute(text("PRAGMA table_info(jobs)"))
            existing_cols = [row[1] for row in res.fetchall()]
            if "default_caption_style" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN default_caption_style TEXT DEFAULT 'hormozi'"))
            if "default_layout_mode" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN default_layout_mode TEXT DEFAULT 'single_speaker'"))
            if "default_karaoke" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN default_karaoke BOOLEAN DEFAULT 1"))
            if "default_motion_title" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN default_motion_title BOOLEAN DEFAULT 1"))
            if "transcription_task" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN transcription_task TEXT DEFAULT 'transcribe'"))
            if "source_language" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN source_language TEXT"))
            if "keep_native_transcript" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN keep_native_transcript BOOLEAN DEFAULT 0"))
            if "smart_blur_enabled" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN smart_blur_enabled BOOLEAN DEFAULT 0"))
            if "target_clips" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN target_clips INTEGER DEFAULT 10"))
            if "auto_upload" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN auto_upload BOOLEAN DEFAULT 0"))
            if "llm_translate" not in existing_cols:
                conn.execute(text("ALTER TABLE jobs ADD COLUMN llm_translate BOOLEAN DEFAULT 0"))
            conn.commit()
        except Exception:
            pass

        # Inspect columns for 'clips' (trim + translated transcript + API integration)
        try:
            res = conn.execute(text("PRAGMA table_info(clips)"))
            existing_cols = [row[1] for row in res.fetchall()]
            if "trim_start" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN trim_start FLOAT"))
            if "trim_end" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN trim_end FLOAT"))
            if "translated_transcript" not in existing_cols:
                conn.execute(text("ALTER TABLE clips ADD COLUMN translated_transcript TEXT"))
            conn.commit()
        except Exception:
            pass

        # Enable SQLite WAL mode and busy timeout for concurrent API & worker safety
        try:
            conn.execute(text("PRAGMA journal_mode=WAL;"))
            conn.execute(text("PRAGMA busy_timeout=5000;"))
            conn.commit()
        except Exception:
            pass

    print("Database tables created/verified with migrations")


def get_database():
    """Dependency to get DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()