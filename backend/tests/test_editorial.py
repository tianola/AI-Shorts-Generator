"""The editorial review (services/quality/editorial.py): frame cutting, validation of each model
finding, agreement between repeat runs, the review loop, the edit description and the prompt.

The media is a tiny ffmpeg-generated clip (1080x1920, 6 s) built once per module. No test calls a
real model: every run comes from a fake caller(prompt, frames).
"""
import asyncio
import json
import os
import shutil
import subprocess
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import pytest

from services.quality import editorial as ed
from services.quality.models import Evidence, Finding

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="requires ffmpeg and ffprobe on PATH")
TIMEOUT = 120


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not HAS_FFMPEG:
        pytest.skip("requires ffmpeg and ffprobe on PATH")
    path = str(tmp_path_factory.mktemp("editorial_media") / "final.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc=size=1080x1920:rate=30:duration=6", "-pix_fmt", "yuv420p", path],
        check=True, timeout=TIMEOUT, capture_output=True)
    return path


@pytest.fixture
def frames(clip, tmp_path):
    """Five real frames cut from the clip, as extract_frames returns them."""
    return ed.extract_frames(clip, 6.0, str(tmp_path / "frames"), 5)


def _item(frame=1, category="captions", observed="caption cut off at the bottom", confidence=0.8, **extra):
    item = {"category": category, "frame": frame, "issue": "caption cut off",
            "explanation": "the caption runs past the edge", "observed": observed,
            "confidence": confidence, "recommendation": "move the caption up"}
    item.update(extra)
    return item


def _raw(*items):
    return json.dumps({"findings": list(items)})


def _finding(frame_path, category="visual", confidence=0.7, fid="x"):
    return Finding(
        id=fid, category=category, severity="info", source="editorial", issue="i", explanation="e",
        confidence=confidence, evidence=Evidence(kind="frame", ref=frame_path, value="seen"),
    )


# ---------------------------------------------------------------------------
# (a) extract_frames
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_five_frames_are_cut_from_a_six_second_clip(clip, tmp_path):
    frames = ed.extract_frames(clip, 6.0, str(tmp_path / "f"), 5)
    assert len(frames) == 5
    for frame in frames:
        assert os.path.isfile(frame["path"])
        assert os.path.getsize(frame["path"]) > 0
        assert frame["path"].endswith(".jpg")
    times = [f["time"] for f in frames]
    assert times == sorted(times) and len(set(times)) == 5


@needs_ffmpeg
def test_count_three_returns_three_frames(clip, tmp_path):
    frames = ed.extract_frames(clip, 6.0, str(tmp_path / "f"), 3)
    assert len(frames) == 3


def test_a_missing_input_file_returns_no_frames(tmp_path):
    if not HAS_FFMPEG:
        pytest.skip("requires ffmpeg and ffprobe on PATH")
    assert ed.extract_frames(str(tmp_path / "missing.mp4"), 6.0, str(tmp_path / "f"), 5) == []


# ---------------------------------------------------------------------------
# (b) parse_findings
# ---------------------------------------------------------------------------
@needs_ffmpeg
def test_a_valid_finding_becomes_an_editorial_info_finding_tied_to_its_frame(frames):
    out = ed.parse_findings(_raw(_item(frame=2)), frames)
    assert len(out) == 1
    f = out[0]
    assert isinstance(f, Finding)
    assert f.source == "editorial"
    assert f.severity == "info"
    assert f.evidence.kind == "frame"
    assert f.evidence.ref == frames[1]["path"]
    assert f.timestamp_final == frames[1]["time"]


@needs_ffmpeg
def test_a_finding_for_a_frame_that_does_not_exist_is_dropped(frames):
    assert ed.parse_findings(_raw(_item(frame=9)), frames) == []


@needs_ffmpeg
def test_a_category_outside_the_allowed_set_is_dropped(frames):
    assert ed.parse_findings(_raw(_item(category="audio")), frames) == []


@needs_ffmpeg
def test_a_finding_with_no_observation_is_dropped(frames):
    assert ed.parse_findings(_raw(_item(observed="   ")), frames) == []


@needs_ffmpeg
def test_a_finding_whose_frame_file_was_deleted_is_dropped(frames, tmp_path):
    gone = [{"index": 1, "time": 0.5, "path": str(tmp_path / "deleted.jpg")}]
    assert ed.parse_findings(_raw(_item(frame=1)), gone) == []


@needs_ffmpeg
@pytest.mark.parametrize("bad", ["high", None, [0.5]])
def test_a_confidence_that_is_not_a_number_is_dropped(frames, bad):
    assert ed.parse_findings(_raw(_item(confidence=bad)), frames) == []


@needs_ffmpeg
def test_confidence_is_clamped_to_zero_and_one(frames):
    high = ed.parse_findings(_raw(_item(confidence=1.7)), frames)
    low = ed.parse_findings(_raw(_item(confidence=-0.3)), frames)
    assert high[0].confidence == 1.0
    assert low[0].confidence == 0.0


@needs_ffmpeg
def test_at_most_eight_findings_come_from_one_run(frames):
    items = [_item(frame=(n % 5) + 1, observed=f"observation {n}") for n in range(12)]
    out = ed.parse_findings(_raw(*items), frames)
    assert len(out) == ed.MAX_FINDINGS_PER_RUN == 8


# ---------------------------------------------------------------------------
# (c) aggregate_runs
# ---------------------------------------------------------------------------
def test_the_same_category_and_frame_in_two_of_three_runs_is_stable():
    a = _finding("/clip/frame_2.jpg", "captions")
    b = _finding("/clip/frame_2.jpg", "captions", fid="y")
    merged, agreement = ed.aggregate_runs([[a], [b], []])
    assert len(merged) == 1
    assert merged[0].stable is True
    assert merged[0].runs_seen == 2


def test_seen_in_only_one_of_three_runs_is_not_stable():
    merged, agreement = ed.aggregate_runs([[_finding("/clip/frame_2.jpg")], [], []])
    assert len(merged) == 1
    assert merged[0].stable is False
    assert merged[0].runs_seen == 1


def test_a_run_repeating_the_same_key_counts_once():
    a = _finding("/clip/frame_2.jpg", "captions")
    merged, _ = ed.aggregate_runs([[a, a], [], []])
    assert merged[0].runs_seen == 1
    assert merged[0].stable is False


def test_agreement_is_the_share_of_distinct_problems_that_repeat():
    a = _finding("/clip/frame_1.jpg", "captions")
    b = _finding("/clip/frame_3.jpg", "visual")
    merged, agreement = ed.aggregate_runs([[a, b], [a], []])
    assert len(merged) == 2
    assert sum(1 for f in merged if f.stable) == 1
    assert agreement == 0.5


def test_three_empty_runs_agree_completely():
    assert ed.aggregate_runs([[], [], []]) == ([], 1.0)


def test_a_single_run_has_no_agreement_rate():
    merged, agreement = ed.aggregate_runs([[_finding("/clip/frame_1.jpg")]])
    assert len(merged) == 1
    assert agreement is None


def test_no_runs_gives_nothing_and_no_rate():
    assert ed.aggregate_runs([]) == ([], None)


# ---------------------------------------------------------------------------
# (d) evaluate_editorial with a fake caller
# ---------------------------------------------------------------------------
def _good_caller(frames_seen=None):
    async def caller(prompt, frames):
        if frames_seen is not None:
            frames_seen.append(frames)
        return _raw(_item(frame=1))
    return caller


@needs_ffmpeg
def test_a_full_review_writes_editorial_json_and_is_advisory(clip, tmp_path):
    clip_dir = str(tmp_path / "clip")
    os.makedirs(clip_dir)
    report = asyncio.run(ed.evaluate_editorial(
        clip, clip_dir, duration=6.0, title="He missed the ending", caller=_good_caller()))

    assert report is not None
    assert report["advisory"] is True
    assert report["runs_completed"] == 3
    assert report["run_errors"] == []
    assert len(report["frames"]) == 5
    with open(os.path.join(clip_dir, "editorial.json"), encoding="utf-8") as handle:
        assert json.load(handle)["runs_completed"] == 3


@needs_ffmpeg
def test_a_caller_that_fails_every_run_gives_none(clip, tmp_path):
    clip_dir = str(tmp_path / "clip")
    os.makedirs(clip_dir)

    async def caller(prompt, frames):
        raise RuntimeError("vision model unavailable")

    assert asyncio.run(ed.evaluate_editorial(clip, clip_dir, duration=6.0, caller=caller)) is None


@needs_ffmpeg
def test_a_caller_that_fails_on_one_run_is_recorded_and_the_rest_count(clip, tmp_path):
    clip_dir = str(tmp_path / "clip")
    os.makedirs(clip_dir)
    calls = {"n": 0}

    async def caller(prompt, frames):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError("second run timed out")
        return _raw(_item(frame=1))

    report = asyncio.run(ed.evaluate_editorial(clip, clip_dir, duration=6.0, caller=caller))
    assert report["runs_completed"] == 2
    assert len(report["run_errors"]) == 1
    assert "timed out" in report["run_errors"][0]


def test_fewer_than_three_frames_gives_none_without_calling_the_model(tmp_path):
    called = []

    async def caller(prompt, frames):
        called.append(True)
        return _raw()

    result = asyncio.run(ed.evaluate_editorial(
        str(tmp_path / "missing.mp4"), str(tmp_path / "clip"), duration=6.0, caller=caller))
    assert result is None
    assert called == []


@needs_ffmpeg
def test_findings_only_appear_in_the_returned_dict_and_never_set_a_status(clip, tmp_path):
    clip_dir = str(tmp_path / "clip")
    os.makedirs(clip_dir)
    report = asyncio.run(ed.evaluate_editorial(clip, clip_dir, duration=6.0, caller=_good_caller()))
    assert report["findings"], "the fake run should have produced a finding"
    assert "status" not in report
    assert all(f["source"] == "editorial" for f in report["findings"])


# ---------------------------------------------------------------------------
# (e) describe_edits
# ---------------------------------------------------------------------------
def test_describe_edits_counts_each_action_kind(tmp_path):
    timeline = tmp_path / "edit_timeline.json"
    timeline.write_text(json.dumps({
        "profile": "gaming",
        "events": [
            {"visual_actions": ["zoom_punch", {"kind": "flash"}], "audio_actions": ["sidechain"]},
            {"visual_actions": ["zoom_punch", "shake"], "audio_actions": []},
        ],
    }), encoding="utf-8")
    assert ed.describe_edits(str(timeline)) == \
        "profile gaming; 1x flash, 1x shake, 1x sidechain, 2x zoom_punch"


def test_describe_edits_with_a_missing_file_is_empty(tmp_path):
    assert ed.describe_edits(str(tmp_path / "no_timeline.json")) == ""


# ---------------------------------------------------------------------------
# (f) SCHEMA sanity
# ---------------------------------------------------------------------------
def test_schema_lists_every_required_key_as_a_property_and_forbids_extras():
    top = ed.SCHEMA
    assert top["additionalProperties"] is False
    for key in top["required"]:
        assert key in top["properties"]

    item = top["properties"]["findings"]["items"]
    assert item["additionalProperties"] is False
    for key in item["required"]:
        assert key in item["properties"]
    assert set(item["properties"]["category"]["enum"]) == set(ed.EDITORIAL_CATEGORIES)


# ---------------------------------------------------------------------------
# (g) build_prompt
# ---------------------------------------------------------------------------
def test_the_prompt_names_every_frame_and_the_title():
    frames = [{"index": i, "time": float(i), "path": f"/x/frame_{i}.jpg"} for i in range(1, 6)]
    prompt = ed.build_prompt(frames, "He missed the ending", "sports", "2x zoom_punch", "he fell asleep")
    for frame in frames:
        assert f"frame {frame['index']} at {frame['time']}s" in prompt
    assert "He missed the ending" in prompt
