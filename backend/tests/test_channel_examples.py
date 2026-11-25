"""The channel's own most and least watched shorts as examples for clip selection.

Pins three things:
1. Nothing is shown until enough clips have real view counts.
2. The best and worst lists are ordered by views, never share a clip, and leave
   out the job being selected.
3. The text reaches both the candidate prompt and the judge prompt.
"""
import os

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import pytest

from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from services import channel_examples as ce
from services import clip_selector


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


def _job(db):
    job = Job(youtube_url="https://youtu.be/x", status="completed")
    db.add(job)
    db.commit()
    return job


def _clips(db, job, views):
    for index, count in enumerate(views):
        db.add(Clip(job_id=job.id, index=index, start=0.0, end=30.0,
                    title=f"title {count}", hook=f"hook {count}", view_count=count))
    db.commit()


def test_nothing_is_shown_before_enough_clips_have_views(db):
    job = _job(db)
    _clips(db, job, [10, 20, 30, 40, 50])
    db.add(Clip(job_id=job.id, index=99, start=0.0, end=30.0, title="never synced"))
    db.commit()
    assert ce.channel_examples(db, per_side=3) == ""


def test_best_and_worst_are_ordered_and_disjoint(db):
    job = _job(db)
    _clips(db, job, [5, 900, 40, 12000, 3, 250, 70])
    text = ce.channel_examples(db, per_side=2)
    best, worst = text.split("Watched the least:")
    assert best.index("12,000 views") < best.index("900 views")
    assert "250 views" not in text and "40 views" not in text
    assert worst.index("5 views") < worst.index("3 views")
    assert 'opening line "hook 12000"' in best


def test_each_side_shrinks_rather_than_repeating_a_clip(db):
    job = _job(db)
    _clips(db, job, [1, 2, 3, 4, 5, 6])
    text = ce.channel_examples(db, per_side=10)
    for count in range(1, 7):
        assert text.count(f"title {count}\"") == 1


def test_the_current_job_is_left_out(db):
    old, current = _job(db), _job(db)
    _clips(db, old, [1, 2, 3, 4, 5, 6])
    _clips(db, current, [99999])
    text = ce.channel_examples(db, per_side=3, exclude_job_id=current.id)
    assert "99,999" not in text and text


def test_zero_turns_the_feature_off(db, monkeypatch):
    job = _job(db)
    _clips(db, job, [1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(settings, "CHANNEL_EXAMPLES", 0, raising=False)
    assert ce.channel_examples(db) == ""


def test_the_examples_reach_the_candidate_prompt():
    segments = [{"start": 0.0, "end": 5.0, "text": "hello there."}]
    prompt = clip_selector._candidate_prompt(segments, {}, 4, 2, "", None,
                                             channel_examples="EXAMPLES-BLOCK")
    assert "EXAMPLES-BLOCK" in prompt
    assert "EXAMPLES-BLOCK" not in clip_selector._candidate_prompt(segments, {}, 4, 2, "", None)


@pytest.mark.asyncio
async def test_the_examples_reach_the_judge_prompt(monkeypatch):
    seen = {}

    async def fake_model(prompt, schema, max_tokens, **kwargs):
        seen["prompt"] = prompt
        return '{"ranked": []}', "ai"

    monkeypatch.setattr(clip_selector, "_selection_model_json", fake_model)
    clips = [{"start": 0.0, "end": 5.0, "title": "a"}, {"start": 5.0, "end": 10.0, "title": "b"}]
    segments = [{"start": 0.0, "end": 5.0, "text": "one."}, {"start": 5.0, "end": 10.0, "text": "two."}]
    await clip_selector._judge_candidates(clips, segments, 1, channel_examples="EXAMPLES-BLOCK")
    assert "EXAMPLES-BLOCK" in seen["prompt"]
