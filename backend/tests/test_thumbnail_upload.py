"""The clip already has a designed thumbnail; YouTube never saw it.

`worker.py` renders `thumbnail.jpg` per clip and stores the path in
`clips.thumbnail_path`, but `services/youtube_upload.py` never mentioned
thumbnails, so every short went out with an auto-picked frame. These tests pin
the new behaviour: the thumbnail request is built *and executed* right after the
insert, and a thumbnail failure never turns a successful publish into a failure.

The execute half is the bug that actually shipped. `thumbnails().set(...)` only
constructs a request; nothing reaches YouTube until `.execute()` runs. The first
version of this module asserted the call was made, passed, and logged "Thumbnail
applied" over an upload that never sent an image.
"""
from tests.youtube_api_fake import fake_service, run_upload


def test_the_thumbnail_request_is_executed_not_just_built(tmp_path):
    service = fake_service()

    result = run_upload(tmp_path, service)

    assert result["success"] is True
    set_call = service.thumbnails.return_value.set
    set_call.assert_called_once()
    assert set_call.call_args.kwargs["videoId"] == "abc123"
    assert set_call.return_value.execute.called is True
    assert result["thumbnail_status"] == "set"


def test_a_thumbnail_failure_does_not_undo_the_publish(tmp_path):
    service = fake_service(thumbnail_execute_raises=True)

    result = run_upload(tmp_path, service)

    assert result["success"] is True and result["video_id"] == "abc123"
    assert result["thumbnail_status"] == "failed"


def test_no_thumbnail_path_means_no_thumbnail_call(tmp_path):
    service = fake_service()

    result = run_upload(tmp_path, service, thumb=False)

    assert result["success"] is True
    assert not service.thumbnails.return_value.set.called
    assert result["thumbnail_status"] == "skipped"
