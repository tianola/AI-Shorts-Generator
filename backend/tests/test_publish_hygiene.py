"""The queue has to behave like a person running a channel, not like a machine with a queue.

Three separate ceilings decide what "correct" means here, and the pipeline used to respect
only one of them:

* the **channel's rolling upload budget** (`UPLOAD_DAILY_BUDGET`) - YouTube's unpublished
  per-channel count, measured at about ten per rolling 24 h on this channel, spent when the
  bytes land and *not* when a video goes public. It answers `uploadLimitExceeded`, and the
  old behaviour was to discover that by getting refused.
* the **release cadence** (`PUBLISH_SCHEDULE_GAP_SECONDS` + `PUBLISH_RELEASE_WINDOW_UTC`) -
  what the audience sees. Growth guidance says 1-3 Shorts a day, so uploads must be paced by
  releases: burning the whole day's upload budget on 15 clips that release over a week spends
  budget the channel never gets to use.
* the **look of the output** - the same CTA sentence, a title repeated inside the description
  and fifteen clips from one stream published back to back are what a repost farm is, and
  each is a spam/claim signal on its own.

Everything is asserted against an in-memory database and a fake uploader. No request reaches
YouTube or TikTok here.
"""
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import worker
from config import settings
from database.models import Clip, Job
from database.session import SessionLocal, init_database
from services.youtube_upload import format_shorts_metadata

NOW = datetime(2026, 11, 12, 3, 0)
VIDEO_FILE = "jobs/1/clip_0/final_video.mp4"


@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.fixture(autouse=True)
def _budget_and_cadence(monkeypatch):
    """Pinned, so a changed default can never make a guard test pass by accident."""
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 3600, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_GAP_JITTER_SECONDS", 0, raising=False)
    monkeypatch.setattr(settings, "UPLOAD_DAILY_BUDGET", 8, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 43200, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_LEAD_SECONDS", 900, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_UPLOAD_LEAD_SECONDS", 21600, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "6-22", raising=False)
    monkeypatch.setattr(settings, "PUBLISH_SAME_SOURCE_MIN_GAP_SECONDS", 86400, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_LIMIT_RETRY_HOURS", 6, raising=False)
    monkeypatch.setattr(settings, "PUBLISH_RETRY_MINUTES", 30, raising=False)
    monkeypatch.setattr(settings, "MAX_UPLOAD_RETRIES", 3, raising=False)


@pytest.fixture(autouse=True)
def _rendered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    directory = tmp_path / "jobs" / "1" / "clip_0"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "final_video.mp4").write_bytes(b"0" * 16)
    return tmp_path


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"video_id": f"vid{len(self.calls)}",
                "shorts_url": f"https://youtube.com/shorts/vid{len(self.calls)}"}


def _job(db, url="https://youtu.be/x", *, published=0, queued=0, first_index=0):
    job = Job(youtube_url=url, status="completed", auto_upload=True)
    db.add(job)
    db.flush()
    for offset in range(published):
        db.add(Clip(job_id=job.id, index=first_index + offset, start=0.0, end=9.0,
                    title=f"Done {offset}", file_path=VIDEO_FILE,
                    youtube_video_id=f"done{offset}", uploaded=True,
                    published_at=NOW - timedelta(hours=5)))
    for offset in range(queued):
        db.add(Clip(job_id=job.id, index=first_index + published + offset, start=0.0, end=9.0,
                    title=f"Queued {offset}", description="d", hashtags=["#Shorts"],
                    file_path=VIDEO_FILE, publish_due_at=NOW - timedelta(minutes=1),
                    scheduled_publish_at=NOW + timedelta(hours=8)))
    db.commit()
    return job


# ------------------------------------------------------- the rolling upload budget

def test_the_queue_refuses_to_upload_past_the_channels_budget(db):
    """If this guard were missing, the eighth attempt would go out and eat a 400.

    The budget is counted from rows that actually have a video id, so it survives a restart
    exactly like the upload clock does.
    """
    _job(db, published=8, queued=1)
    uploader = _Recorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 0
    assert uploader.calls == []

    waiting = db.query(Clip).filter(Clip.youtube_video_id.is_(None)).one()
    # The five-hour-old successes age out at NOW+19h; that is when headroom returns.
    assert waiting.publish_due_at == NOW + timedelta(hours=19)


def test_a_budget_below_the_measured_cap_never_reaches_youtubes_refusal(db):
    """One slot under the budget still uploads: the guard stops at the line, not before it."""
    _job(db, published=7, queued=1)
    uploader = _Recorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 1
    assert len(uploader.calls) == 1


def test_a_refusal_now_waits_for_the_window_to_clear_not_for_a_fixed_six_hours(db):
    """YouTube still answers uploadLimitExceeded if the real cap is lower than our budget.

    Guessing six hours means the next attempt lands inside the same full window and is
    refused again; the window's own shape says when the next slot actually frees.
    """
    _job(db, published=8, queued=1)
    clip = db.query(Clip).filter(Clip.youtube_video_id.is_(None)).one()

    worker._record_publish_failure(db, clip, NOW,
                                   'HttpError 400 ... "reason": "uploadLimitExceeded"')

    assert clip.publish_due_at == NOW + timedelta(hours=19)
    assert clip.upload_retry_count == 0, "the channel's budget is not the clip's fault"


# ------------------------------------- a disconnected channel is no clip's fault

AUTH_REFUSAL = ("YouTube account is not authenticated. "
                "Please connect your YouTube account first.")
TOKEN_REVOKED = ("('invalid_grant: Token has been expired or revoked.', "
                 "{'error': 'invalid_grant', 'error_description': "
                 "'Token has been expired or revoked.'})")


@pytest.fixture
def logged(monkeypatch):
    """What the operator would actually read in the log, level included."""
    records = []
    monkeypatch.setattr(worker, "job_warning",
                        lambda jid, m: records.append(("warning", m)))
    monkeypatch.setattr(worker, "job_error",
                        lambda jid, m: records.append(("error", m)))
    return records


def _waiting_clip(db):
    _job(db, queued=1)
    return db.query(Clip).filter(Clip.youtube_video_id.is_(None)).one()


def test_an_expired_token_does_not_charge_the_clips_that_could_not_go_out(db, logged):
    """Job 109 lost five clips this way overnight: three refusals in ninety minutes.

    The strikes are a budget for faults in *this* upload. A channel nobody can
    authenticate against fails every clip identically, so it retires the whole
    queue while the operator's own next upload looks fine.
    """
    clip = _waiting_clip(db)

    worker._record_publish_failure(db, clip, NOW, AUTH_REFUSAL)

    assert clip.upload_retry_count == 0, "the channel being offline is not this clip's fault"
    assert clip.publish_due_at == NOW + timedelta(minutes=30)
    assert not any("left the publish queue" in m for _, m in logged)


def test_a_revoked_refresh_token_holds_the_queue_the_same_way(db, logged):
    """The same outage arrives as google's RefreshError when the upload is already past
    the auth check - the classifier has to know both shapes or one of them still strikes.
    """
    clip = _waiting_clip(db)

    worker._record_publish_failure(db, clip, NOW, TOKEN_REVOKED)

    assert clip.upload_retry_count == 0
    assert clip.publish_due_at == NOW + timedelta(minutes=30)


def test_the_release_slot_survives_an_outage(db, logged):
    """A clip held back by the channel keeps its slot; the schedule is the channel's.

    Retiring with the slot cleared is what made those five clips invisible - no clock
    left set means nothing in the UI or the queue can find them again. Tried at the
    boundary, on a clip whose next failure would have been its third strike.
    """
    clip = _waiting_clip(db)
    clip.upload_retry_count = settings.MAX_UPLOAD_RETRIES - 1
    db.commit()
    slot = clip.scheduled_publish_at

    worker._record_publish_failure(db, clip, NOW, AUTH_REFUSAL)

    assert clip.upload_retry_count == settings.MAX_UPLOAD_RETRIES - 1
    assert clip.scheduled_publish_at == slot


def test_the_outage_is_announced_with_the_step_that_repairs_it(db, logged):
    """A warning naming a clip is the wrong shape for a failure of the whole channel.

    The line has to say what to do: the token cannot be renewed by code, only by a
    human re-consenting in Settings.
    """
    clip = _waiting_clip(db)

    worker._record_publish_failure(db, clip, NOW, AUTH_REFUSAL)

    errors = [m for level, m in logged if level == "error"]
    text = " ".join(errors).lower()
    assert errors, f"an outage is only logged as {logged}"
    assert "connect" in text and "settings" in text, f"recovery not named: {text}"


def test_an_ordinary_upload_failure_still_costs_a_strike(db, logged):
    """The exemption must not become a blanket: without it nothing ever leaves the queue.

    A crop that cannot be encoded, or a 403 on one video, is this clip's problem.
    """
    clip = _waiting_clip(db)

    worker._record_publish_failure(db, clip, NOW, "HttpError 403 forbidden for this video")

    assert clip.upload_retry_count == 1
    assert clip.publish_due_at == NOW + timedelta(minutes=30)


# ------------------------------------------------------ the release cadence and hours

def test_a_slot_outside_the_release_window_moves_to_its_next_open_hour(db):
    """03:00 UTC is not a person posting; the window is what makes the hours look human."""
    assert worker.next_release_slot(db, now=datetime(2026, 11, 12, 3, 0)) \
        == datetime(2026, 11, 12, 6, 0)
    assert worker.next_release_slot(db, now=datetime(2026, 11, 11, 23, 30)) \
        == datetime(2026, 11, 12, 6, 0)


def test_an_empty_window_leaves_the_schedule_alone(db, monkeypatch):
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    assert worker.next_release_slot(db, now=datetime(2026, 11, 12, 3, 0)) \
        == datetime(2026, 11, 12, 3, 15)


def test_two_jobs_from_one_source_never_release_close_together(db, monkeypatch):
    """Fifteen clips of the same stream over two days is the repost-farm shape.

    A different source may still interleave on the shared cadence, so the channel keeps its
    rhythm while one video cannot dominate it. The window is switched off here so this test
    measures the anchor and nothing else.
    """
    monkeypatch.setattr(settings, "PUBLISH_RELEASE_WINDOW_UTC", "", raising=False)
    same = _job(db, url="https://youtu.be/same", published=1)
    db.add(Clip(job_id=same.id, index=1, start=0.0, end=9.0, title="released",
                file_path=VIDEO_FILE, youtube_video_id="r", uploaded=True,
                published_at=NOW - timedelta(hours=5),
                scheduled_publish_at=NOW - timedelta(hours=5)))
    db.commit()

    from_source = worker.next_release_slot(db, now=NOW,
                                           source_url="https://youtu.be/same")
    from_other = worker.next_release_slot(db, now=NOW, source_url="https://youtu.be/other")

    # That release went out at NOW-5h: the same source waits a full day for its
    # next clip, while another one only waits out the channel's own 12 h cadence.
    assert from_source == NOW + timedelta(hours=19), from_source
    assert from_other == NOW + timedelta(hours=7), from_other
    assert from_source - from_other == timedelta(hours=12)


def test_a_clip_becomes_due_on_the_way_to_its_release_not_before_it(db):
    """Uploads are paced by the release clock, so budget is never spent ahead of use.

    With a 12 h cadence and a 6 h lead the first clip is due now (its slot is 06:00 UTC) and
    the rest are dated into the future, so one pass ships one clip and the queue sits idle
    until the next slot approaches. Yesterday's queue would have fired all three inside an
    hour of each other and burned a day of upload budget on videos not yet released.
    """
    job = _job(db, queued=3)
    for clip in db.query(Clip).filter(Clip.youtube_video_id.is_(None)).all():
        clip.publish_due_at = None
        clip.scheduled_publish_at = None
    db.commit()

    assert worker.enqueue_clips_for_publish(db, job.id, now=NOW) == 3
    rows = db.query(Clip).order_by(Clip.index.asc()).all()
    for row in rows:
        assert row.publish_due_at == row.scheduled_publish_at - timedelta(hours=6), row.index
    assert rows[1].publish_due_at > NOW
    assert rows[2].publish_due_at > NOW

    uploader = _Recorder()
    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 1
    assert len(uploader.calls) == 1
    assert uploader.calls[0]["publish_at"] == rows[0].scheduled_publish_at
    # An hour later nothing else is due yet, even though a whole clip is queued.
    assert worker.publish_queued_clips(db, now=NOW + timedelta(hours=1),
                                       uploader=uploader) == 0
    assert len(uploader.calls) == 1


def test_a_job_that_runs_past_the_window_edge_does_not_leak_outside_it(db, monkeypatch):
    """Snapping only the first slot handed the later ones an out-of-window release.

    An hour of cadence from 21:45 gives 22:45 and 23:45, and the 03:17-style hours
    are the whole thing the window exists to keep off the channel.
    """
    monkeypatch.setattr(settings, "PUBLISH_SCHEDULE_GAP_SECONDS", 3600, raising=False)
    job = _job(db, queued=3)
    for clip in db.query(Clip).filter(Clip.youtube_video_id.is_(None)).all():
        clip.publish_due_at = None
        clip.scheduled_publish_at = None
    db.commit()

    start = datetime(2026, 11, 12, 21, 30)
    assert worker.enqueue_clips_for_publish(db, job.id, now=start) == 3
    rows = db.query(Clip).order_by(Clip.index.asc()).all()
    assert [row.scheduled_publish_at for row in rows] == [
        datetime(2026, 11, 12, 21, 45),
        datetime(2026, 11, 13, 6, 0),
        datetime(2026, 11, 13, 7, 0),
    ]


# ------------------------------------------------------------- what the video reads like

def test_the_description_credits_the_source_instead_of_repeating_the_title():
    meta = format_shorts_metadata("One Punch Man Reacts", "A clip about the fight.",
                                  ["#Shorts"], source_title="Full stream",
                                  source_url="https://youtu.be/abc123")

    assert "https://youtu.be/abc123" in meta["description"]
    assert "Full stream" in meta["description"]
    assert "daily viral clips" not in meta["description"].lower()
    assert meta["description"].count("One Punch Man Reacts") <= 1


def test_the_closing_line_comes_from_settings_and_defaults_to_nothing(monkeypatch):
    monkeypatch.setattr(settings, "SHORTS_CTA", "", raising=False)
    plain = format_shorts_metadata("Title", "Body", ["#Shorts"])
    assert "subscribe" not in plain["description"].lower()

    monkeypatch.setattr(settings, "SHORTS_CTA", "New clips twice a day.", raising=False)
    with_cta = format_shorts_metadata("Title", "Body", ["#Shorts"])
    assert "New clips twice a day." in with_cta["description"]


# ------------------------------------------------- the script a published title is written in
# Clip 233 went live on 2026-09-17 with a Chinese title on a Greek/English channel. Nothing
# in the pipeline checked the script of a title, description or hashtag before the upload
# call, so a Chinese roll from a free-tier model reached the channel and nothing would have
# stopped a second one. The gate is at the publish boundary - the one place every clip
# crosses - not at the generator, because the boundary is what decides what leaves this
# machine, and an operator can edit a clip and click again there.
def _blocked_clip(db, *, title, description="A description", hashtags=None):
    clip = db.query(Clip).filter(Clip.youtube_video_id.is_(None)).first()
    clip.title = title
    clip.description = description
    clip.hashtags = hashtags if hashtags is not None else ["#Shorts"]
    db.commit()
    return clip


def test_a_clip_with_a_non_latin_title_never_reaches_youtube(db, monkeypatch):
    """The gate is the boundary, so it stops the upload call rather than the generator."""
    _job(db, queued=1)
    _blocked_clip(db, title="当他拿出这盘菌子的时候全场都愣住了")
    uploader = _Recorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 0
    assert uploader.calls == [], "the upload call must never see a foreign-script title"

    waiting = db.query(Clip).filter(Clip.youtube_video_id.is_(None)).one()
    assert waiting.publish_due_at > NOW, "the clip stays queued, not retired"
    assert waiting.upload_retry_count == 0, "a title the generator got wrong is not a clip fault"


def test_a_greek_title_is_not_blocked_by_the_gate(db):
    """The gate checks script, not language, so Greek copy still ships."""
    _job(db, queued=1)
    _blocked_clip(db, title="Ο καλύτερος τρόπος να μάθεις κάτι")
    uploader = _Recorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 1
    assert len(uploader.calls) == 1


def test_the_gate_blocks_the_script_in_any_field_it_reaches(db):
    """A foreign hashtag is the same defect as a foreign title: the channel reads it."""
    _job(db, queued=1)
    _blocked_clip(db, title="A title", hashtags=["#Shorts", "搞笑"])
    uploader = _Recorder()

    assert worker.publish_queued_clips(db, now=NOW, uploader=uploader) == 0
    assert uploader.calls == []


def test_the_gate_reports_what_was_blocked_and_how_to_fix_it(db, monkeypatch):
    """A refusal that names the repair is the one an operator will act on."""
    _job(db, queued=1)
    _blocked_clip(db, title="当他拿出这盘菌子的时候全场都愣住了")
    uploader = _Recorder()
    warnings = []
    monkeypatch.setattr(worker, "job_warning", lambda jid, msg: warnings.append(msg))

    worker.publish_queued_clips(db, now=NOW, uploader=uploader)

    assert warnings, "a held clip is announced"
    held = [w for w in warnings if "held back" in w]
    assert held, warnings
    assert "当他拿出这盘菌子的时候全场都愣住了" in held[0], \
        "the message shows the characters it refused, not just a label"
    assert "edit" in held[0].lower()
