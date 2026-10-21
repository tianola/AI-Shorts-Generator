"""Titles and tags that reach YouTube must describe the clip.

Two live failures drove this file:

* `clip_selector` answered a total LLM outage with evenly spaced clips titled
  `VIRAL MOMENT {n}` and a fake descending 0.85 score, and those titles were
  published as-is (35 clip rows, jobs 80/81/83 carry them).
* `format_shorts_metadata` padded every upload with a fixed tag cloud
  (Viral, Trending, FYP, ForYou, Storytime, Explore), which is the opposite of
  the niche signal the channel needs, and `MultiPlatformCopy` demanded an exact
  three-platform shape so a slightly different LLM answer threw away all the
  specific copy and fell back to `#shorts #viral #foryou`.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


# ---------------------------------------------------------------------------
# placeholder detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "", "   ", "Viral Moment", "VIRAL MOMENT 2", "Viral Moment #10",
])
def test_placeholder_titles_are_recognised(title):
    from services.metadata import is_placeholder_title

    assert is_placeholder_title(title), f"{title!r} should read as a placeholder"


@pytest.mark.parametrize("title", [
    "Your Brain Is Lying About Confusion",
    "The Strategy Only Top 1% of Learners Use",
])
def test_real_titles_are_not_placeholders(title):
    from services.metadata import is_placeholder_title

    assert not is_placeholder_title(title)


def test_title_derived_from_transcript_text():
    from services.metadata import derive_title_from_text

    text = ("so the real reason you forget things is that you encoded them "
            "passively in the first place")
    title = derive_title_from_text(text)
    assert title and "Viral Moment" not in title
    assert len(title) <= 60
    assert "forget" in title.lower()


# ---------------------------------------------------------------------------
# clip_selector hard fallback
# ---------------------------------------------------------------------------
def test_selector_fallback_titles_come_from_the_transcript(tmp_path):
    """When every LLM backend is down the run must still describe the source."""
    from services import clip_selector

    segments = [
        {"start": 0.0, "end": 20.0, "text": "Confusion is the signal that your brain is rebuilding a schema."},
        {"start": 20.0, "end": 40.0, "text": "Most students reread, and rereading feels like learning but it is not."},
        {"start": 40.0, "end": 60.0, "text": "You should recall the idea with the page closed before you trust it."},
        {"start": 60.0, "end": 80.0, "text": "Sleep is when the fragile trace becomes a stable one."},
    ]
    seg_file = tmp_path / "segments.json"
    seg_file.write_text(json.dumps(segments), encoding="utf-8")

    with patch.object(clip_selector, "analyze_story_segments",
                      new_callable=AsyncMock, return_value={"story_type": "lesson", "segments": []}), \
         patch.object(clip_selector, "call_llm_json",
                      new_callable=AsyncMock, side_effect=RuntimeError("All LLM backends failed")):
        clips = asyncio.run(clip_selector.select_clips(str(seg_file), num_clips=4))

    assert clips, "an LLM outage must not silently produce zero clips"
    for clip in clips:
        assert clip["title"], "a fallback clip needs a title to publish"
        assert "VIRAL MOMENT" not in clip["title"].upper()
        assert clip["virality_score"] < 0.85, "the fake 0.85 fallback score outranks real clips"
    joined = " ".join(c["title"].lower() for c in clips)
    assert any(word in joined for word in ("confusion", "reread", "recall", "sleep")), \
        f"fallback titles carry no word from the transcript: {clips}"


# ---------------------------------------------------------------------------
# platform copy must survive an odd LLM shape
# ---------------------------------------------------------------------------
def test_multiplatform_copy_accepts_the_shape_the_model_actually_returns():
    """Observed live on job 105: title_options instead of title, no instagram."""
    from services.copy_generator import MultiPlatformCopy

    payload = {
        "tiktok": {
            "title_options": ["You'r brain lies about confusion", "Why feeling lost means you learned"],
            "description": "Encoding beats rereading.",
            "hashtags": ["#studytips", "#brainhack"],
        },
        "youtube": {
            "title": "Why Confusion Actually Means You're Learning",
            "description": "A memory scientist explains the one habit that replaces rereading.",
            "hashtags": ["#Shorts", "#LearningTips"],
        },
    }

    copy = MultiPlatformCopy.model_validate(payload)

    assert "Confusion" in copy.youtube.title
    assert copy.tiktok.title, "title_options must still yield a title"
    assert copy.instagram.title, "a missing platform inherits from the ones present"
    assert copy.instagram.hashtags


def test_platform_copy_defaults_instead_of_raising_on_partial_fields():
    from services.copy_generator import PlatformCopy

    copy = PlatformCopy.model_validate({"title": "Only 1% of learners do this"})

    assert copy.title == "Only 1% of learners do this"
    assert isinstance(copy.hashtags, list)
    assert isinstance(copy.description, str)


# ---------------------------------------------------------------------------
# tags that reach YouTube
# ---------------------------------------------------------------------------
def test_no_filler_tag_cloud_in_the_published_metadata():
    from services.youtube_upload import format_shorts_metadata

    meta = format_shorts_metadata(
        title="Your Brain Is Lying About Confusion",
        description="A learning scientist on why rereading fails.",
        hashtags=["#Shorts", "#LearningTips", "#StudyTips", "#BrainHack", "#Memory"],
    )

    lowered = {t.lower() for t in meta["tags"]}
    for filler in ("fyp", "trending", "explore", "storytime", "viral", "youtubeshorts"):
        assert filler not in lowered, f"generic filler tag #{filler} was injected"
    assert "learningtips" in lowered and "brainhack" in lowered
    assert "#Shorts" in meta["title"]


def test_upload_refuses_a_placeholder_title(tmp_path):
    """A placeholder title is not a reason to skip a publish quietly."""
    from services.youtube_upload import upload_clip_to_youtube

    video = tmp_path / "final_video.mp4"
    video.write_bytes(b"not a real video")

    with patch("services.youtube_upload.get_youtube_auth_status") as auth_spy:
        with pytest.raises(ValueError, match="[Vv]iral moment|placeholder"):
            upload_clip_to_youtube(
                video_path=str(video),
                title="VIRAL MOMENT 2",
                description="whatever",
                hashtags=["#Shorts"],
                privacy_status="unlisted",
            )

    assert not auth_spy.called, "the guard must fire before any network work"
