from unittest.mock import patch
from fastapi.testclient import TestClient
from main import app
from services.youtube_upload import (
    format_shorts_metadata,
)

client = TestClient(app)

def test_format_shorts_metadata():
    # Plain title
    meta1 = format_shorts_metadata("Mindblowing Secret Revealed", "Check this out", ["#AI", "Tech"])
    assert "#Shorts" in meta1["title"]
    assert len(meta1["title"]) <= 100
    assert "#AI" in meta1["description"]
    assert "AI" in meta1["tags"]
    assert "Shorts" in meta1["tags"]

    # Long title truncation
    meta2 = format_shorts_metadata("A" * 150, "Desc", ["#Viral"])
    assert len(meta2["title"]) <= 100
    assert meta2["title"].endswith(" #Shorts")

def test_youtube_status_endpoint():
    response = client.get("/api/youtube/status")
    assert response.status_code == 200
    data = response.json()
    assert "authenticated" in data
    assert "has_client_secrets" in data

def test_youtube_auth_url_endpoint_without_secrets():
    with patch("services.youtube_upload.get_auth_url", return_value=None):
        response = client.post("/api/youtube/auth-url")
        assert response.status_code == 400


def test_batch_upload_endpoint_unauthorized():
    with patch("services.youtube_upload.get_youtube_auth_status", return_value={"authenticated": False}):
        response = client.post("/api/jobs/999/upload-all-youtube", json={"privacy_status": "public"})
        assert response.status_code == 401
