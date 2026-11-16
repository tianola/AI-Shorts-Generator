"""
Unit tests for the 3 Virality Pipeline Upgrades:
1. Audio Energy Pre-pass integration in select_clips
2. Rerender with custom thumbnail_template selection
3. Composite Virality Score with real multi-signal breakdown
"""
import pytest
import os
import json
import tempfile
from unittest.mock import patch
from services.clip_selector import (
    _audio_dynamics_score,
    _composite_score,
    _selection_score,
    select_clips,
)


def _run_selection(segments):
    """Drive select_clips with a stubbed LLM over the given segments file."""
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(segments, f)
        temp_path = f.name

    # The LLM returns 1 clip spanning 0-35s
    mock_llm_response = json.dumps({
        "clips": [
            {
                "start": 0.0,
                "end": 35.0,
                "title": "CAUGHT RED-HANDED",
                "hook": "Nobody knows what really happened",
                "virality_score": 0.9,
                "layout": "single_speaker",
                "caption_style": "mrbeast"
            }
        ]
    })

    try:
        with patch("services.clip_selector.analyze_story_segments", return_value={"story_type": "reaction", "cta": "Follow"}), \
             patch("services.clip_selector.call_llm_json", return_value=mock_llm_response):
            import asyncio
            return asyncio.run(select_clips(temp_path, num_clips=1))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


def test_audio_energy_prepass_enrichment_in_composite_score():
    """The audio pre-pass reaches the composite score through the v2 channel.

    Stale-contract history (tracked as #81): this test was written for the v1
    composite, which consumed the pre-pass MEAN (clip["audio_energy_score"],
    25% weight) and cleared 0.7 on these inputs. The v2 refactor moved the
    composite off the mean and onto audio_dynamics — energy VARIATION, 10%
    weight (clip_selector _composite_score_v2) — while the mean is still
    stored as audio_energy_score for the UI breakdown. So the threshold
    assertion went stale, not the enrichment: the pre-pass still moves the
    score, through the renamed channel (now _selection_score, same 10% weight). That is what this pins — a change of
    either channel re-breaks it.
    """
    sample_segments = [
        {"start": 0.0, "end": 15.0, "text": "Nobody knows this secret truth!", "audio_energy": 0.85},
        {"start": 15.0, "end": 35.0, "text": "He got caught red-handed right now.", "audio_energy": 0.90},
    ]
    plain_segments = [
        {k: v for k, v in s.items() if k != "audio_energy"} for s in sample_segments
    ]

    clips = _run_selection(sample_segments)
    assert len(clips) == 1
    clip = clips[0]

    # The pre-pass mean is still stored on the clip for the breakdown UI.
    assert "audio_energy_score" in clip
    assert clip["audio_energy_score"] == pytest.approx(0.875, 0.01)

    # The pre-pass reaches the composite via audio_dynamics (energy variation).
    expected_dynamics = _audio_dynamics_score([0.85, 0.90])
    assert clip["audio_dynamics"] == pytest.approx(expected_dynamics)

    # The ranking score is exactly _selection_score over the stored signals: the
    # model's rating plus the language-independent measurements. The English
    # keyword/hook/emotion terms of _composite_score_v2 no longer rank model picks.
    recomputed = _selection_score(clip)
    assert clip["virality_score"] == pytest.approx(recomputed)

    # And the enrichment genuinely moves the score: the difference against the
    # same clip WITHOUT pre-passed energies equals the audio_dynamics term
    # (10% weight) exactly. No audio in the segments keeps v2 at its neutral
    # 0.5 default.
    plain_clip = _run_selection(plain_segments)[0]
    assert plain_clip["audio_dynamics"] == 0.5
    assert clip["virality_score"] - plain_clip["virality_score"] == pytest.approx(
        0.10 * (expected_dynamics - 0.5), abs=0.001
    )
    assert 0.0 < clip["virality_score"] <= 1.0


def test_rerender_clip_request_schema_supports_thumbnail_template():
    """Test that RerenderClipRequest accepts thumbnail_template integer."""
    from api.clips import RerenderClipRequest
    req = RerenderClipRequest(caption_style="cyber_neon", layout_mode="split_screen", thumbnail_template=2)
    assert req.thumbnail_template == 2
    assert req.caption_style == "cyber_neon"


def test_composite_score_weights_breakdown():
    """Test exact mathematical weights: LLM 40%, Audio 25%, Keywords 20%, Density 10%, Structure 5%."""
    clip = {
        "virality_score": 1.0,      # 1.0 * 0.40 = 0.40
        "audio_energy_score": 1.0,  # 1.0 * 0.25 = 0.25
        "keyword_score": 1.0,       # 1.0 * 0.20 = 0.20
        "semantic_density": 1.0,    # 1.0 * 0.10 = 0.10
        "structural_score": 1.0,    # 1.0 * 0.05 = 0.05
    }
    # Sum = 1.0
    assert _composite_score(clip) == 1.0

    clip_half = {
        "virality_score": 0.5,
        "audio_energy_score": 0.5,
        "keyword_score": 0.5,
        "semantic_density": 0.5,
        "structural_score": 0.5,
    }
    assert _composite_score(clip_half) == 0.5
