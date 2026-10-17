"""Tests for temporal event localization."""
import json
import os
import pytest
from unittest.mock import patch, MagicMock

from services.temporal_localization import (
    _track_objects,
    _track_person_count,
    _align_with_transcript,
    format_events_for_prompt,
    events_for_clip_window,
)


class TestTrackObjects:
    def test_object_appear_after_persistent_samples(self):
        samples = [
            (0.0, []),
            (2.0, []),
            (4.0, [{"name": "laptop", "cls": 6, "conf": 0.9, "box": (0, 0, 100, 100)}]),
            (6.0, [{"name": "laptop", "cls": 6, "conf": 0.9, "box": (0, 0, 100, 100)}]),
        ]
        events = _track_objects(samples)
        appear_events = [e for e in events if e["type"] == "object_appear"]
        assert len(appear_events) == 1
        assert appear_events[0]["name"] == "laptop"
        assert appear_events[0]["t"] == 6.0

    def test_single_sample_does_not_trigger(self):
        samples = [
            (0.0, []),
            (2.0, [{"name": "cup", "cls": 41, "conf": 0.8, "box": (0, 0, 50, 50)}]),
            (4.0, []),
        ]
        events = _track_objects(samples)
        appear_events = [e for e in events if e["type"] == "object_appear"]
        assert len(appear_events) == 0

    def test_object_disappear_after_persistent_absence(self):
        samples = [
            (0.0, [{"name": "phone", "cls": 67, "conf": 0.9, "box": (0, 0, 50, 50)}]),
            (2.0, [{"name": "phone", "cls": 67, "conf": 0.9, "box": (0, 0, 50, 50)}]),
            (4.0, []),
            (6.0, []),
        ]
        events = _track_objects(samples)
        disappear_events = [e for e in events if e["type"] == "object_disappear"]
        assert len(disappear_events) == 1
        assert disappear_events[0]["name"] == "phone"


class TestTrackPersonCount:
    def test_person_enter(self):
        samples = [
            (0.0, [{"cls": 0, "name": "person", "conf": 0.9, "box": (0, 0, 100, 200)}]),
            (2.0, [
                {"cls": 0, "name": "person", "conf": 0.9, "box": (0, 0, 100, 200)},
                {"cls": 0, "name": "person", "conf": 0.8, "box": (200, 0, 100, 200)},
            ]),
        ]
        events = _track_person_count(samples)
        enter_events = [e for e in events if e["type"] == "person_enter"]
        assert len(enter_events) == 1
        assert enter_events[0]["count"] == 2

    def test_person_leave(self):
        samples = [
            (0.0, [
                {"cls": 0, "name": "person", "conf": 0.9, "box": (0, 0, 100, 200)},
                {"cls": 0, "name": "person", "conf": 0.8, "box": (200, 0, 100, 200)},
            ]),
            (2.0, [{"cls": 0, "name": "person", "conf": 0.9, "box": (0, 0, 100, 200)}]),
        ]
        events = _track_person_count(samples)
        leave_events = [e for e in events if e["type"] == "person_leave"]
        assert len(leave_events) == 1
        assert leave_events[0]["count"] == 1


class TestAlignWithTranscript:
    def test_boost_when_object_named_in_transcript(self):
        events = [{"t": 10.0, "type": "object_appear", "name": "laptop", "importance": 0.6}]
        segments = [{"start": 9.0, "end": 11.0, "text": "look at this laptop"}]
        aligned = _align_with_transcript(events, segments, window=3.0)
        assert aligned[0]["importance"] == 0.85
        assert aligned[0]["transcript_aligned"] is True

    def test_no_boost_when_not_named(self):
        events = [{"t": 10.0, "type": "object_appear", "name": "laptop", "importance": 0.6}]
        segments = [{"start": 9.0, "end": 11.0, "text": "the weather is nice"}]
        aligned = _align_with_transcript(events, segments, window=3.0)
        assert aligned[0]["importance"] == 0.6
        assert "transcript_aligned" not in aligned[0]


class TestFormatEventsForPrompt:
    def test_format_empty(self):
        assert format_events_for_prompt([]) == ""

    def test_format_includes_event_details(self):
        events = [
            {"t": 5.0, "type": "object_appear", "name": "tv", "importance": 0.6},
            {"t": 30.0, "type": "person_enter", "count": 2, "importance": 0.7},
            {"t": 60.0, "type": "scene_change", "importance": 0.5},
        ]
        text = format_events_for_prompt(events)
        assert "tv" in text
        assert "5.0s" in text
        assert "person enters" in text
        assert "Scene change" in text


class TestEventsForClipWindow:
    def test_filters_to_window(self):
        events = [
            {"t": 5.0, "type": "object_appear", "name": "laptop"},
            {"t": 25.0, "type": "person_enter", "count": 2},
            {"t": 55.0, "type": "scene_change"},
        ]
        filtered = events_for_clip_window(events, 20.0, 40.0)
        assert len(filtered) == 1
        assert filtered[0]["t"] == 25.0
