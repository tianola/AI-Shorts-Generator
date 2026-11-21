"""``PATCH /api/clips/{id}/update-bounds`` must refuse windows it cannot render.

The endpoint parses the payload with a bare ``float()`` and then checks
``new_end <= new_start``. Two things are wrong with that:

* ``float("nan")`` parses, and every comparison with NaN is False - including
  ``nan <= nan`` and ``nan < 0`` - so the guard passes. The NaN is then written
  to the row, becomes the derived-cache ``window`` key (``worker.process_clip``)
  and ends up as an ``ffmpeg -ss nan`` argument, i.e. the failure surfaces an
  hour later inside a render instead of in the request that caused it.
* There is no ceiling. A window past the end of ``source.mp4`` is accepted here
  and only dies in ``cut_clip``.

These tests pin the boundary: finite, non-negative, ordered, and inside the
source when the source's duration is knowable.
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app

SOURCE_DURATION = 600.0


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def clip(db):
    """A clip row plus the job's source.mp4 on disk, in the temp jobs tree."""
    job = Job(youtube_url="https://youtu.be/bounds", status="completed")
    db.add(job)
    db.commit()
    db.refresh(job)
    row = Clip(job_id=job.id, index=0, start=10.0, end=40.0, title="Window")
    db.add(row)
    db.commit()
    db.refresh(row)

    job_dir = Path(settings.JOBS_DIR) / str(job.id)
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "source.mp4").write_bytes(b"a source, any source")
    return row


@pytest.fixture
def fixed_source_duration(monkeypatch):
    """The ceiling comes from ffprobe; here it is a fixed 600 s source.

    ``source_duration`` stays real, so the tests that ask for it still prove it
    reads ``jobs/<id>/source.mp4`` and not something else.
    """
    from api import clips as clips_api

    monkeypatch.setattr(clips_api, "probe_media_duration",
                        lambda path: SOURCE_DURATION)


def _patch(client, clip_id, body):
    """Send the JSON text verbatim.

    ``json=`` cannot carry these payloads: httpx serialises with allow_nan=False,
    and the point is what a caller that ignores that - or posts the literal
    ``NaN`` token Python's own json module writes - can put on the wire.
    """
    return client.patch(f"/api/clips/{clip_id}/update-bounds",
                        content=body.encode("utf-8"),
                        headers={"Content-Type": "application/json"})


def test_nan_bounds_are_rejected_and_the_row_is_untouched(client, db, clip,
                                                            fixed_source_duration):
    response = _patch(client, clip.id, '{"start": NaN, "end": 50.0}')

    assert response.status_code == 422, response.text
    db.refresh(clip)
    assert (clip.start, clip.end) == (10.0, 40.0)


def test_infinite_bounds_are_rejected(client, db, clip, fixed_source_duration):
    for body in ('{"start": -Infinity, "end": 50.0}',
                 '{"start": 10.0, "end": Infinity}',
                 '{"start": 10.0, "end": NaN}'):
        response = _patch(client, clip.id, body)
        assert response.status_code == 422, f"{body} -> {response.status_code}"
        db.refresh(clip)
        assert (clip.start, clip.end) == (10.0, 40.0)


def test_numeric_strings_that_parse_to_nan_are_rejected(client, db, clip,
                                                          fixed_source_duration):
    """``float("nan")`` succeeds, so the string form reached the row too."""
    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": "nan", "end": "50"})

    assert response.status_code == 422, response.text
    db.refresh(clip)
    assert (clip.start, clip.end) == (10.0, 40.0)


@pytest.mark.parametrize("start,end", [
    (-1.0, 20.0),          # negative start
    (30.0, 20.0),          # inverted
    (20.0, 20.0),          # zero-length
    (0.0, 601.0),          # past the end of the source
    (595.0, 700.0),        # starts inside, ends outside
])
def test_out_of_range_windows_are_rejected(client, db, clip, fixed_source_duration,
                                           start, end):
    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": start, "end": end})

    assert response.status_code == 422, f"{start}-{end} -> {response.status_code}"
    db.refresh(clip)
    assert (clip.start, clip.end) == (10.0, 40.0)


def test_a_window_inside_the_source_is_accepted(client, clip, fixed_source_duration):
    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": 12.5, "end": 37.5})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["start"] == 12.5
    assert body["end"] == 37.5
    assert body["duration"] == pytest.approx(25.0)


def test_a_window_ending_exactly_at_the_source_duration_is_accepted(client, clip,
        fixed_source_duration):
    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": 0.0, "end": SOURCE_DURATION})

    assert response.status_code == 200, response.text


def test_missing_source_rejects_nan_but_keeps_the_ceiling_open(client, db, clip,
                                                                 fixed_source_duration):
    """With no source on disk nothing can be probed, so the endpoint must not
    invent a ceiling - but finite/order still apply."""
    (Path(settings.JOBS_DIR) / str(clip.job_id) / "source.mp4").unlink()

    ok = client.patch(f"/api/clips/{clip.id}/update-bounds",
                      json={"start": 0.0, "end": 9999.0})
    assert ok.status_code == 200, ok.text

    db.refresh(clip)
    assert (clip.start, clip.end) == (0.0, 9999.0)

    bad = _patch(client, clip.id, '{"start": NaN, "end": 9999.0}')
    assert bad.status_code == 422, bad.text


def test_a_probe_that_returns_nothing_is_not_read_as_zero_length(client, clip,
                                                                 monkeypatch):
    from api import clips as clips_api

    monkeypatch.setattr(clips_api, "probe_media_duration", lambda path: None)

    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": 0.0, "end": 9999.0})

    assert response.status_code == 200, response.text


def test_source_duration_reads_the_job_source_not_the_clip(monkeypatch):
    """The ceiling is the *source* length: clip windows are source-absolute."""
    from api import clips as clips_api

    jobs_dir = Path(settings.JOBS_DIR)
    job_dir = jobs_dir / "4242"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "source.mp4").write_bytes(b"not really a video")

    seen = []

    def fake_probe(path):
        seen.append(path)
        return 123.4

    monkeypatch.setattr(clips_api, "probe_media_duration", fake_probe)

    assert clips_api.source_duration(4242) == 123.4
    assert seen and Path(seen[0]).name == "source.mp4"
    # No source on disk => unknown, and "unknown" never means "zero".
    assert clips_api.source_duration(4243) is None
    assert clips_api.source_duration(None) is None


def test_probe_media_duration_reports_nothing_for_unreadable_media(tmp_path):
    """The real ffprobe path: a junk file must answer None, not 0.0.

    0.0 would become a ceiling that rejects every window on the row.
    """
    from api import clips as clips_api

    junk = tmp_path / "source.mp4"
    junk.write_bytes(b"this is not a video")

    assert clips_api.probe_media_duration(str(junk)) is None
    assert clips_api.probe_media_duration(str(tmp_path / "absent.mp4")) is None


def test_update_bounds_does_not_probe_on_the_event_loop(client, clip, monkeypatch):
    """The endpoint is ``async def``; ffprobe must not block the loop it runs on,
    because that loop also serves the UI and the job websocket."""
    import asyncio

    from api import clips as clips_api

    where = []

    def fake_probe(path):
        try:
            asyncio.get_running_loop()
            where.append("on-loop")
        except RuntimeError:
            where.append("off-loop")
        return SOURCE_DURATION

    monkeypatch.setattr(clips_api, "probe_media_duration", fake_probe)

    response = client.patch(f"/api/clips/{clip.id}/update-bounds",
                            json={"start": 1.0, "end": 2.0})

    assert response.status_code == 200, response.text
    assert where == ["off-loop"], (
        "ffprobe ran on the event loop: the UI, the websocket and every other "
        f"request wait behind it ({where})")
