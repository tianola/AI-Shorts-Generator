"""The clip already has a designed thumbnail; YouTube never saw it.

`worker.py` renders `thumbnail.jpg` per clip and stores the path in
`clips.thumbnail_path`, but `services/youtube_upload.py` never mentioned
thumbnails, so every short went out with an auto-picked frame. These tests pin
the new behaviour: set the thumbnail after the insert, and never let a
thumbnail failure turn a successful publish into a failure.
"""
import json
from unittest.mock import MagicMock, patch



def _fake_upload_env(tmp_path, thumbnails_raises=False):
    """Build the mocks upload_clip_to_youtube needs, returning the service spy."""
    video = tmp_path / "final_video.mp4"
    video.write_bytes(b"x" * 1024)
    thumb = tmp_path / "thumbnail.jpg"
    thumb.write_bytes(b"jpeg-bytes")

    service = MagicMock()
    service.videos.return_value.insert.return_value.next_chunk.side_effect = [
        (None, {"id": "abc123"})
    ]
    if thumbnails_raises:
        service.thumbnails.return_value.set.side_effect = RuntimeError("403 the user has insufficient permissions")

    return video, thumb, service


def _run_upload(video, thumb, service, tmp_path):
    from services import youtube_upload

    token_file = tmp_path / "youtube_token.json"
    token_file.write_text(json.dumps({"token": "t", "refresh_token": "r",
                                      "id_token": None, "token_uri": "https://oauth2.example",
                                      "client_id": "c", "client_secret": "s",
                                      "scopes": [], "quota_project_id": None,
                                      "type": "authorized_user"}), encoding="utf-8")
    creds = MagicMock()
    creds.expired = False
    creds.refresh_token = None

    with patch.object(youtube_upload, "get_youtube_auth_status",
                      return_value={"authenticated": True, "username": "tester"}), \
         patch.object(youtube_upload, "get_token_path", return_value=str(token_file)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_info",
               return_value=creds), \
         patch("googleapiclient.discovery.build", return_value=service), \
         patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()), \
         patch("services.dimension_checker.check_dimensions",
               return_value={"is_valid_916": True, "width": 1080, "height": 1920, "aspect_ratio": 0.5625}):
        return youtube_upload.upload_clip_to_youtube(
            video_path=str(video),
            title="Your Brain Is Lying About Confusion",
            description="A learning scientist on why rereading fails.",
            hashtags=["#Shorts", "#StudyTips"],
            privacy_status="unlisted",
            thumbnail_path=str(thumb) if thumb else None,
        )


def test_thumbnail_is_set_right_after_the_video_insert(tmp_path):
    video, thumb, service = _fake_upload_env(tmp_path)

    result = _run_upload(video, thumb, service, tmp_path)

    assert result["success"] is True
    service.thumbnails.return_value.set.assert_called_once()
    kwargs = service.thumbnails.return_value.set.call_args.kwargs
    assert kwargs["videoId"] == "abc123"
    assert result["thumbnail_status"] == "set"


def test_a_thumbnail_failure_does_not_undo_the_publish(tmp_path):
    video, thumb, service = _fake_upload_env(tmp_path, thumbnails_raises=True)

    result = _run_upload(video, thumb, service, tmp_path)

    assert result["success"] is True and result["video_id"] == "abc123"
    assert result["thumbnail_status"] == "failed"


def test_no_thumbnail_path_means_no_thumbnail_call(tmp_path):
    video, _thumb, service = _fake_upload_env(tmp_path)

    result = _run_upload(video, None, service, tmp_path)

    assert result["success"] is True
    assert not service.thumbnails.return_value.set.called
    assert result["thumbnail_status"] == "skipped"
