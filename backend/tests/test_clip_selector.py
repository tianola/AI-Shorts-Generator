"""Clip length: 30 s floor, 60 s ceiling (it was 30 until job 113 cut mid-sentence).

YouTube Shorts completion rate beats absolute watch time, so the prompt aims at
25-45 s, but a clip may run to a minute rather than lose its last sentence.
These tests pin the guards that enforce the range:

1. The shipped config defaults allow 30-60 s clips.
2. The LLM is not trusted to respect the range — clips longer than MAX
   get trimmed to MAX by the code.
3. The validation floor is 80% of MIN (24s at the default MIN of 30), not the old 60%.
4. The heuristic fallback uses the new MAX when sizing clips.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings


@pytest.fixture
def pin_clip_range(monkeypatch):
    """Lock the clip range to the shipped defaults for every test."""
    monkeypatch.setattr(settings, "MIN_CLIP_SECONDS", 30, raising=False)
    monkeypatch.setattr(settings, "MAX_CLIP_SECONDS", 30, raising=False)
    monkeypatch.setattr(settings, "NUM_CLIPS", 3, raising=False)


def test_the_shipped_defaults_allow_a_story_up_to_a_minute():
    """30 s cut half of job 113's clips mid-sentence; 60 s is a ceiling, and the
    prompt still aims at 25-45 s."""
    assert settings.MIN_CLIP_SECONDS == 30
    assert settings.MAX_CLIP_SECONDS == 60


@pytest.mark.asyncio
async def test_an_ai_clip_longer_than_max_gets_trimmed_to_max(pin_clip_range, monkeypatch):
    """The LLM returned a 50-second clip. Code trims it to MAX_CLIP_SECONDS."""
    from services import clip_selector

    segments = [{"start": 0.0, "end": 300.0, "text": "word " * 40}]
    segments_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    )
    json.dump(segments, segments_file)
    segments_file.close()

    try:
        llm_clips = {
            "clips": [
                {
                    "start": 10.0,
                    "end": 60.0,  # 50 seconds, above the MAX of 30 pinned by the fixture
                    "title": "Too Long Clip",
                    "hook": "Wait for it",
                    "virality_score": 0.9,
                }
            ]
        }

        async def fake_llm(*a, **kw):
            return json.dumps(llm_clips)

        async def fake_story(*a, **kw):
            return {"story_type": "reaction", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

        result = await clip_selector.select_clips(segments_file.name, num_clips=1)

        assert len(result) == 1
        clip = result[0]
        duration = clip["end"] - clip["start"]
        assert duration <= settings.MAX_CLIP_SECONDS, (
            f"clip duration {duration}s exceeds MAX {settings.MAX_CLIP_SECONDS}s"
        )
        assert clip["start"] == 10.0
        assert clip["end"] == 40.0  # 10 + 30
    finally:
        os.unlink(segments_file.name)


@pytest.mark.asyncio
async def test_the_validation_floor_is_eighty_percent_of_min_not_sixty(pin_clip_range, monkeypatch):
    """A 15-second clip fails the 80% floor (24s); a 25-second one passes it."""
    from services import clip_selector

    segments = [{"start": 0.0, "end": 300.0, "text": "word " * 40}]
    segments_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    )
    json.dump(segments, segments_file)
    segments_file.close()

    try:
        llm_clips = {
            "clips": [
                {
                    "start": 5.0,
                    "end": 20.0,  # 15 seconds — below the 24s floor
                    "title": "Too Short Clip",
                    "hook": "Quick",
                    "virality_score": 0.85,
                },
                {
                    "start": 50.0,
                    "end": 75.0,  # 25 seconds, above the 24s floor, valid
                    "title": "Good Clip",
                    "hook": "This one works",
                    "virality_score": 0.88,
                },
            ]
        }

        async def fake_llm(*a, **kw):
            return json.dumps(llm_clips)

        async def fake_story(*a, **kw):
            return {"story_type": "reaction", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

        result = await clip_selector.select_clips(segments_file.name, num_clips=3)

        starts = [c["start"] for c in result]
        assert 5.0 not in starts, "15-second clip should have been filtered out by 0.8 floor"
        assert 50.0 in starts, "25-second clip should pass validation"
    finally:
        os.unlink(segments_file.name)


@pytest.mark.asyncio
async def test_a_heuristic_clip_respects_the_new_max(pin_clip_range, monkeypatch):
    """When the LLM returns nothing usable, the heuristic fallback sizes clips to MAX."""
    from services import clip_selector

    segments = [
        {"start": 0.0, "end": 10.0, "text": "first segment here"},
        {"start": 10.0, "end": 20.0, "text": "second segment here"},
        {"start": 20.0, "end": 30.0, "text": "third segment here"},
    ]
    segments_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False
    )
    json.dump(segments, segments_file)
    segments_file.close()

    try:
        llm_clips = {"clips": []}  # LLM returned nothing

        async def fake_llm(*a, **kw):
            return json.dumps(llm_clips)

        async def fake_story(*a, **kw):
            return {"story_type": "reaction", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

        result = await clip_selector.select_clips(segments_file.name, num_clips=3)

        for clip in result:
            duration = clip["end"] - clip["start"]
            assert duration <= settings.MAX_CLIP_SECONDS, (
                f"heuristic clip duration {duration}s exceeds MAX {settings.MAX_CLIP_SECONDS}s"
            )
    finally:
        os.unlink(segments_file.name)
