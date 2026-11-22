"""
Tests for the editing timeline package (services/editing):
- signals: sentence splitting, loudness peaks, event detection
- profiles: tone to profile mapping and override rules
- planner: deterministic edit planning, budgets, collision rules
- models: JSON round trip and version gate
- describe: the human-readable explanation
- bridge: scene cuts in a window, and the worker entry point plan_clip_edits

All times are clip-relative seconds. No network, no LLM, no ffmpeg.
"""
import json

import pytest

from services.editing.bridge import plan_clip_edits, scene_cuts_in_window
from services.editing.describe import explain
from services.editing.models import (
    TIMELINE_VERSION, Timeline, timeline_from_json, timeline_to_json,
)
from services.editing.planner import WANTS, build_timeline
from services.editing.profiles import PROFILES, TONE_TO_PROFILE, profile_for_tone
from services.editing.signals import audio_peaks, detect_events, split_sentences


SAMPLE_TEXT = "Listen to this. He NEVER saw it coming! Why would anyone do that? This is the BEST day ever! And then the money came in."
SAMPLE_ENERGY = [0.2] * 30 + [0.9, 0.95] + [0.2] * 20
SAMPLE_DURATION = 14.0
CAMERA_EFFECTS = ("zoom_punch", "shake")


def sample_words(step=0.34, length=0.3):
    return [
        {"word": text, "start": round(i * step, 3), "end": round(i * step + length, 3)}
        for i, text in enumerate(SAMPLE_TEXT.split())
    ]


def sample_timeline(tone="casual_podcast", profile_override=None):
    return build_timeline(
        sample_words(), SAMPLE_DURATION, tone=tone, profile_override=profile_override,
        energy=SAMPLE_ENERGY, energy_step_s=0.25, scene_cuts=[9.0], popup_starts=[6.0],
        emotional_intensity=0.9,
    )


# =====================
# 1. signals.split_sentences
# =====================

def test_split_sentences_on_terminal_punctuation():
    sentences = split_sentences(sample_words())
    assert [s["text"] for s in sentences] == [
        "Listen to this.",
        "He NEVER saw it coming!",
        "Why would anyone do that?",
        "This is the BEST day ever!",
        "And then the money came in.",
    ]


def test_split_sentences_on_long_pause_without_punctuation():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "there", "start": 0.4, "end": 0.7},
        {"word": "friend", "start": 2.0, "end": 2.3},   # gap 1.3 s > 0.7 s
    ]
    assert [s["text"] for s in split_sentences(words)] == ["hello there", "friend"]


def test_split_sentences_keeps_short_pause_together():
    words = [
        {"word": "hello", "start": 0.0, "end": 0.3},
        {"word": "there", "start": 0.4, "end": 0.7},
        {"word": "friend", "start": 1.2, "end": 1.5},   # gap 0.5 s
    ]
    assert [s["text"] for s in split_sentences(words)] == ["hello there friend"]


def test_split_sentences_flags_exclaim_and_question():
    sentences = split_sentences(sample_words())
    by_text = {s["text"]: s for s in sentences}
    assert by_text["He NEVER saw it coming!"]["exclaim"] is True
    assert by_text["Why would anyone do that?"]["question"] is True
    assert by_text["Listen to this."]["exclaim"] is False
    assert by_text["Listen to this."]["question"] is False


def test_split_sentences_flags_caps_for_shouted_words():
    shouted = [
        {"word": "NEVER", "start": 0.0, "end": 0.3},
        {"word": "BEST", "start": 0.4, "end": 0.7},
    ]
    sentence = split_sentences(shouted)[0]
    assert sentence["caps"] is True


def test_split_sentences_plain_sentence_has_no_flags():
    plain = [{"word": "fine", "start": 0.0, "end": 0.3}, {"word": "today.", "start": 0.34, "end": 0.64}]
    sentence = split_sentences(plain)[0]
    assert sentence["caps"] is False
    assert sentence["exclaim"] is False
    assert sentence["question"] is False


# =====================
# 2. signals.audio_peaks
# =====================

def test_audio_peak_found_at_expected_start():
    peaks = audio_peaks(SAMPLE_ENERGY, 0.25)
    assert len(peaks) == 1
    assert peaks[0]["start"] == pytest.approx(7.5)
    assert peaks[0]["level"] == pytest.approx(0.95)


def test_audio_flat_curve_has_no_peaks():
    assert audio_peaks([0.5] * 40, 0.25) == []


def test_audio_fewer_than_four_samples_has_no_peaks():
    assert audio_peaks([0.2, 0.9, 0.2], 0.25) == []


# =====================
# 3. signals.detect_events
# =====================

def test_detect_events_always_starts_with_hook_at_zero():
    events = detect_events(sample_words(), SAMPLE_DURATION)
    hook = events[0]
    assert hook.kind == "hook"
    assert hook.start == 0.0
    assert hook.priority == 100


def test_detect_events_stressed_sentence_becomes_statement():
    kinds = [e.kind for e in detect_events(sample_words(), SAMPLE_DURATION)]
    assert "statement" in kinds


def test_detect_events_popup_start_becomes_popup():
    events = detect_events(sample_words(), SAMPLE_DURATION, popup_starts=[6.0])
    popups = [e for e in events if e.kind == "popup"]
    assert len(popups) == 1
    assert popups[0].start == pytest.approx(6.0)


def test_detect_events_scene_cuts_near_edges_are_ignored():
    events = detect_events(
        sample_words(), SAMPLE_DURATION, scene_cuts=[0.2, 5.0, 13.9],
    )
    cuts = [e for e in events if e.kind == "scene_cut"]
    assert [c.start for c in cuts] == [pytest.approx(5.0)]


def test_detect_events_high_emotion_adds_one_peak_at_loudest_audio_peak():
    events = detect_events(
        sample_words(), SAMPLE_DURATION, energy=SAMPLE_ENERGY, emotional_intensity=0.9,
    )
    peaks = [e for e in events if e.kind == "emotional_peak"]
    assert len(peaks) == 1
    assert peaks[0].start == pytest.approx(7.5)


@pytest.mark.parametrize("emotion", [0.5, 0.79])
def test_detect_events_low_emotion_adds_no_peak(emotion):
    events = detect_events(
        sample_words(), SAMPLE_DURATION, energy=SAMPLE_ENERGY, emotional_intensity=emotion,
    )
    assert not [e for e in events if e.kind == "emotional_peak"]


def test_detect_events_ids_are_sequential_unique_and_in_time_order():
    events = detect_events(
        sample_words(), SAMPLE_DURATION, energy=SAMPLE_ENERGY, scene_cuts=[9.0],
        popup_starts=[6.0], emotional_intensity=0.9,
    )
    assert [e.id for e in events] == [f"event_{n:03d}" for n in range(1, len(events) + 1)]
    assert len({e.id for e in events}) == len(events)
    starts = [e.start for e in events]
    assert starts == sorted(starts)


# =====================
# 4. profiles
# =====================

@pytest.mark.parametrize("tone, expected", sorted(TONE_TO_PROFILE.items()))
def test_profile_for_each_mapped_tone(tone, expected):
    assert profile_for_tone(tone).id == expected


@pytest.mark.parametrize("tone", ["something_unknown", "", None])
def test_unknown_tone_falls_back_to_dynamic(tone):
    assert profile_for_tone(tone).id == "dynamic"


def test_valid_override_wins_over_tone():
    assert profile_for_tone("somber_tragic", "dramatic").id == "dramatic"


def test_invalid_override_is_ignored():
    assert profile_for_tone("humorous_entertainment", "no_such_profile").id == "humorous"


def test_documentary_profile_allows_no_visual_and_no_audio():
    documentary = PROFILES["documentary"]
    assert documentary.allowed_visual == frozenset()
    assert documentary.allowed_audio == frozenset()


# =====================
# 5. planner.build_timeline
# =====================

def test_somber_tone_yields_no_tracks_and_refuses_every_wanted_action():
    timeline = sample_timeline(tone="somber_tragic")
    assert timeline.profile == "documentary"
    assert timeline.visual_tracks == []
    assert timeline.audio_tracks == []

    expected = set()
    for event in timeline.events:
        for action in WANTS[event.kind]["visual"] + WANTS[event.kind]["audio"]:
            expected.add((event.id, action))
    refused = {(s.event_id, s.action) for s in timeline.suppressed}
    assert expected and refused == expected
    assert all("documentary" in s.reason for s in timeline.suppressed)


def test_casual_podcast_plan_follows_dynamic_profile():
    timeline = sample_timeline(tone="casual_podcast")
    profile = PROFILES["dynamic"]
    assert timeline.profile == profile.id

    hook_id = timeline.events[0].id
    assert any(t.kind == "zoom_punch" and t.start == pytest.approx(0.0) and t.event_id == hook_id
               for t in timeline.visual_tracks)

    assert not any(t.kind == "tint_red" for t in timeline.visual_tracks)

    camera = sorted(t.start for t in timeline.visual_tracks if t.kind in CAMERA_EFFECTS)
    for a, b in zip(camera, camera[1:]):
        assert b - a >= profile.min_spacing_s - 1e-9

    assert len(timeline.visual_tracks) <= profile.max_events

    flashes = sorted(t.start for t in timeline.visual_tracks if t.kind == "flash")
    for a, b in zip(flashes, flashes[1:]):
        assert b - a >= profile.flash_spacing_s - 1e-9

    assert all(a.kind in profile.allowed_audio for a in timeline.audio_tracks)

    for kind in {a.kind for a in timeline.audio_tracks}:
        starts = sorted(a.start for a in timeline.audio_tracks if a.kind == kind)
        for a, b in zip(starts, starts[1:]):
            assert b - a >= 3.0 - 1e-9


def test_build_timeline_is_deterministic():
    first = sample_timeline(tone="casual_podcast").model_dump()
    second = sample_timeline(tone="casual_podcast").model_dump()
    assert first == second


def test_dramatic_profile_adds_red_tint_for_strong_emotional_peak():
    timeline = sample_timeline(profile_override="dramatic")
    assert timeline.profile == "dramatic"
    tints = [t for t in timeline.visual_tracks if t.kind == "tint_red"]
    assert len(tints) >= 1
    assert len(tints) <= 2
    peak = next(e for e in timeline.events if e.kind == "emotional_peak")
    assert any(t.event_id == peak.id for t in tints)


def test_visual_tracks_respect_timing_intensity_and_event_links():
    for tone, override in [("casual_podcast", None), (None, "dramatic"), ("humorous_entertainment", None)]:
        timeline = sample_timeline(tone=tone or "", profile_override=override)
        profile = PROFILES[timeline.profile]
        event_ids = {e.id for e in timeline.events}
        for track in timeline.visual_tracks:
            assert track.start < track.end <= timeline.clip_duration
            assert track.intensity <= profile.max_intensity + 1e-9
            assert track.event_id in event_ids


def test_humorous_profile_never_shakes_or_tints():
    timeline = sample_timeline(tone="humorous_entertainment")
    assert timeline.profile == "humorous"
    kinds = {t.kind for t in timeline.visual_tracks}
    assert "shake" not in kinds
    assert "tint_red" not in kinds


# =====================
# 6. models: JSON round trip and version gate
# =====================

def test_timeline_json_round_trip_equals_original():
    original = sample_timeline(tone="casual_podcast")
    restored = timeline_from_json(timeline_to_json(original))
    assert restored is not None
    assert restored == original


def test_timeline_from_json_rejects_unknown_version():
    payload = sample_timeline().model_dump()
    payload["version"] = 99
    assert timeline_from_json(json.dumps(payload)) is None


def test_timeline_from_json_rejects_invalid_text():
    assert timeline_from_json("this is not json {") is None


def test_timeline_version_constant_is_current_version():
    assert sample_timeline().version == TIMELINE_VERSION


# =====================
# 7. describe.explain
# =====================

def test_explain_first_line_names_profile_and_tone():
    timeline = sample_timeline(tone="casual_podcast")
    lines = explain(timeline)
    assert "'dynamic'" in lines[0]
    assert "casual_podcast" in lines[0]


def test_explain_has_one_line_per_event_after_header():
    timeline = sample_timeline(tone="casual_podcast")
    lines = explain(timeline)
    assert len(lines) == 1 + len(timeline.events)


def test_explain_mentions_refused_actions():
    timeline = sample_timeline(tone="somber_tragic")
    lines = explain(timeline)
    assert any("refused" in line for line in lines[1:])


# =====================
# 8. bridge
# =====================

def test_scene_cuts_in_window_are_clip_relative_sorted_and_unique(tmp_path):
    events_file = tmp_path / "temporal_events.json"
    events_file.write_text(json.dumps([
        {"t": 15.0, "type": "scene_cut"},
        {"t": 12.5, "type": "scene_change"},
        {"t": 15.0, "type": "scene_cut"},          # duplicate
        {"t": 14.0, "type": "object_appear"},      # not a scene type
        {"t": 100.0, "type": "scene_cut"},         # outside the window
    ]), encoding="utf-8")
    assert scene_cuts_in_window(str(events_file), 10.0, 24.0) == [2.5, 5.0]


def test_scene_cuts_missing_file_returns_empty(tmp_path):
    assert scene_cuts_in_window(str(tmp_path / "absent.json"), 0.0, 10.0) == []


def test_scene_cuts_corrupt_file_returns_empty(tmp_path):
    events_file = tmp_path / "temporal_events.json"
    events_file.write_text("{not valid json", encoding="utf-8")
    assert scene_cuts_in_window(str(events_file), 0.0, 10.0) == []


def test_plan_clip_edits_writes_readable_timeline(tmp_path):
    clip_dir = tmp_path / "clip"
    clip_dir.mkdir()
    words_file = tmp_path / "words.json"
    words_file.write_text(json.dumps(sample_words()), encoding="utf-8")
    logs = []

    result = plan_clip_edits(
        str(clip_dir), str(words_file), SAMPLE_DURATION, tone="casual_podcast", log=logs.append,
    )

    assert result is not None
    written = clip_dir / "edit_timeline.json"
    assert written.is_file()
    restored = timeline_from_json(written.read_text(encoding="utf-8"))
    assert restored is not None
    assert restored.profile == "dynamic"
    assert len(logs) >= 1


def test_plan_clip_edits_returns_none_when_words_file_missing(tmp_path):
    clip_dir = tmp_path / "clip"
    clip_dir.mkdir()
    result = plan_clip_edits(
        str(clip_dir), str(tmp_path / "no_words.json"), SAMPLE_DURATION, tone="casual_podcast",
    )
    assert result is None


def test_plan_clip_edits_returns_none_when_clip_dir_missing(tmp_path):
    words_file = tmp_path / "words.json"
    words_file.write_text(json.dumps(sample_words()), encoding="utf-8")
    result = plan_clip_edits(
        str(tmp_path / "no_such_clip_dir"), str(words_file), SAMPLE_DURATION, tone="casual_podcast",
    )
    assert result is None
