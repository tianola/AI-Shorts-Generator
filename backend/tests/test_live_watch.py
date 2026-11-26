"""The Live Studio watch list: saved links, normalising, and the check loop (no network)."""

import pytest

from services import live_watch as lw


@pytest.fixture
def store(monkeypatch, tmp_path):
    from config import settings
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path), raising=False)
    monkeypatch.setattr(lw, "_sessions", {})
    monkeypatch.setattr(lw, "_checks", {})
    return tmp_path


def test_a_channel_page_becomes_its_live_page():
    assert lw.normalise("youtube.com/@SomeChannel") == ("youtube", "https://youtube.com/@SomeChannel/live")
    assert lw.normalise("https://www.youtube.com/watch?v=abc")[1] == "https://www.youtube.com/watch?v=abc"
    assert lw.normalise("twitch.tv/kai") == ("twitch", "https://twitch.tv/kai")
    assert lw.normalise("https://example.com/x")[0] == ""


def test_add_update_remove_and_duplicates(store):
    entry = lw.add("youtube.com/@Chan")
    assert entry["kind"] == "youtube" and entry["enabled"] is True
    with pytest.raises(ValueError):
        lw.add("https://youtube.com/@Chan/live")          # the same live page
    with pytest.raises(ValueError):
        lw.add("https://example.com/nope")
    assert lw.update(entry["id"], enabled=False)["enabled"] is False
    assert lw.view()[0]["state"] == "paused"
    assert lw.remove(entry["id"]) is True and lw.load() == []


def test_a_live_entry_starts_its_own_session_and_an_offline_one_does_not(store, monkeypatch):
    started = []

    class FakeSession:
        status, detail = "live", "Recording"
        def __init__(self, root, kind, url, **options):
            started.append((str(root), kind, url))
        def start(self):
            pass
        def stop(self):
            pass

    live = lw.add("twitch.tv/live_one")
    lw.add("twitch.tv/offline_one")
    monkeypatch.setattr(lw.live_studio, "LiveSession", FakeSession)
    monkeypatch.setattr(lw, "_check", lambda url: {"live": "live_one" in url, "title": "", "channel": ""})
    lw._tick()
    assert [kind for _, kind, _ in started] == ["twitch"]
    assert started[0][2].endswith("live_one")
    assert started[0][0].endswith(live["id"])            # its own folder for buffer and clips
    states = {e["url"].rsplit("/", 1)[-1]: e["state"] for e in lw.view()}
    assert states == {"live_one": "live", "offline_one": "offline"}


def test_not_being_live_is_offline_not_an_error(store, monkeypatch):
    lw.add("youtube.com/@Quiet")

    def fails(url):
        raise RuntimeError("ERROR: This live event will begin in a few hours.")
    monkeypatch.setattr(lw, "_check", fails)
    lw._tick()
    assert lw.view()[0]["state"] == "offline"
