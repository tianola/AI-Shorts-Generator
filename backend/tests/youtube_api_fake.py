"""One fake YouTube API client, shared by every test that drives an upload.

`upload_clip_to_youtube` needs an authenticated token file, a resumable media body
and a discovery-built service. Each module rebuilt that scaffolding around its own
question, and that is how the thumbnail check passed while asserting a request that
was never sent: a mock answers any question asked of it, so a test that asks "was
`set` called" proves nothing about whether the call was executed. The harness keeps
the service in the caller's hands so a test can read the body that actually left.
"""
import json
from unittest.mock import MagicMock, patch

_TITLE = "Your Brain Is Lying About Confusion"


def fake_service(*, thumbnails_raises=False, thumbnail_execute_raises=False):
    service = MagicMock()
    service.videos.return_value.insert.return_value.next_chunk.side_effect = [
        (None, {"id": "abc123"})
    ]
    if thumbnails_raises:
        service.thumbnails.return_value.set.side_effect = RuntimeError(
            "403 the user has insufficient permissions")
    if thumbnail_execute_raises:
        service.thumbnails.return_value.set.return_value.execute.side_effect = RuntimeError(
            "403 the user has insufficient permissions")
    return service


def insert_body(service):
    """The body videos().insert was handed - the only evidence of what YouTube saw."""
    return service.videos.return_value.insert.call_args.kwargs["body"]


def run_upload(tmp_path, service, *, thumb=True, privacy_status="unlisted",
               publish_at=None, title=_TITLE, thumbnail_path=None):
    """Drive the real uploader against the fake service and return its result."""
    from services import youtube_upload

    video = tmp_path / "final_video.mp4"
    video.write_bytes(b"x" * 1024)
    token_file = tmp_path / "youtube_token.json"
    token_file.write_text(json.dumps({
        "token": "t", "refresh_token": "r", "id_token": None,
        "token_uri": "https://oauth2.example", "client_id": "c",
        "client_secret": "s", "scopes": [], "quota_project_id": None,
        "type": "authorized_user"}), encoding="utf-8")

    (tmp_path / "thumbnail.jpg").write_bytes(b"jpeg-bytes")
    if thumbnail_path is None and thumb:
        thumbnail_path = str(tmp_path / "thumbnail.jpg")

    creds = MagicMock()
    creds.expired = False
    creds.refresh_token = None

    kwargs = {"hashtags": ["#Shorts", "#StudyTips"], "privacy_status": privacy_status,
              "thumbnail_path": thumbnail_path}
    if publish_at is not None:
        kwargs["publish_at"] = publish_at

    with patch.object(youtube_upload, "get_youtube_auth_status",
                      return_value={"authenticated": True, "username": "tester"}), \
         patch.object(youtube_upload, "get_token_path", return_value=str(token_file)), \
         patch("google.oauth2.credentials.Credentials.from_authorized_user_info",
               return_value=creds), \
         patch("googleapiclient.discovery.build", return_value=service), \
         patch("googleapiclient.http.MediaFileUpload", return_value=MagicMock()), \
         patch("services.dimension_checker.check_dimensions",
               return_value={"is_valid_916": True, "width": 1080, "height": 1920,
                             "aspect_ratio": 0.5625}):
        return youtube_upload.upload_clip_to_youtube(
            video_path=str(video),
            title=title,
            description="A learning scientist on why rereading fails.",
            **kwargs,
        )
