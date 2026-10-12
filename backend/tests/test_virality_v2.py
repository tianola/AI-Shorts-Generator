from services.virality_detector import compute_composite_virality

def test_virality_composite_scoring():
    stats = compute_composite_virality(
        llm_score=0.92,
        transcript="This secret will literally make you a millionaire in 30 days",
        video_path="nonexistent.mp4"
    )
    assert "virality_score_100" in stats
    assert 50.0 <= stats["virality_score_100"] <= 99.0
    assert "virality_explanation" in stats
    assert "visual_intensity" in stats
