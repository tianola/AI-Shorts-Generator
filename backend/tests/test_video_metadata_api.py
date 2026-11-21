"""`GET /api/video/metadata` is a URL fetcher: it must pick the URL, and it must
admit when the fetch failed.

Two defects, both operator-visible:

* **SSRF.** The endpoint handed any string it was given to `yt_dlp.extract_info`,
  so the box - which holds a YouTube-authenticated cookie store
  (`settings.YT_COOKIES_FILE`) - would fetch whatever a caller asked for:
  `http://169.254.169.254/...`, an internal admin port, a file on a mount. The
  pipeline only ever processes YouTube sources, so anything else is refused at
  the boundary now.
* **A 200 that lies.** The `except Exception` returned
  `{"title": None, "duration": 0}` with status 200. The Studio reads that as "no
  metadata", the operator sees a blank badge, and the log line is the only trace
  of why the job that gets created from that URL cannot possibly work.

`yt_dlp` is stubbed throughout: nothing here touches the network.
"""
import asyncio
import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from main import app


@pytest.fixture
def client():
    return TestClient(app)


class Recorder:
    """Stand-in for ``yt_dlp.YoutubeDL`` that records instead of fetching."""

    def __init__(self, info=None, error=None):
        self.info = info if info is not None else {
            "title": "A real title", "duration": 125, "thumbnail": "https://i.ytimg.com/x.jpg",
        }
        self.error = error
        self.constructed = []
        self.urls = []
        self.where = []

    def factory(self, opts):
        self.constructed.append(opts)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        try:
            asyncio.get_running_loop()
            self.where.append("on-loop")
        except RuntimeError:
            self.where.append("off-loop")
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.info


@pytest.fixture
def fake_ydl(monkeypatch):
    import yt_dlp

    recorder = Recorder()
    monkeypatch.setattr(yt_dlp, "YoutubeDL", recorder.factory)
    return recorder


def _fail_with(monkeypatch, error):
    import yt_dlp

    recorder = Recorder(error=error)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", recorder.factory)
    return recorder


VALID = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_youtube_url_returns_the_extracted_metadata(client, fake_ydl):
    response = client.get("/api/video/metadata", params={"url": VALID})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "A real title"
    assert body["duration"] == 125.0
    assert body["duration_formatted"] == "2m 5s"
    assert fake_ydl.urls == [VALID]


@pytest.mark.parametrize("url", [
    "https://youtu.be/dQw4w9WgXcQ",
    "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ",
    "https://youtube.com/shorts/dQw4w9WgXcQ",
])
def test_every_youtube_host_is_accepted(client, fake_ydl, url):
    assert client.get("/api/video/metadata", params={"url": url}).status_code == 200


@pytest.mark.parametrize("url, why", [
    ("http://169.254.169.254/latest/meta-data/", "cloud metadata endpoint"),
    ("http://localhost:6379/", "loopback service"),
    ("http://127.0.0.1:8000/api/jobs", "this very API"),
    ("file:///etc/passwd", "local file scheme"),
    ("https://youtube.com.evil.tld/watch?v=1", "suffix look-alike host"),
    ("https://notyoutube.com/watch?v=1", "substring look-alike host"),
    ("https://evil.tld/redirect?to=https://youtu.be/abc", "off-site URL with a YouTube parameter"),
    ("https://youtube.com.evil.tld@attacker.tld/", "userinfo trick"),
    ("", "empty"),
    ("   ", "blank"),
    ("not a url at all", "no scheme, no host"),
])
def test_a_non_youtube_url_is_refused_before_anything_is_fetched(client, fake_ydl, url, why):
    response = client.get("/api/video/metadata", params={"url": url})

    assert response.status_code == 400, f"{url!r} ({why}) -> {response.status_code}"
    assert fake_ydl.constructed == [], (
        "a yt-dlp instance was built for a URL the endpoint should never fetch")
    assert fake_ydl.urls == []


def test_a_fetch_failure_is_an_error_status_not_a_clean_200(client, fake_ydl, monkeypatch):
    """The complaint is the lie, not the failure: duration 0 / title null with a
    200 is what let the UI create a job nobody could diagnose."""
    fake_ydl.error = RuntimeError("ERROR: unable to download webpage")

    response = client.get("/api/video/metadata", params={"url": VALID})

    assert response.status_code == 502, response.text
    assert "unable to download webpage" in response.json()["detail"]


def test_extractor_returning_nothing_is_an_error_too(client, fake_ydl):
    fake_ydl.info = None

    response = client.get("/api/video/metadata", params={"url": VALID})

    assert response.status_code == 502, response.text


def test_private_or_unavailable_video_is_not_reported_as_a_success(client, fake_ydl):
    """duration 0 is a legitimate answer for a live stream, so the contract is
    "title and duration came back", not "the call did not raise"."""
    fake_ydl.info = {"title": None, "duration": None, "thumbnail": None}

    response = client.get("/api/video/metadata", params={"url": VALID})

    assert response.status_code == 502, response.text
    assert response.json()["detail"]


def test_metadata_extraction_does_not_block_the_event_loop(client, fake_ydl):
    """`extract_info` is a 15 s socket call. On the loop it stalls the UI, the
    job websocket and every other request for that long."""
    response = client.get("/api/video/metadata", params={"url": VALID})

    assert response.status_code == 200, response.text
    assert fake_ydl.where == ["off-loop"], (
        f"yt-dlp ran on the event loop ({fake_ydl.where})")


def test_the_cookie_store_is_still_used_for_a_youtube_url(client, fake_ydl, monkeypatch):
    """Refusing non-YouTube hosts must not quietly drop the authentication the
    pipeline needs for age-gated / membership sources."""
    from config import settings

    monkeypatch.setattr(settings, "YT_COOKIES_FILE", "/exists/cookies.txt", raising=False)
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    assert client.get("/api/video/metadata", params={"url": VALID}).status_code == 200
    assert fake_ydl.constructed[-1].get("cookiefile") == "/exists/cookies.txt"


def test_system_info_probes_hardware_off_the_event_loop(client, monkeypatch):
    """`wmic` gets a 5 s timeout; on the loop that is 5 s of frozen UI."""
    import subprocess

    where = []
    real_run = subprocess.run

    def watch_run(cmd, *args, **kwargs):
        try:
            asyncio.get_running_loop()
            where.append(("on-loop", cmd[0] if isinstance(cmd, list) else str(cmd)))
        except RuntimeError:
            where.append(("off-loop", cmd[0] if isinstance(cmd, list) else str(cmd)))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", watch_run)

    response = client.get("/api/system/info")

    assert response.status_code == 200, response.text
    assert where, "no subprocess probe happened at all - the test proves nothing"
    assert all(site == "off-loop" for site, _cmd in where), (
        f"blocking hardware probe ran on the event loop: {where}")
    body = response.json()
    assert body["hardware"]["cpu"]
    assert body["models"]
