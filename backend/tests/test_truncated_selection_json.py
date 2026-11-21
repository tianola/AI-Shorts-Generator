"""A truncated LLM answer must not throw away the clips it finished writing.

Measured 2026-11-12 on job 110: the selection prompt asked for 10 clips, the
backend cut the reply at `max_tokens=2000` after 7 complete ones (6869 chars),
`_extract_json_text` handed json.loads an envelope with no closing `]}`, and the
resulting ValueError dropped the pipeline into the exception fallback at
clip_selector.py:634 — which emits evenly-spaced windows, no LLM picks, and null
score fields. Ten clips shipped that way while the log claimed "Selected 10 clips".

These tests pin the three guards that close that hole:

1. `salvage_incomplete_json` recovers every element that closed before the cut.
2. `select_clips` keeps the AI's chosen windows when the reply is truncated,
   instead of falling back to a grid.
3. The output budget scales with the number of clips asked for, so a normal
   reply is not truncated in the first place.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402

TOKENS_PER_CLIP = 350  # measured: ~285/clip at a 2000-token cap, plus headroom


def _clip(i: int) -> dict:
    """One clip in the shape the model actually emits — verbose enough to matter."""
    return {
        "start": 49.2 + i * 400.0,
        "end": 78.1 + i * 400.0,
        "title": f"THEY CONTROL YOUR DEVICE {i}",
        "hook": f"the hook number {i}",
        "virality_score": 0.82,
        "hook_score": 0.8,
        "coherence_score": 0.7,
        "connection_score": 0.6,
        "trend_score": 0.5,
        "story_type": "reveal",
        "layout": "single_speaker",
        "caption_style": "mrbeast",
        "tone_category": "tech_educational",
        "popup_eligible": True,
        "style_profile": "meme_bouncy",
        "popup_cues": [
            {"query": "bitcoin logo", "start_offset": 3.0, "duration": 2.5,
             "position": "upper_right"}
        ],
        "cta": "Subscribe to find out.",
    }


def _ai_starts(n: int) -> list:
    return [49.2 + i * 400.0 for i in range(n)]


def _truncated(n_complete: int, cut_mid_string: bool = False) -> str:
    """The exact shape a max_tokens cut produces: clips that closed, then the
    stream ends inside the array with no `]` and no `}`."""
    full = json.dumps({"clips": [_clip(i) for i in range(n_complete)]}, indent=4)
    body = full[: full.rindex("]")].rstrip()
    if cut_mid_string:
        body += ',\n        {\n            "start": 99.0,\n            "title": "THE OPEN SOU'
    return body


def test_salvage_leaves_a_valid_reply_exactly_as_json_loads_parsed_it():
    from services.llm_router import salvage_incomplete_json

    text = json.dumps({"clips": [_clip(0), _clip(1)]}, indent=4)
    assert salvage_incomplete_json(text) == json.loads(text)


def test_salvage_keeps_every_clip_that_closed_before_the_cut():
    from services.llm_router import salvage_incomplete_json

    data = salvage_incomplete_json(_truncated(7))

    assert [c["start"] for c in data["clips"]] == _ai_starts(7)


def test_salvage_drops_a_clip_cut_mid_string_instead_of_guessing_it():
    from services.llm_router import salvage_incomplete_json

    data = salvage_incomplete_json(_truncated(3, cut_mid_string=True))

    assert [c["start"] for c in data["clips"]] == _ai_starts(3)
    assert 99.0 not in [c["start"] for c in data["clips"]]


def test_salvage_returns_an_empty_array_when_nothing_closed():
    from services.llm_router import salvage_incomplete_json

    assert salvage_incomplete_json('{"clips": [') == {"clips": []}


def test_salvage_returns_nothing_when_there_is_no_array_to_recover():
    from services.llm_router import salvage_incomplete_json

    assert salvage_incomplete_json("i cannot answer that") == {}


@pytest.mark.asyncio
async def test_a_truncated_selection_reply_still_ships_the_ai_windows(monkeypatch):
    """The regression job 110 actually hit: 7 of 10 clips answered, cut at the token cap."""
    from services import clip_selector

    segments = [
        {"start": i * 30.0, "end": (i + 1) * 30.0, "text": f"spoken words about tech number {i}"}
        for i in range(120)
    ]
    segments_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(segments, segments_file)
    segments_file.close()

    try:
        async def fake_llm(*a, **kw):
            return _truncated(7)

        async def fake_story(*a, **kw):
            return {"story_type": "reveal", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)
        monkeypatch.setattr(settings, "MAX_CLIP_SECONDS", 30, raising=False)
        monkeypatch.setattr(settings, "MIN_CLIP_SECONDS", 20, raising=False)

        result = await clip_selector.select_clips(segments_file.name, num_clips=10)

        # A window is moved onto a transcript boundary when one is within a second
        # of the model's timestamp, so a kept clip is one that starts near its pick.
        starts = [round(c["start"], 1) for c in result]
        for expected in _ai_starts(7):
            assert any(abs(start - expected) <= 1.0 for start in starts), (
                f"the model finished a clip at {expected}s and the run dropped it; starts={starts}"
            )

        # 3600s source / 10 clips is a 360s grid. A reply that lands on every grid
        # point is the exception fallback, i.e. the LLM answer was discarded.
        grid = {round(i * 360.0) for i in range(10)}
        assert not grid.issubset({round(c["start"]) for c in result})
        assert len(result) == 10, f"shortfall must be topped up, not left missing; got {len(result)}"
    finally:
        os.unlink(segments_file.name)


@pytest.mark.asyncio
async def test_the_output_budget_scales_with_the_number_of_clips_asked_for(monkeypatch):
    """One clip costs ~285 tokens in this prompt (6869 chars / 7 clips at a 2000 cap).
    A flat 2000 can only ever answer 7, so asking for 10 guarantees truncation."""
    from services import clip_selector

    segments = [{"start": 0.0, "end": 60.0, "text": "hello there"}]
    segments_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(segments, segments_file)
    segments_file.close()

    seen = {}

    try:
        async def fake_llm(*a, **kw):
            seen["max_tokens"] = kw.get("max_tokens")
            return json.dumps({"clips": [_clip(0)]}, indent=4)

        async def fake_story(*a, **kw):
            return {"story_type": "reveal", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

        await clip_selector.select_clips(segments_file.name, num_clips=10)

        assert seen["max_tokens"] is not None, "select_clips must set its own output budget"
        assert seen["max_tokens"] >= 10 * TOKENS_PER_CLIP, (
            f"10 clips need at least {TOKENS_PER_CLIP} tokens each; "
            f"got {seen['max_tokens']}"
        )
    finally:
        os.unlink(segments_file.name)
