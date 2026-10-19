"""The local VLM tier: an always-available, no-API vision provider.

Real-world clips (buildings, construction, cities) have no COCO class, so the
remote Gemini/OmniRoute tiers are the only thing that can name them — and both
are currently unusable (credits / model not served). This adds a third, LOCAL
tier that talks to a running Ollama vision model over the same /api/generate
HTTP shape the text router already uses, so a frame can be described without any
external account. The tests never hit a real Ollama: the HTTP client and frame
extraction are faked.
"""
import asyncio
import json

import numpy as np
import pytest

import services.vlm_scene_attention as vlm
from config import settings


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Stand-in for httpx.AsyncClient: records the POST, returns _FakeClient.response."""
    last: dict = {}
    response: dict = {"response": ""}

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeClient.last = {"url": url, "payload": json}
        return _FakeResponse(_FakeClient.response)


def _frame():
    return np.zeros((4, 4, 3), dtype=np.uint8)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_VLM_URL", "http://localhost:11434")
    monkeypatch.setattr(settings, "LOCAL_VLM_MODEL", "qwen2.5-vl:test")
    monkeypatch.setattr(settings, "LOCAL_VLM_ENABLED", True)


def test_local_vlm_posts_the_frame_as_an_image(monkeypatch):
    """The local provider must send the sampled frame to Ollama and parse its JSON."""
    _FakeClient.response = {"response": json.dumps({"subject": "construction site",
                                                    "objects": ["building", "crane"],
                                                    "action": "building", "confidence": 0.8})}
    monkeypatch.setattr(vlm.httpx, "AsyncClient", _FakeClient)

    result = asyncio.run(vlm._local_vlm(_frame(), "they are building a house here"))

    sent = _FakeClient.last["payload"]
    assert sent["model"] == "qwen2.5-vl:test"
    assert isinstance(sent.get("images"), list) and len(sent["images"]) == 1
    assert sent["images"][0]  # base64 payload is non-empty
    assert result["subject"] == "construction site"
    assert result["confidence"] == 0.8


def test_local_vlm_is_preferred_when_enabled(monkeypatch):
    """With the local tier on, understand_scene_vlm uses it before the remote tiers."""
    async def _local_ok(frame, transcript):
        return {"subject": "local sees a city", "objects": [], "action": "reveal",
                "confidence": 0.7, "source": "local_ollama"}

    async def _remote_boom(frame, transcript):
        raise AssertionError("remote must not be reached when local answers")

    monkeypatch.setattr(vlm, "_extract_frame", lambda path, ts: _frame())
    monkeypatch.setattr(vlm, "_local_vlm", _local_ok)
    monkeypatch.setattr(vlm, "_google_gemini_vision", _remote_boom)

    result = asyncio.run(vlm.understand_scene_vlm("clip.mp4", 1.0, "a city"))
    assert result["subject"] == "local sees a city"


def test_local_vlm_skipped_when_disabled(monkeypatch):
    """Off by default is wrong for the channel but the code must honour the flag:
    when disabled the local provider is not attempted and the chain still works."""
    monkeypatch.setattr(settings, "LOCAL_VLM_ENABLED", False)
    called = {"local": False}

    async def _local_spy(frame, transcript):
        called["local"] = True
        raise AssertionError("must not run")

    def _hybrid_spy(video_path, ts, transcript):  # sync, like the real one
        return {"subject": "person", "objects": [], "action": "speaking",
                "confidence": 0.5, "source": "hybrid_fallback"}

    async def _remote_boom(frame, transcript):
        raise ConnectionError("no remote")

    monkeypatch.setattr(vlm, "_extract_frame", lambda path, ts: _frame())
    monkeypatch.setattr(vlm, "_local_vlm", _local_spy)
    monkeypatch.setattr(vlm, "_google_gemini_vision", _remote_boom)
    monkeypatch.setattr(vlm, "_omniroute_vision", _remote_boom)
    monkeypatch.setattr(vlm, "_hybrid_understanding", _hybrid_spy)

    result = asyncio.run(vlm.understand_scene_vlm("clip.mp4", 1.0, "hi"))
    assert called["local"] is False
    assert result["source"] == "hybrid_fallback"
