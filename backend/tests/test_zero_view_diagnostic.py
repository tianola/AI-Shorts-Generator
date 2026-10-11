"""
Tests for the zero-view diagnostic endpoint.
Tests the /api/test/zero-view-diagnostic endpoint creation and hook style setup.
"""
import os
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

# Add backend to path
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app
from database.session import SessionLocal, init_database
from database.models import Job


@pytest.fixture
def client():
    init_database()
    return TestClient(app)


def test_zero_view_diagnostic_endpoint_exists(client):
    """Test that the endpoint exists and accepts requests."""
    # Create a test request with a dummy URL
    response = client.post(
        "/api/test/zero-view-diagnostic",
        data={
            "youtube_url": "https://www.youtube.com/watch?v=test123",
            "target_clips": 10
        }
    )
    # Should return 200 OK and a job id
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    assert data["message"] is not None


def test_zero_view_diagnostic_sets_flags(client):
    """Test that the job is created with is_zero_view_diagnostic=True and hook_style_list set."""
    response = client.post(
        "/api/test/zero-view-diagnostic",
        data={
            "youtube_url": "https://www.youtube.com/watch?v=test456",
            "target_clips": 10
        }
    )
    data = response.json()
    job_id = data["job_id"]
    
    # Check DB
    db = SessionLocal()
    job = db.query(Job).filter(Job.id == job_id).first()
    assert job is not None
    assert job.is_zero_view_diagnostic == True
    assert job.hook_style_list is not None
    assert len(job.hook_style_list) == 10
    # Check it cycles styles: aggressive, curiosity, story-driven
    assert job.hook_style_list[0] == "aggressive"
    assert job.hook_style_list[1] == "curiosity"
    assert job.hook_style_list[2] == "story-driven"
    assert job.hook_style_list[3] == "aggressive"
    db.close()


def test_zero_view_diagnostic_accepts_file_upload(client):
    """Test that the endpoint accepts file uploads too."""
    # Create a dummy file
    from io import BytesIO
    dummy_file = BytesIO(b"fake mp4 content for test")
    dummy_file.name = "test.mp4"
    
    response = client.post(
        "/api/test/zero-view-diagnostic",
        files={"file": ("test.mp4", dummy_file, "video/mp4")},
        data={
            "target_clips": 10
        }
    )
    assert response.status_code == 200
    data = response.json()
    assert "job_id" in data
    job_id = data["job_id"]
    
    db = SessionLocal()
    job = db.query(Job).filter(Job.id == job_id).first()
    assert job is not None
    assert job.is_zero_view_diagnostic == True
    db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])