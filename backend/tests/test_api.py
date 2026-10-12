import os

os.environ["TEST_DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from fastapi.testclient import TestClient

from database.base import Base
from database.models import Job
from database.session import SessionLocal, engine

from main import app

Base.metadata.create_all(bind=engine)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def test_create_job_stores_style_prompt(client, db):
    response = client.post(
        "/api/jobs", params={"youtube_url": "https://youtube.com/test", "style_prompt": "cyberpunk"}
    )
    assert response.status_code == 200

    job = db.query(Job).filter(Job.id == response.json()["job_id"]).one()
    assert job.style_prompt == "cyberpunk"


def test_new_job_does_not_auto_publish(client, db):
    """Publishing must stay opt-in: a job created without explicit flags must
    never be uploaded automatically, and must default to unlisted visibility."""
    response = client.post("/api/jobs", params={"youtube_url": "https://youtube.com/test"})
    assert response.status_code == 200

    job = db.query(Job).filter(Job.id == response.json()["job_id"]).one()
    assert job.auto_upload is False
    assert job.upload_privacy == "unlisted"


def test_create_job_rejects_invalid_privacy(client):
    response = client.post(
        "/api/jobs",
        params={"youtube_url": "https://youtube.com/test", "upload_privacy": "everyone"},
    )
    assert response.status_code == 400


def test_create_job_rejects_empty_url(client):
    response = client.post("/api/jobs", params={"youtube_url": "   "})
    assert response.status_code == 400


def test_translate_endpoint_reports_unavailable_not_500(client, db):
    """POST /jobs/{id}/translate used to die on an unhandled ImportError.

    The LLM translation helpers it calls were never implemented in
    services.whisper, so every call answered a bare 500 with no explanation. It
    must answer 501 and say what to do instead, while the request-level checks
    keep their priority.
    """
    from pathlib import Path

    from config import settings

    assert client.post("/api/jobs/999999/translate").status_code == 404

    job = Job(youtube_url="https://youtu.be/x", status="completed")
    db.add(job)
    db.commit()
    db.refresh(job)

    # Only a job that already has a transcript could be translated.
    assert client.post(f"/api/jobs/{job.id}/translate").status_code == 400

    job_dir = Path(settings.JOBS_DIR) / str(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "segments.json").write_text("[]", encoding="utf-8")

    response = client.post(f"/api/jobs/{job.id}/translate")
    assert response.status_code == 501, response.text
    assert "llm_translate=true" in response.json()["detail"]
