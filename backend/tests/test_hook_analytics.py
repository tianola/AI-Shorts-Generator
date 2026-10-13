"""The hook experiment has to produce a number, not only a video.

A zero-view diagnostic job gives each clip a different hook arm, but the arm was
written into the title and thrown away: the clip row recorded no arm, and nothing
ever asked YouTube what the published short did. So the pipeline could ship five
guesses and report nothing about any of them. These tests cover both halves -
which arm each clip shipped with, and the counts that come back.
"""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from api.common import clip_payload
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from main import app
import services.hook_analytics as analytics
import services.youtube_stats as stats
import worker


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture
def client():
    init_database()
    return TestClient(app)


# ---------------------------------------------------------------------------
# the arm is recorded where it can still be read back
# ---------------------------------------------------------------------------

def _select_clips(monkeypatch, job, clips):
    """Run the real selection stage with the selector's judgement stubbed out."""
    async def fake_select(segments_file, num_target_clips, video_title=None):
        return [dict(clip) for clip in clips]

    monkeypatch.setattr(worker, "select_clips", fake_select)
    return asyncio.run(worker.stage_select_clips(
        1, job, "segments.json", "source", lambda stage, percent: None))


def test_a_diagnostic_job_records_which_arm_each_clip_shipped_with(monkeypatch):
    job = SimpleNamespace(
        target_clips=2,
        is_zero_view_diagnostic=True,
        hook_style_list=["aggressive", "curiosity"],
    )
    clips = _select_clips(monkeypatch, job, [
        {"start": 0.0, "end": 9.0, "title": "One", "hook": "H1"},
        {"start": 9.0, "end": 18.0, "title": "Two", "hook": "H2"},
    ])

    assert [c["hook_style"] for c in clips] == ["aggressive", "curiosity"]
    # The visible mutation still happens; recording the arm must not undo it.
    assert clips[0]["title"].startswith("[AGGRESSIVE]")


def test_an_ordinary_job_claims_no_arm(monkeypatch):
    job = SimpleNamespace(target_clips=1, is_zero_view_diagnostic=False,
                          hook_style_list=None)
    clips = _select_clips(monkeypatch, job,
                          [{"start": 0.0, "end": 9.0, "title": "One", "hook": "H1"}])

    assert "hook_style" not in clips[0], "only a diagnostic job may label an arm"


def test_the_arm_and_the_numbers_reach_the_api_payload(db):
    clip = Clip(job_id=1, index=0, start=0.0, end=9.0, hook_style="curiosity",
                view_count=1234, like_count=40, comment_count=3,
                stats_synced_at=datetime(2026, 10, 1, 12, 0))
    db.add(clip)
    db.commit()

    payload = clip_payload(clip)
    assert payload["hook_style"] == "curiosity"
    assert (payload["view_count"], payload["like_count"], payload["comment_count"]) == (1234, 40, 3)


# ---------------------------------------------------------------------------
# reading the numbers back
# ---------------------------------------------------------------------------

class _StubYouTube:
    """videos().list(part=..., id=...).execute(), the only calls the service makes."""

    def __init__(self, items):
        self.items = items
        self.batches = []

    def videos(self):
        return self

    def list(self, part, id):
        self.batches.append(id.split(","))
        return self

    def execute(self):
        return {"items": self.items}


def _clip(db, *, job_id=1, index=0, **fields):
    clip = Clip(job_id=job_id, index=index, start=0.0, end=9.0, **fields)
    db.add(clip)
    db.commit()
    return clip


def test_placeholder_ids_are_never_sent_to_the_api():
    clips = [SimpleNamespace(youtube_video_id="dQw4w9WgXcQ"),
             SimpleNamespace(youtube_video_id="queued"),
             SimpleNamespace(youtube_video_id=None),
             SimpleNamespace(youtube_video_id="dQw4w9WgXcQ")]
    assert stats.real_video_ids(clips) == ["dQw4w9WgXcQ"]


def test_a_stat_the_platform_reports_is_turned_into_a_number():
    client = _StubYouTube([{"id": "dQw4w9WgXcQ",
                            "statistics": {"viewCount": "17", "likeCount": "0"}}])

    assert stats.fetch_statistics(["dQw4w9WgXcQ"], client=client) == {
        "dQw4w9WgXcQ": {"viewCount": 17, "likeCount": 0}}


def test_a_stat_the_platform_withholds_stays_absent_rather_than_zero():
    # A private video reports views but no likes at all.
    client = _StubYouTube([{"id": "dQw4w9WgXcQ", "statistics": {"viewCount": "17"}}])

    assert stats.fetch_statistics(["dQw4w9WgXcQ"], client=client) == {
        "dQw4w9WgXcQ": {"viewCount": 17}}


def test_ids_are_queried_in_batches_of_fifty():
    ids = [f"{i:011d}" for i in range(120)]
    client = _StubYouTube([])

    stats.fetch_statistics(ids, client=client)

    assert [len(b) for b in client.batches] == [50, 50, 20]


def test_sync_writes_the_counts_onto_the_clips_that_were_published(db):
    published = _clip(db, index=0, uploaded=True, youtube_video_id="dQw4w9WgXcQ",
                      hook_style="aggressive")
    _clip(db, index=1, uploaded=True, youtube_video_id="queued")
    _clip(db, index=2, uploaded=False)

    client = _StubYouTube([{"id": "dQw4w9WgXcQ", "statistics": {
        "viewCount": "240", "likeCount": "12", "commentCount": "4"}}])
    summary = stats.sync_clip_stats(db, client=client)

    assert summary == {"clips_with_id": 2, "placeholders": 1, "videos_queried": 1,
                       "updated": 1, "missing_on_youtube": 0}
    db.refresh(published)
    assert (published.view_count, published.like_count, published.comment_count) == (240, 12, 4)
    assert published.stats_synced_at is not None


def test_a_deleted_video_is_missing_not_zeroed(db):
    clip = _clip(db, uploaded=True, youtube_video_id="dQw4w9WgXcQ", view_count=99)

    summary = stats.sync_clip_stats(db, client=_StubYouTube([]))

    assert summary["missing_on_youtube"] == 1
    db.refresh(clip)
    assert clip.view_count == 99, "a deleted upload is not a video nobody watched"
    assert clip.stats_synced_at is not None


def test_sync_of_one_job_leaves_the_others_alone(db):
    _clip(db, job_id=1, index=0, uploaded=True, youtube_video_id="AAAAAAAAAAA")
    other = _clip(db, job_id=2, index=0, uploaded=True, youtube_video_id="BBBBBBBBBBB")

    stats.sync_clip_stats(db, job_id=1, client=_StubYouTube([
        {"id": "AAAAAAAAAAA", "statistics": {"viewCount": "5"}}]))

    db.refresh(other)
    assert other.view_count is None


def test_nothing_is_queried_when_no_clip_has_a_real_id(db):
    _clip(db, uploaded=True, youtube_video_id="queued")

    class _Never:
        def videos(self):
            raise AssertionError("asked YouTube about a placeholder")

    assert stats.sync_clip_stats(db, client=_Never())["videos_queried"] == 0


# ---------------------------------------------------------------------------
# comparing the arms
# ---------------------------------------------------------------------------

def test_the_arm_that_pulled_most_views_is_reported_first(db):
    _clip(db, index=0, hook_style="curiosity", uploaded=True, view_count=100)
    _clip(db, index=1, hook_style="curiosity", uploaded=True, view_count=300)
    _clip(db, index=2, hook_style="aggressive", uploaded=True, view_count=50)

    groups = analytics.summarize_hook_styles(db)

    assert list(groups) == ["curiosity", "aggressive"]
    arm = groups["curiosity"]
    assert (arm["clips"], arm["uploaded"], arm["with_stats"]) == (2, 2, 2)
    assert (arm["total_views"], arm["mean_views"], arm["median_views"]) == (400, 200.0, 200.0)
    assert arm["top_views"] == 300
    assert arm["top_clip_id"] == groups["curiosity"]["top_clip_id"]


def test_an_even_sample_medians_the_two_middle_clips(db):
    for index, views in enumerate((10, 20, 30, 40)):
        _clip(db, index=index, hook_style="story-driven", view_count=views)

    group = analytics.summarize_hook_styles(db)["story-driven"]

    assert group["median_views"] == 25.0
    assert group["mean_views"] == 25.0


def test_an_arm_nobody_has_measured_yet_is_not_average_zero(db):
    _clip(db, index=0, hook_style="aggressive", uploaded=True)
    _clip(db, index=1, hook_style="curiosity", uploaded=True, view_count=7)

    groups = analytics.summarize_hook_styles(db)

    assert groups["aggressive"]["clips"] == 1
    assert groups["aggressive"]["with_stats"] == 0
    assert groups["aggressive"]["mean_views"] is None
    assert list(groups) == ["curiosity", "aggressive"], "unmeasured arms go last"


def test_clips_without_an_arm_are_not_credited_to_one(db):
    _clip(db, index=0, hook_style="aggressive", view_count=10)
    _clip(db, index=1, view_count=90)

    groups = analytics.summarize_hook_styles(db)

    assert groups[analytics.UNASSIGNED]["clips"] == 1
    assert groups[analytics.UNASSIGNED]["mean_views"] == 90.0
    assert groups["aggressive"]["mean_views"] == 10.0


def test_the_summary_can_be_narrowed_to_one_job(db):
    _clip(db, job_id=1, index=0, hook_style="aggressive", view_count=10)
    _clip(db, job_id=2, index=0, hook_style="aggressive", view_count=990)

    assert analytics.summarize_hook_styles(db, job_id=1)["aggressive"]["mean_views"] == 10.0


# ---------------------------------------------------------------------------
# the two surfaces people reach these numbers through
# ---------------------------------------------------------------------------

def test_the_endpoint_summarises_what_is_already_stored(client, db):
    _clip(db, hook_style="aggressive", uploaded=True, view_count=60)

    body = client.get("/api/analytics/hook-styles").json()

    assert body["hook_styles"]["aggressive"]["mean_views"] == 60.0


def test_the_sync_endpoint_uses_the_stored_token(client, db, monkeypatch):
    _clip(db, uploaded=True, youtube_video_id="dQw4w9WgXcQ")
    seen = {}

    def build_client():
        seen["called"] = True
        return _StubYouTube([{"id": "dQw4w9WgXcQ", "statistics": {"viewCount": "3"}}])

    monkeypatch.setattr(stats, "_build_client", build_client)
    response = client.post("/api/analytics/sync-views")

    assert response.status_code == 200, response.text
    assert seen.get("called")
    assert response.json()["sync"]["updated"] == 1


def test_the_sync_endpoint_says_so_when_youtube_is_not_connected(client, db, monkeypatch):
    _clip(db, uploaded=True, youtube_video_id="dQw4w9WgXcQ")

    def build_client():
        raise FileNotFoundError("no token file")

    monkeypatch.setattr(stats, "_build_client", build_client)
    response = client.post("/api/analytics/sync-views")

    assert response.status_code == 409
    assert "not connected" in response.json()["detail"]


def test_the_report_prints_one_row_per_arm(capsys, db):
    _clip(db, index=0, hook_style="aggressive", uploaded=True, view_count=30)
    _clip(db, index=1, hook_style="curiosity", uploaded=True, view_count=1500)

    from scripts.hook_style_report import main

    assert main([]) == 0
    out = capsys.readouterr().out
    assert out.index("curiosity") < out.index("aggressive")
    assert "1500" in out
    assert "leads" in out, "with both arms measured the report may name the winner"


def test_the_report_json_is_machine_readable(capsys, db):
    _clip(db, hook_style="aggressive", uploaded=True, view_count=30)
    capsys.readouterr()  # the schema setup prints; --json must print nothing but JSON

    from scripts.hook_style_report import main

    assert main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["hook_styles"]["aggressive"]["total_views"] == 30
    assert payload["sync"] is None


def test_the_report_explains_a_database_that_predates_the_columns(capsys, monkeypatch):
    import scripts.hook_style_report as report

    def explode(*args, **kwargs):
        raise OperationalError("SELECT", {}, Exception("no such column: clips.hook_style"))

    monkeypatch.setattr(report, "summarize_hook_styles", explode)

    assert report.main([]) == 2
    assert "no hook_style / stats columns" in capsys.readouterr().err
