"""The title brief and checker: generic, hype or unsupported titles never ship.

No test calls a real model: suggest_title is run against a fake call_llm_json.
"""
import asyncio
import json

import pytest

from services import llm_router
from services import title_style as ts

TRANSCRIPT = "he missed the good ending because he fell asleep in the car on the way home"


def test_the_offline_fallback_fits_the_limit_and_never_ends_on_a_dangling_word():
    spoken = ("He had the ball at the last second and passed it instead of shooting, "
              "so the team lost by two points.")
    for topic in ("", "basketball"):
        title, _changed = ts.fix_title(ts.derive_from_transcript(spoken, topic), spoken, topic)
        assert len(title) <= ts.MAX_CHARS
        assert title.split()[-1].lower() not in ts._DANGLING
        assert ts.check_title(title, spoken, topic) == []


def test_a_concrete_title_from_the_clip_passes():
    assert ts.check_title("Why he missed the good ending", TRANSCRIPT) == []


def test_hype_length_emoji_topic_and_unsupported_words_are_reported():
    assert "empty hype phrase" in ts.check_title("You won't believe this ending", TRANSCRIPT)
    assert any("longer" in p for p in ts.check_title("x" * 5 + " good ending " * 6, TRANSCRIPT))
    assert "more than 1 emoji" in ts.check_title("good ending 🔥🔥", TRANSCRIPT)
    assert 'topic "GTA 6" missing' in ts.check_title("He missed the good ending", TRANSCRIPT, "GTA 6")
    assert "no word the clip actually says" in ts.check_title("Pizza prices are going up", TRANSCRIPT)


def test_fix_removes_hype_and_extra_emoji_and_capitalises():
    fixed, changed = ts.fix_title("you won't believe why he missed the good ending!!! 🔥🔥", TRANSCRIPT)
    assert changed and fixed[0].isupper()
    assert "believe" not in fixed.lower()
    assert fixed.count("🔥") <= 1 and "!!!" not in fixed


def test_fix_keeps_the_topic_for_a_live_stream():
    fixed, _ = ts.fix_title("He missed the good ending", TRANSCRIPT, "GTA 6")
    assert fixed.lower().startswith("gta 6")


def test_a_title_the_clip_does_not_support_is_replaced_by_its_own_words():
    fixed, changed = ts.fix_title("Pizza prices are going up fast", TRANSCRIPT)
    assert changed and any(w in fixed.lower() for w in ("missed", "fell", "asleep", "car"))
    assert ts.check_title(fixed, TRANSCRIPT) == []


def test_clean_cuts_at_a_word_boundary():
    text = ts.clean("word " * 30)
    assert len(text) <= ts.MAX_CHARS and not text.endswith(" ")


def test_suggest_title_returns_the_first_title_that_passes(monkeypatch):
    async def fake(prompt, system, model=None, temperature=0.3, max_tokens=600):
        return json.dumps({"titles": ["You won't believe it", "He missed the good ending", "Another one"]})

    monkeypatch.setattr(llm_router, "call_llm_json", fake)
    monkeypatch.setattr(llm_router, "pick_model", lambda *a, **k: "test-model")
    assert asyncio.run(ts.suggest_title(TRANSCRIPT)) == "He missed the good ending"


def test_suggest_title_returns_none_when_nothing_passes(monkeypatch):
    async def fake(prompt, system, model=None, temperature=0.3, max_tokens=600):
        return json.dumps({"titles": ["You won't believe this", "Pizza prices are going up"]})

    monkeypatch.setattr(llm_router, "call_llm_json", fake)
    monkeypatch.setattr(llm_router, "pick_model", lambda *a, **k: "test-model")
    assert asyncio.run(ts.suggest_title(TRANSCRIPT)) is None


def test_retitle_saves_the_new_title_and_asks_for_a_rerender(monkeypatch):
    """The retitle endpoint: title from the clip's words, saved, then re-rendered so the burned title changes."""
    import shutil
    from pathlib import Path

    from api import clips as clips_api
    from config import settings
    from database.models import Clip, Job
    from database.session import SessionLocal, init_database

    init_database()
    db = SessionLocal()
    job = Job(youtube_url="https://youtu.be/x", status="completed", auto_upload=False)
    db.add(job)
    db.flush()
    clip = Clip(job_id=job.id, index=5, start=10.0, end=20.0, title="Old title")
    db.add(clip)
    db.commit()
    clip_dir = Path(settings.JOBS_DIR) / str(job.id) / "clip_5"
    clip_dir.mkdir(parents=True, exist_ok=True)
    (clip_dir / "words.json").write_text(json.dumps([{"word": TRANSCRIPT.split()[0], "start": 0, "end": 1}]
                                                    + [{"word": w, "start": 1, "end": 2} for w in TRANSCRIPT.split()[1:]]),
                                         encoding="utf-8")
    seen = {}

    async def fake_suggest(transcript, topic="", count=3):
        seen["transcript"] = transcript
        return "He missed the good ending"

    async def fake_rerender(clip_id, request, db):
        seen["request"] = request
        return {"message": "ok", "clip": {"id": clip_id, "title": request.title}}

    monkeypatch.setattr(ts, "suggest_title", fake_suggest)
    monkeypatch.setattr(clips_api, "rerender_clip", fake_rerender)
    try:
        import asyncio as _asyncio
        body = _asyncio.run(clips_api.retitle_clip(clip.id, db=db))
        db.refresh(clip)
        assert body["clip"]["title"] == "He missed the good ending"
        assert seen["request"].title == "He missed the good ending"
        assert "fell asleep" in seen["transcript"]
    finally:
        shutil.rmtree(Path(settings.JOBS_DIR) / str(job.id), ignore_errors=True)
        db.query(Clip).delete()
        db.query(Job).delete()
        db.commit()
        db.close()
