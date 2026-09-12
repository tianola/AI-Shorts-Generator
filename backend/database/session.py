"""
Database session management.
SQLAlchemy session factory with proper configuration.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from config import settings
from database.base import Base

# Database URL - SQLite for now, can be changed to PostgreSQL later
# Use absolute path for Windows compatibility
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
    """Initialize database tables."""
    Base.metadata.create_all(bind=engine)
    print("Database tables created/verified")


def get_database():
    """Dependency to get DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()