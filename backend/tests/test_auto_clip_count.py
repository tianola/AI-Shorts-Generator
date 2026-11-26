"""Automatic clip count: the selector decides how many moments a video yields.

The model is faked: each call returns candidates for its own part of the transcript, and the
judge passes them all, so the number kept is decided by the ceiling and the candidates alone.
No test calls the real LLM.
"""
import asyncio
import json

import pytest

from config import settings
from services import clip_selector as cs


def _segments(minutes: int = 60):
    segs, t = [], 0.0
    while t < minutes * 60:
        segs.append({"start": t, "end": t + 10.0,
                     "text": "one two three four five six seven eight nine ten eleven twelve."})
        t += 10.0
    return segs


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(_segments()), encoding="utf-8")
    return str(path)


@pytest.fixture
def fake_model(monkeypatch):
    calls = {"n": 0}

    async def model(prompt, schema, max_tokens=None, **_kwargs):
        n = calls["n"]
        calls["n"] += 1
        base = n * cs.AUTO_CHUNK_SECONDS
        clips = [{"start": base + 60 + 50 * j, "end": base + 100 + 50 * j, "title": f"t{n}-{j}",
                  "hook": "hook", "virality_score": 0.8} for j in range(cs.AUTO_CANDIDATES_PER_CHUNK)]
        return json.dumps({"clips": clips}), "fake"

    async def story(segments, video_title=""):
        return {}

    async def keep_all(clips, segments, num_clips, mode="viral", video_path=None, **_kw):
        for clip in clips:
            clip["judge_decision"] = "keep"
            clip["judge_score"] = 0.8
        return True

    monkeypatch.setattr(cs, "_selection_model_json", model)
    monkeypatch.setattr(cs, "analyze_story_segments", story)
    monkeypatch.setattr(cs, "_judge_candidates", keep_all)
    return calls


def test_the_transcript_is_read_in_parts_of_about_fifteen_minutes():
    parts = cs._time_chunks(_segments(60))
    assert len(parts) == 4
    assert all(part[-1]["end"] - part[0]["start"] <= cs.AUTO_CHUNK_SECONDS + 10 for part in parts)


def test_automatic_count_keeps_what_passes_up_to_the_ceiling(video, fake_model, monkeypatch):
    monkeypatch.setattr(settings, "MAX_AUTO_CLIPS", 60, raising=False)
    clips = asyncio.run(cs.select_clips(video, 0))
    assert fake_model["n"] == 4                       # one call per fifteen-minute part
    assert len(clips) == 4 * cs.AUTO_CANDIDATES_PER_CHUNK   # 32 candidates, all passed
    assert len(clips) != settings.NUM_CLIPS           # not the fixed fallback count


def test_the_ceiling_caps_the_automatic_count(video, fake_model, monkeypatch):
    monkeypatch.setattr(settings, "MAX_AUTO_CLIPS", 5, raising=False)
    clips = asyncio.run(cs.select_clips(video, 0))
    assert len(clips) == 5


def test_a_fixed_count_still_returns_that_many(video, fake_model):
    clips = asyncio.run(cs.select_clips(video, 3))
    assert len(clips) == 3


def test_a_model_failure_falls_back_to_the_small_count(video, monkeypatch):
    async def broken(prompt, schema, max_tokens=None):
        raise RuntimeError("model down")

    async def story(segments, video_title=""):
        return {}

    monkeypatch.setattr(cs, "_selection_model_json", broken)
    monkeypatch.setattr(cs, "analyze_story_segments", story)
    clips = asyncio.run(cs.select_clips(video, 0))
    assert 1 <= len(clips) <= max(1, int(settings.NUM_CLIPS))
