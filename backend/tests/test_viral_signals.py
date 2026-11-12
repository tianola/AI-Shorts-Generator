"""Viral signal improvements: hook strength, emotional intensity, audio dynamics, visual events.

Research (OpusClip, Munch, academic papers) shows these signals predict virality:
1. Hook strength (first 3s) — curiosity gap, concrete claim, visual surprise
2. Emotional intensity — high-arousal emotions (awe, humor, surprise) drive shares
3. Audio dynamics — dB spikes and pitch changes, not just average energy
4. Visual event density — moments where something happens on screen
5. Pacing — no dead air, tight topic transitions
6. Narrative completeness — stands alone without prior context

These tests pin each new signal before implementation.
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import settings


@pytest.fixture
def pin_clip_range(monkeypatch):
    monkeypatch.setattr(settings, "MIN_CLIP_SECONDS", 20, raising=False)
    monkeypatch.setattr(settings, "MAX_CLIP_SECONDS", 30, raising=False)


def test_hook_strength_scores_first_three_seconds():
    """A clip starting with a curiosity gap scores higher on hook_strength."""
    from services.clip_selector import _hook_strength_score

    # Strong hook: concrete claim + curiosity gap
    strong = "This is the secret they don't want you to know"
    # Weak hook: generic opening
    weak = "So today we're going to talk about something"

    strong_score = _hook_strength_score(strong)
    weak_score = _hook_strength_score(weak)

    assert strong_score > weak_score, f"Strong hook ({strong_score}) should beat weak ({weak_score})"
    assert 0.0 <= strong_score <= 1.0
    assert 0.0 <= weak_score <= 1.0


def test_emotional_intensity_detects_high_arousal_emotions():
    """High-arousal emotions (awe, humor, surprise) score higher than neutral."""
    from services.clip_selector import _emotional_intensity_score

    # High arousal: surprise + humor
    high_arousal = "I can't believe he just did that! That's insane!"
    # Low arousal: neutral statement
    low_arousal = "The weather is nice today"

    high_score = _emotional_intensity_score(high_arousal)
    low_score = _emotional_intensity_score(low_arousal)

    assert high_score > low_score, f"High arousal ({high_score}) should beat low ({low_score})"


def test_audio_dynamics_score_uses_spikes_not_average():
    """Audio dynamics should reward dB spikes, not just consistent energy."""
    from services.clip_selector import _audio_dynamics_score

    # Spike: one segment much louder than others
    spike_energies = [0.3, 0.3, 0.3, 0.9, 0.3, 0.3]
    # Flat: all same energy
    flat_energies = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5]

    spike_score = _audio_dynamics_score(spike_energies)
    flat_score = _audio_dynamics_score(flat_energies)

    assert spike_score > flat_score, f"Spike ({spike_score}) should beat flat ({flat_score})"


def test_visual_event_density_boosts_clip_score():
    """Clips with more visual events should score higher."""
    from services.clip_selector import _visual_event_density_score

    # High density: 5 events in 30 seconds
    high_density = _visual_event_density_score(event_count=5, duration=30.0)
    # Low density: 1 event in 30 seconds
    low_density = _visual_event_density_score(event_count=1, duration=30.0)
    # No events
    no_events = _visual_event_density_score(event_count=0, duration=30.0)

    assert high_density > low_density > no_events


def test_pacing_score_penalizes_dead_air():
    """Clips with gaps of silence score lower on pacing."""
    from services.clip_selector import _pacing_score

    # Tight: segments cover the whole clip
    tight_segments = [
        {"start": 0.0, "end": 10.0, "text": "word " * 20},
        {"start": 10.0, "end": 20.0, "text": "word " * 20},
        {"start": 20.0, "end": 30.0, "text": "word " * 20},
    ]
    # Gappy: 5-second silence in the middle
    gappy_segments = [
        {"start": 0.0, "end": 10.0, "text": "word " * 20},
        # 5-second gap
        {"start": 15.0, "end": 25.0, "text": "word " * 20},
    ]

    tight_score = _pacing_score(tight_segments, clip_start=0.0, clip_end=30.0)
    gappy_score = _pacing_score(gappy_segments, clip_start=0.0, clip_end=30.0)

    assert tight_score > gappy_score, f"Tight ({tight_score}) should beat gappy ({gappy_score})"


def test_narrative_completeness_checks_standalone_quality():
    """A clip that stands alone scores higher than one needing context."""
    from services.clip_selector import _narrative_completeness_score

    # Self-contained: has subject and conclusion
    complete = "The CEO announced the merger. Stock prices jumped 40%."
    # Incomplete: starts mid-thought, no resolution
    incomplete = "and then he said that we should probably"

    complete_score = _narrative_completeness_score(complete)
    incomplete_score = _narrative_completeness_score(incomplete)

    assert complete_score > incomplete_score


@pytest.mark.asyncio
async def test_composite_score_includes_new_signals(pin_clip_range, monkeypatch):
    """The composite score now weighs hook, emotion, dynamics, visual, pacing, narrative."""
    from services.clip_selector import _composite_score_v2

    clip = {
        "virality_score": 0.85,  # LLM raw
        "audio_energy_score": 0.6,
        "keyword_score": 0.5,
        "semantic_density": 0.7,
        "structural_score": 0.6,
        "hook_strength": 0.9,  # NEW
        "emotional_intensity": 0.8,  # NEW
        "audio_dynamics": 0.7,  # NEW
        "visual_event_density": 0.6,  # NEW
        "pacing_score": 0.8,  # NEW
        "narrative_completeness": 0.7,  # NEW
    }

    score = _composite_score_v2(clip)

    assert 0.0 <= score <= 1.0
    # The new score should be different from the old formula
    old_score = (
        0.85 * 0.40 + 0.6 * 0.25 + 0.5 * 0.20 + 0.7 * 0.10 + 0.6 * 0.05
    )
    assert score != old_score, "New composite should use different weights"


@pytest.mark.asyncio
async def test_end_to_end_clip_selection_uses_new_signals(pin_clip_range, monkeypatch):
    """Full pipeline: LLM + new signals produce ranked clips."""
    from services import clip_selector

    segments = [
        {"start": 0.0, "end": 10.0, "text": "This is the secret they never told you", "audio_energy": 0.8},
        {"start": 10.0, "end": 20.0, "text": "Wait until you see what happens next", "audio_energy": 0.9},
        {"start": 20.0, "end": 30.0, "text": "The results were absolutely insane", "audio_energy": 0.7},
        {"start": 30.0, "end": 40.0, "text": "word " * 10, "audio_energy": 0.3},  # boring segment
        {"start": 40.0, "end": 50.0, "text": "and then he said that we should probably", "audio_energy": 0.4},  # incomplete
    ]
    segments_file = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
    json.dump(segments, segments_file)
    segments_file.close()

    try:
        llm_clips = {
            "clips": [
                {"start": 0.0, "end": 30.0, "title": "The Secret", "hook": "This is the secret", "virality_score": 0.85},
                {"start": 30.0, "end": 50.0, "title": "Boring Part", "hook": "word word", "virality_score": 0.60},
            ]
        }

        async def fake_llm(*a, **kw):
            return json.dumps(llm_clips)

        async def fake_story(*a, **kw):
            return {"story_type": "reveal", "cta": "Follow for more."}

        monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
        monkeypatch.setattr(clip_selector, "analyze_story_segments", fake_story)

        result = await clip_selector.select_clips(segments_file.name, num_clips=2)

        assert len(result) == 2
        # The exciting clip should rank higher
        assert result[0]["start"] == 0.0, "Exciting clip should rank first"
        assert result[1]["start"] == 30.0, "Boring clip should rank second"
        # New signals should be present in output
        assert "hook_strength" in result[0]
        assert "emotional_intensity" in result[0]
    finally:
        os.unlink(segments_file.name)
