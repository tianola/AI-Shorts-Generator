from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
from main import app
from services.youtube_upload import (
    format_shorts_metadata,
)
import json
import pytest
from google.auth.exceptions import RefreshError

client = TestClient(app)

def test_format_shorts_metadata():
    # Plain title. The tag list is the clip's own: the fixed filler cloud
    # (Viral/Trending/FYP/ForYou/Storytime/Explore) used to be appended to every
    # upload, which is the generic signal a niche channel has to stop sending.
    meta1 = format_shorts_metadata("Mindblowing Secret Revealed", "Check this out", ["#AI", "Tech"])
    assert "#Shorts" in meta1["title"]
    assert len(meta1["title"]) <= 100
    assert "#AI" in meta1["description"]
    assert meta1["tags"] == ["AI", "Tech"]
    assert not {"Viral", "Trending", "FYP", "ForYou", "Storytime", "Explore"} & set(meta1["tags"])

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


# --- why the channel is not connected -------------------------------------
# Job 109 lost five clips to a refresh token Google had already killed. The queue
# said nothing, and the panel this dict feeds said only `authenticated: false`, so
# the operator could not tell "never connected" from "connected, then revoked" -
# which are different problems with different repairs. The reason is now part of the
# answer instead of a warning line in a rotating log nobody reads.
def _status_with_token(tmp_path, monkeypatch, error=None):
    """Drive the real status check against a temp storage tree and a fake Google."""
    from config import settings
    from services import youtube_upload

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "client_secrets.json").write_text(
        json.dumps({"installed": {"client_id": "c", "client_secret": "s",
                                  "auth_uri": "https://oauth2.example",
                                  "token_uri": "https://oauth2.example"}}),
        encoding="utf-8")
    token = secrets / "youtube_token.json"
    token.write_text(json.dumps({"token": "t", "refresh_token": "r", "type": "authorized_user"}),
                     encoding="utf-8")

    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))

    creds = MagicMock()
    creds.expired = True
    creds.refresh_token = "r"
    creds.valid = True
    creds.to_json.return_value = json.dumps({"token": "t2", "refresh_token": "r",
                                             "type": "authorized_user"})
    if error is not None:
        creds.refresh.side_effect = error

    with patch("google.oauth2.credentials.Credentials.from_authorized_user_info",
               return_value=creds), \
         patch("googleapiclient.discovery.build") as build:
        build.return_value.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"snippet": {"title": "Test Channel"}}]}
        return youtube_upload.get_youtube_auth_status()


def test_a_revoked_token_is_reported_as_the_refusal_it_actually_is(tmp_path, monkeypatch):
    refused = RefreshError("invalid_grant", "Token has been expired or revoked.", status_code=400)
    status = _status_with_token(tmp_path, monkeypatch, error=refused)

    assert status["authenticated"] is False
    reason = status["reason"].lower()
    # Google's own words, not a generic "check your credentials".
    assert "invalid_grant" in reason
    assert "expired or revoked" in reason
    # And the step that repairs it - which no retry, restart or code change can do.
    assert "settings" in reason
    assert "connect" in reason


def test_a_transient_failure_is_not_reported_as_a_revoked_token(tmp_path, monkeypatch):
    """Only Google's refusal may be called a dead token.

    The check also covers a socket timeout or a broken token file, and telling an
    operator to re-consent the channel for those sends them to the wrong door while
    the real fault keeps happening.
    """
    status = _status_with_token(tmp_path, monkeypatch,
                                error=ConnectionError("HTTPSConnectionPool: Read timed out."))

    assert status["authenticated"] is False
    reason = status["reason"].lower()
    assert "expired or revoked google-side" not in reason
    assert "read timed out" in reason


def test_a_channel_never_authorized_is_not_blamed_on_a_dead_token(tmp_path, monkeypatch):
    from config import settings
    from services import youtube_upload

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "client_secrets.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))

    status = youtube_upload.get_youtube_auth_status()

    assert status["authenticated"] is False
    reason = status["reason"].lower()
    assert "expired" not in reason and "revoked" not in reason
    assert "connect" in reason


def test_a_live_connection_carries_no_reason(tmp_path, monkeypatch):
    status = _status_with_token(tmp_path, monkeypatch)

    assert status["authenticated"] is True
    assert status["channel_name"] == "Test Channel"
    assert status["reason"] is None


# --- what the refusal says when a clip cannot go out ----------------------
# The uploader's PermissionError is not only what the operator is shown: the publish
# queue stores it verbatim as clip.upload_error and classifies on its wording, so a
# generic sentence both hides the cause from the ledger and can be mistaken for a
# fault in the clip rather than in the channel.
def test_the_uploader_refusal_carries_the_reason_it_cannot_upload(tmp_path):
    from services import youtube_upload

    video = tmp_path / "final_video.mp4"
    video.write_bytes(b"x" * 1024)
    refusal = ("Google refused the saved authorization ('invalid_grant', "
               "'Token has been expired or revoked.'). Reconnect the channel in "
               "Settings -> YouTube.")

    with patch.object(youtube_upload, "get_youtube_auth_status",
                      return_value={"authenticated": False, "reason": refusal}):
        with pytest.raises(PermissionError) as exc:
            youtube_upload.upload_clip_to_youtube(
                video_path=str(video), title="A title", description="A description",
                hashtags=["#Shorts"], privacy_status="unlisted")

    message = str(exc.value)
    assert "not authenticated" in message
    assert "invalid_grant" in message
    assert "Settings -> YouTube" in message


def test_both_refusals_are_words_the_publish_queue_forgives(tmp_path):
    """The queue's channel-auth classifier must recognise every refusal this service raises.

    A disconnected channel is forgiven - it costs no strike - but only if the message
    says so in a shape worker._is_channel_auth_failure knows. Wording drifting here is
    how a whole-channel outage gets charged to individual clips again.
    """
    from services import youtube_upload
    import worker

    refusal = ("Google refused the saved authorization ('invalid_grant', "
               "'Token has been expired or revoked.')")
    status = {"authenticated": False, "reason": refusal}
    video = tmp_path / "final_video.mp4"
    video.write_bytes(b"x" * 1024)

    with patch.object(youtube_upload, "get_youtube_auth_status", return_value=status), \
         patch("services.dimension_checker.check_dimensions",
               return_value={"is_valid_916": True}):
        messages = []
        with pytest.raises(PermissionError) as exc:
            youtube_upload.upload_clip_to_youtube(
                video_path=str(video), title="A title", description="A description",
                hashtags=["#Shorts"], privacy_status="unlisted")
        messages.append(str(exc.value))
        with pytest.raises(PermissionError) as exc:
            youtube_upload.upload_all_clips_for_job(1, None)
        messages.append(str(exc.value))

    for message in messages:
        assert worker._is_channel_auth_failure(message), message
        assert refusal in message
