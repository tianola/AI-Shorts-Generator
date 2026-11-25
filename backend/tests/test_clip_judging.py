"""Eleven-dimension judging, keep/reject, editing modes and topic segmentation."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import clip_judging as cj  # noqa: E402


def _row(**over):
    row = {"candidate_id": "c0", "decision": "keep", "reason": "ok"}
    row.update({name: 8 for name in cj.DIMENSIONS})
    row.update(over)
    return row


def test_a_complete_row_parses_and_clamps():
    parsed = cj.parse_judgement(_row(hook=14, payoff=-3))
    assert parsed["decision"] == "keep"
    assert parsed["dims"]["hook"] == 10.0 and parsed["dims"]["payoff"] == 0.0


@pytest.mark.parametrize("bad", [{"decision": "maybe"}, {"decision": None}])
def test_an_unknown_decision_is_not_an_approval(bad):
    assert cj.parse_judgement(_row(**bad)) is None


def test_a_row_missing_a_dimension_is_unusable():
    row = _row()
    del row["coherence"]
    assert cj.parse_judgement(row) is None


def test_the_dimension_score_is_a_weighted_mean_on_zero_to_one():
    assert cj.dimension_score({n: 10 for n in cj.DIMENSIONS}) == pytest.approx(1.0)
    assert cj.dimension_score({n: 5 for n in cj.DIMENSIONS}) == pytest.approx(0.5)


def test_an_incoherent_clip_is_pulled_down_hard():
    dims = {n: 8 for n in cj.DIMENSIONS}
    good = cj.apply_penalties(0.8, dims, "keep", 40)
    dims["coherence"] = 3
    assert cj.apply_penalties(0.8, dims, "keep", 40) < good - 0.25


def test_reject_and_context_dependence_cost_score_and_preferred_length_earns_some():
    dims = {n: 8 for n in cj.DIMENSIONS}
    base = cj.apply_penalties(0.7, dims, "keep", 20)
    assert cj.apply_penalties(0.7, dims, "reject", 20) < base
    assert cj.apply_penalties(0.7, dims, "keep", 40) > base
    dims["standalone"] = 2
    assert cj.apply_penalties(0.7, dims, "keep", 20) < base


def test_unknown_modes_mean_auto_and_auto_guidance_is_viral():
    assert cj.normalise_mode("GAMING") == "gaming"
    assert cj.normalise_mode("nonsense") == "auto" and cj.normalise_mode(None) == "auto"
    assert "viral" in cj.mode_guidance("auto").lower()
    assert "funny" in cj.mode_guidance("funny").lower()


def _segments(topics):
    """Ten 12-second segments per topic, each topic with its own vocabulary."""
    segs, t = [], 0.0
    for words in topics:
        for i in range(10):
            text = " ".join(words[(i + k) % len(words)] for k in range(14))
            segs.append({"start": t, "end": t + 12.0, "text": text})
            t += 12.0
    return segs


def test_topic_boundaries_land_where_the_vocabulary_changes():
    cooking = ["pasta", "sauce", "tomato", "garlic", "boiling", "kitchen", "recipe", "simmer"]
    rockets = ["rocket", "engine", "orbit", "thrust", "launch", "payload", "fuel", "stage"]
    segs = _segments([cooking, rockets])
    boundaries = cj.topic_boundaries(segs)
    assert boundaries, "a clear subject change must be found"
    assert any(abs(b - 120.0) <= 24.0 for b in boundaries)


def test_no_boundaries_in_a_short_or_single_subject_transcript():
    assert cj.topic_boundaries([{"start": 0, "end": 5, "text": "just a few words here"}]) == []


def test_a_window_stopping_just_before_a_break_is_extended_to_it():
    assert cj.fit_to_topic(100.0, 130.0, [138.0], max_len=60) == (100.0, 138.0)


def test_extension_respects_the_length_ceiling_and_reach():
    assert cj.fit_to_topic(100.0, 130.0, [138.0], max_len=30) == (100.0, 130.0)
    assert cj.fit_to_topic(100.0, 130.0, [190.0], max_len=100) == (100.0, 130.0)


def test_a_window_starting_just_after_a_break_is_pulled_back_to_it():
    assert cj.fit_to_topic(103.0, 140.0, [100.0], max_len=60)[0] == 100.0


@pytest.mark.asyncio
async def test_rejected_candidates_never_ship_and_all_rejected_means_no_short(monkeypatch, tmp_path):
    from services import clip_selector

    segs = [{"start": float(i * 10), "end": float(i * 10 + 10), "text": f"Sentence number {i} ends here."}
            for i in range(30)]
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(segs), encoding="utf-8")
    candidates = {"clips": [
        {"start": 0.0, "end": 35.0, "title": "A", "hook": "h", "why": "w", "virality_score": 0.9},
        {"start": 100.0, "end": 135.0, "title": "B", "hook": "h", "why": "w", "virality_score": 0.8},
    ]}

    verdict = {"decision": "reject"}

    async def fake_model(prompt, schema, max_tokens, **kwargs):
        if '"ranked"' in prompt:
            return json.dumps({"ranked": [_row(candidate_id="c0", **verdict), _row(candidate_id="c1", **verdict)]}), "ai"
        return json.dumps(candidates), "ai"

    async def fake_story(*a, **kw):
        return {"story_type": "reaction", "cta": "x"}

    monkeypatch.setattr(clip_selector, "_selection_model_json", fake_model)
    monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

    assert await clip_selector.select_clips(str(path), num_clips=2) == []

    verdict["decision"] = "keep"
    kept = await clip_selector.select_clips(str(path), num_clips=2)
    assert len(kept) == 2 and all(c["judge_decision"] == "keep" for c in kept)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply,expected", [
    ('{"mode": "gaming"}', "gaming"), ('{"mode": "FUNNY"}', "funny"),
    ('{"mode": "cooking"}', "viral"), ("not json", "viral"),
])
async def test_the_mode_is_read_from_the_video(monkeypatch, reply, expected):
    import services.llm_router as router

    async def fake(**_kw):
        return reply
    monkeypatch.setattr(router, "call_llm_json", fake)
    segs = [{"start": 0, "end": 5, "text": "welcome back to the stream"}] * 6
    assert await cj.infer_mode("Ranked grind", segs) == expected


@pytest.mark.asyncio
async def test_a_failing_model_or_empty_transcript_means_viral(monkeypatch):
    import services.llm_router as router

    async def boom(**_kw):
        raise RuntimeError("down")
    monkeypatch.setattr(router, "call_llm_json", boom)
    assert await cj.infer_mode("t", [{"text": "hello there"}]) == "viral"
    assert await cj.infer_mode("t", []) == "viral"


@pytest.mark.asyncio
async def test_auto_picks_a_mode_and_tags_every_clip(monkeypatch, tmp_path):
    from services import clip_selector

    seen = {}

    async def fake_infer(title, segments):
        return "gaming"

    async def fake_inner(path, num, title, events, mode, **kwargs):
        seen["mode"] = mode
        return [{"start": 0.0, "end": 30.0}]
    monkeypatch.setattr(clip_selector, "infer_mode", fake_infer)
    monkeypatch.setattr(clip_selector, "_select_clips", fake_inner)
    path = tmp_path / "segments.json"
    path.write_text(json.dumps([{"start": 0, "end": 5, "text": "hi"}]), encoding="utf-8")

    clips = await clip_selector.select_clips(str(path), 1, mode="auto")
    assert seen["mode"] == "gaming" and clips[0]["edit_mode"] == "gaming"
    clips = await clip_selector.select_clips(str(path), 1, mode="funny")
    assert seen["mode"] == "funny" and clips[0]["edit_mode"] == "funny"
