from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_media_proxy_blocks_sensitive_and_text_files():
    # Attempt to read secrets
    res_cookies = client.get("/media/secrets/cookies.txt")
    assert res_cookies.status_code == 403

    # Attempt to read databases or logs
    res_db = client.get("/media/app.db")
    assert res_db.status_code == 403

    res_log = client.get("/media/jobs/0/pipeline.log")
    assert res_log.status_code == 403

def test_media_proxy_allows_media_extensions_404_when_missing():
    # An allowed media extension returns 404 if missing, not 403
    res_mp4 = client.get("/media/jobs/999999/final_video.mp4")
    assert res_mp4.status_code == 404
