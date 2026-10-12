from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_create_job_stores_layout_and_caption_style():
    response = client.post(
        "/api/jobs",
        params={
            "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "caption_style": "capcut_aqua",
            "layout_mode": "split_screen"
        }
    )
    assert response.status_code == 200
    data = response.json()
    job_id = data["job_id"]

    job_res = client.get(f"/api/jobs/{job_id}")
    assert job_res.status_code == 200
