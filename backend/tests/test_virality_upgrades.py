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
    _composite_score,
    select_clips,
)


def test_audio_energy_prepass_enrichment_in_composite_score():
    """Test that enriched segments with audio_energy update the composite virality score."""
    sample_segments = [
        {"start": 0.0, "end": 15.0, "text": "Nobody knows this secret truth!", "audio_energy": 0.85},
        {"start": 15.0, "end": 35.0, "text": "He got caught red-handed right now.", "audio_energy": 0.90},
    ]
    
    with tempfile.NamedTemporaryFile(mode="w+", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(sample_segments, f)
        temp_path = f.name

    try:
        # Mock the LLM to return 1 clip spanning 0-35s
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
        
        with patch("services.clip_selector.analyze_story_segments", return_value={"story_type": "reaction", "cta": "Follow"}), \
             patch("services.clip_selector.call_llm_json", return_value=mock_llm_response):
            import asyncio
            clips = asyncio.run(select_clips(temp_path, num_clips=1))
            
            assert len(clips) == 1
            clip = clips[0]
            # Audio energy should be average of (0.85 + 0.90) / 2 = 0.875
            assert "audio_energy_score" in clip
            assert clip["audio_energy_score"] == pytest.approx(0.875, 0.01)
            # Virality score should be composite calculation
            assert clip["virality_score"] > 0.7
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


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
