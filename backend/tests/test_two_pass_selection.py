"""Two-pass clip selection: candidates, then a ranking pass, with code placing the windows.

The old selector asked the model for exactly the number of clips to publish and
shipped every one it got back; the "virality" composite only reordered them, and
45% of it was English keyword matching that read nothing in a Greek transcript.
These tests pin what replaced it:

1. More candidates are requested than will ship, and a second pass decides.
2. Windows are placed on transcript boundaries by code, never cut mid-sentence
   when a boundary fits.
3. Two picks of the same moment collapse into one.
4. Claude is used when a key is configured and the OmniRoute chain is the
   fallback - and every clip says which path it came from.
5. A clip no model chose is not queued for publishing on its own.
6. A model pick keeps the selector's headline through metadata generation.

No test here reaches a real model: conftest blanks ANTHROPIC_API_KEY and every
call is replaced with a fake.
"""
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings  # noqa: E402
from services import clip_selector  # noqa: E402


@pytest.fixture(autouse=True)
def _clip_range(monkeypatch):
    monkeypatch.setattr(settings, "MIN_CLIP_SECONDS", 20, raising=False)
    monkeypatch.setattr(settings, "MAX_CLIP_SECONDS", 30, raising=False)


def _segments_file(tmp_path, segments):
    path = tmp_path / "segments.json"
    path.write_text(json.dumps(segments), encoding="utf-8")
    return str(path)


def _ten_second_segments(count=60):
    return [
        {"start": i * 10.0, "end": (i + 1) * 10.0, "text": f"sentence number {i}."}
        for i in range(count)
    ]


def _candidate(start, end, title, score=0.5):
    return {"start": start, "end": end, "title": title, "hook": "opening", "virality_score": score}


class _TwoPassModel:
    """A fake LLM that answers the candidate prompt and the ranking prompt separately."""

    def __init__(self, candidates, ranking=None):
        self.candidates = candidates
        self.ranking = ranking
        self.prompts = []

    async def __call__(self, *args, **kwargs):
        prompt = kwargs.get("prompt") or args[0]
        self.prompts.append(prompt)
        if '"ranked"' in prompt:
            if self.ranking is None:
                return "i would rather not rank these"
            return json.dumps({"ranked": self.ranking})
        return json.dumps({"clips": self.candidates})


async def _story(*args, **kwargs):
    return {"story_type": "reveal", "cta": "Follow for more.", "story_summary": "a talk"}


def _patch_models(monkeypatch, model):
    monkeypatch.setattr(clip_selector, "call_llm_json", model)
    monkeypatch.setattr(clip_selector, "analyze_story_segments", _story)


# ---------------------------------------------------------------- two passes

def test_more_candidates_are_requested_than_will_be_published():
    assert clip_selector._candidates_wanted(1) == 5
    assert clip_selector._candidates_wanted(4) == 8
    assert clip_selector._candidates_wanted(10) == 20
    assert clip_selector._candidates_wanted(20) == 30, "capped so the reply can be finished"


@pytest.mark.asyncio
async def test_the_ranking_pass_decides_which_candidates_ship(tmp_path, monkeypatch):
    """The first pass loved c0 and was lukewarm on c2; the second pass, reading only
    what the viewer hears, says the opposite. The second pass wins."""
    candidates = [
        _candidate(0.0, 30.0, "FIRST PASS FAVOURITE", 0.9),
        _candidate(100.0, 130.0, "MIDDLE", 0.6),
        _candidate(200.0, 230.0, "SECOND PASS FAVOURITE", 0.5),
    ]
    ranking = [
        {"candidate_id": "c0", "score": 2.0, "reason": "opens on a greeting"},
        {"candidate_id": "c1", "score": 5.0, "reason": "fine"},
        {"candidate_id": "c2", "score": 9.5, "reason": "opens on the reveal"},
    ]
    model = _TwoPassModel(candidates, ranking)
    _patch_models(monkeypatch, model)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=2)

    assert [c["title"] for c in result] == ["SECOND PASS FAVOURITE", "MIDDLE"]
    assert result[0]["judge_score"] == pytest.approx(0.95)
    assert result[0]["judge_reason"] == "opens on the reveal"
    assert result[0]["llm_score_raw"] == pytest.approx(0.5), "the first-pass rating is kept for the breakdown"
    assert len(model.prompts) == 2, "one candidate call, one ranking call"


@pytest.mark.asyncio
async def test_the_ranking_prompt_shows_the_words_inside_each_window(tmp_path, monkeypatch):
    model = _TwoPassModel(
        [_candidate(20.0, 50.0, "A"), _candidate(100.0, 130.0, "B")],
        [{"candidate_id": "c0", "score": 5, "reason": ""}, {"candidate_id": "c1", "score": 6, "reason": ""}],
    )
    _patch_models(monkeypatch, model)

    await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=1)

    ranking_prompt = model.prompts[1]
    assert "OPENING LINE: sentence number 2." in ranking_prompt
    assert "sentence number 1." not in ranking_prompt, (
        "the sentence before the clip only touches its edge; it is not what the viewer hears"
    )


@pytest.mark.asyncio
async def test_an_unusable_ranking_reply_keeps_the_first_pass_order(tmp_path, monkeypatch):
    model = _TwoPassModel(
        [_candidate(0.0, 30.0, "WEAK", 0.4), _candidate(100.0, 130.0, "STRONG", 0.9)],
        ranking=None,
    )
    _patch_models(monkeypatch, model)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=2)

    assert [c["title"] for c in result] == ["STRONG", "WEAK"]
    assert "judge_score" not in result[0]


@pytest.mark.asyncio
async def test_two_picks_of_the_same_moment_collapse_into_the_stronger_one(tmp_path, monkeypatch):
    model = _TwoPassModel([
        _candidate(100.0, 130.0, "THE MOMENT", 0.9),
        _candidate(110.0, 140.0, "THE SAME MOMENT AGAIN", 0.8),
        _candidate(300.0, 330.0, "ANOTHER MOMENT", 0.5),
    ])
    _patch_models(monkeypatch, model)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=2)

    assert [c["title"] for c in result] == ["THE MOMENT", "ANOTHER MOMENT"]


# ---------------------------------------------------------------- windows

def test_a_timestamp_near_a_sentence_edge_moves_onto_it():
    segments = _ten_second_segments()
    assert clip_selector._fit_window(99.4, 130.7, segments) == (100.0, 130.0)


def test_a_window_that_is_too_long_ends_on_the_last_sentence_that_fits():
    """start+MAX lands mid-sentence; the last segment end inside the limit does not."""
    segments = [
        {"start": 0.0, "end": 12.0, "text": "a."},
        {"start": 12.0, "end": 27.0, "text": "b."},
        {"start": 27.0, "end": 41.0, "text": "c."},
    ]
    assert clip_selector._fit_window(0.0, 41.0, segments) == (0.0, 27.0)


def test_a_window_that_is_slightly_short_is_extended_to_the_next_sentence_end():
    segments = [
        {"start": 0.0, "end": 14.0, "text": "a."},
        {"start": 14.0, "end": 22.0, "text": "b."},
        {"start": 22.0, "end": 60.0, "text": "c."},
    ]
    assert clip_selector._fit_window(0.0, 14.0, segments) == (0.0, 22.0)


def test_a_window_outside_the_video_is_dropped():
    segments = _ten_second_segments(count=3)
    assert clip_selector._fit_window(500.0, 525.0, segments) is None
    assert clip_selector._fit_window(-5.0, 20.0, segments) is None
    assert clip_selector._fit_window(20.0, 10.0, segments) is None


def test_a_segment_that_only_touches_the_window_is_not_inside_it():
    segments = _ten_second_segments(count=5)
    inside = clip_selector._segments_in_window(segments, 10.0, 30.0)
    assert [seg["text"] for seg in inside] == ["sentence number 1.", "sentence number 2."]


# ---------------------------------------------------------------- ranking score

def test_english_keywords_do_not_move_the_ranking():
    """Two clips the model rated the same must rank the same whatever English
    power words one of them happens to contain."""
    plain = {"llm_score_raw": 0.7, "audio_dynamics": 0.5, "pacing_score": 0.9,
             "visual_event_density": 0.3}
    keyworded = {**plain, "keyword_score": 1.0, "hook_strength": 1.0, "emotional_intensity": 1.0}
    assert clip_selector._selection_score(plain) == clip_selector._selection_score(keyworded)


# ---------------------------------------------------------------- language

def test_a_greek_transcript_is_named_in_the_prompt():
    segments = [{"start": 0.0, "end": 30.0, "text": "Αυτό είναι το μυστικό που δεν σου είπε κανείς."}]
    prompt = clip_selector._candidate_prompt(segments, {}, 5, 1, "", None)
    assert "language spoken in the transcript (Greek)" in prompt
    assert "Never switch to another language or script" in prompt


def test_the_prompt_asks_for_a_teasing_title_not_a_summary():
    prompt = clip_selector._candidate_prompt(_ten_second_segments(3), {}, 5, 1, "", None)
    assert "not to summarise" in prompt
    assert "never invent stakes" in prompt
    assert "STALKER" not in prompt.upper(), "the old examples anchored every title on one story"


# ---------------------------------------------------------------- provenance

@pytest.mark.asyncio
async def test_claude_is_used_when_a_key_is_configured(tmp_path, monkeypatch):
    from services import claude_client

    seen = []

    async def fake_claude(prompt, system, schema, model=None, max_tokens=16000, effort="medium"):
        seen.append(schema)
        if "ranked" in schema["properties"]:
            return json.dumps({"ranked": [{"candidate_id": "c0", "score": 8, "reason": "r"},
                                          {"candidate_id": "c1", "score": 3, "reason": "r"}]})
        return json.dumps({"clips": [_candidate(0.0, 30.0, "A", 0.5), _candidate(100.0, 130.0, "B", 0.5)]})

    async def never(*args, **kwargs):
        raise AssertionError("the OmniRoute chain must not be called while Claude answers")

    monkeypatch.setattr(claude_client, "claude_available", lambda: True)
    monkeypatch.setattr(claude_client, "call_claude_json", fake_claude)
    monkeypatch.setattr(clip_selector, "call_llm_json", never)
    monkeypatch.setattr(clip_selector, "analyze_story_segments", _story)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=1)

    assert result[0]["title"] == "A"
    assert result[0]["selection_path"] == "claude"
    assert len(seen) == 2, "candidates and ranking both went to Claude"


@pytest.mark.asyncio
async def test_a_claude_failure_falls_back_to_the_omniroute_chain(tmp_path, monkeypatch):
    from services import claude_client

    async def broken_claude(*args, **kwargs):
        raise RuntimeError("credit balance is too low")

    model = _TwoPassModel([_candidate(0.0, 30.0, "FROM THE FREE CHAIN", 0.8)])
    monkeypatch.setattr(claude_client, "claude_available", lambda: True)
    monkeypatch.setattr(claude_client, "call_claude_json", broken_claude)
    _patch_models(monkeypatch, model)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=1)

    assert result[0]["title"] == "FROM THE FREE CHAIN"
    assert result[0]["selection_path"] == "ai"


@pytest.mark.asyncio
async def test_a_total_outage_marks_every_clip_as_grid(tmp_path, monkeypatch):
    async def down(*args, **kwargs):
        raise RuntimeError("All LLM backends failed")

    _patch_models(monkeypatch, down)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=3)

    assert result, "the job still renders something to review"
    assert {c["selection_path"] for c in result} == {"grid"}


@pytest.mark.asyncio
async def test_a_shortfall_is_filled_with_clips_marked_heuristic(tmp_path, monkeypatch):
    model = _TwoPassModel([_candidate(0.0, 30.0, "THE ONLY MODEL PICK", 0.8)])
    _patch_models(monkeypatch, model)

    result = await clip_selector.select_clips(_segments_file(tmp_path, _ten_second_segments()), num_clips=3)

    assert len(result) == 3
    assert result[0]["selection_path"] == "ai"
    assert {c["selection_path"] for c in result[1:]} == {"heuristic"}


# ---------------------------------------------------------------- publish queue

@pytest.fixture
def db():
    from database.models import Clip, Job
    from database.session import SessionLocal, init_database

    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _job_with_clips(db, paths):
    from database.models import Clip, Job

    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=True)
    db.add(job)
    db.commit()
    for index, path in enumerate(paths):
        db.add(Clip(job_id=job.id, index=index, start=0.0, end=25.0, title=f"clip {index}",
                    file_path=f"jobs/{job.id}/clip_{index}/final_video.mp4",
                    score_breakdown={"selection_path": path} if path else None))
    db.commit()
    return job


def test_only_model_picks_are_queued_automatically(db, monkeypatch):
    import worker
    from database.models import Clip

    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    job = _job_with_clips(db, ["claude", "heuristic", "grid", "ai", None])

    queued = worker.enqueue_clips_for_publish(db, job.id)

    rows = db.query(Clip).filter(Clip.job_id == job.id).order_by(Clip.index).all()
    assert queued == 3
    assert [row.publish_due_at is not None for row in rows] == [True, False, False, True, True], (
        "a row with no recorded path predates this rule and keeps its old behaviour"
    )


def test_the_operator_pressing_upload_queues_the_unreviewed_clips_too(db, monkeypatch):
    import worker
    from database.models import Clip

    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    job = _job_with_clips(db, ["heuristic", "grid"])

    assert worker.enqueue_clips_for_publish(db, job.id) == 0
    assert worker.enqueue_clips_for_publish(db, job.id, include_unreviewed=True) == 2
    assert all(row.publish_due_at is not None
               for row in db.query(Clip).filter(Clip.job_id == job.id).all())


# ---------------------------------------------------------------- headline

@pytest.mark.asyncio
async def test_a_model_pick_keeps_the_selectors_headline():
    from services.metadata import ClipMetadata, generate_metadata

    rewritten = ClipMetadata(title="A Man Discusses His Business", description="d", hashtags=["#Shorts"])
    with patch("services.metadata.call_llm_structured", new_callable=AsyncMock, return_value=rewritten):
        kept = await generate_metadata("transcript", "He Lost 40,000 In One Weekend", "hook", keep_title=True)
        replaced = await generate_metadata("transcript", "He Lost 40,000 In One Weekend", "hook")

    assert kept["title"] == "He Lost 40,000 In One Weekend"
    assert kept["description"] == "d", "description and hashtags are still generated"
    assert replaced["title"] == "A Man Discusses His Business"


@pytest.mark.asyncio
async def test_a_placeholder_headline_is_never_kept():
    from services.metadata import ClipMetadata, generate_metadata

    written = ClipMetadata(title="The Real Title", description="d", hashtags=["#Shorts"])
    with patch("services.metadata.call_llm_structured", new_callable=AsyncMock, return_value=written):
        result = await generate_metadata("transcript", "VIRAL MOMENT 2", "hook", keep_title=True)

    assert result["title"] == "The Real Title"
