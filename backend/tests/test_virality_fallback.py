"""The metadata stage must never silently rewrite a clip's score to a constant.

compute_composite_virality can raise (a corrupt vertical mp4, an OpenCV
failure, a missing ffmpeg backend). worker.py used to swallow that behind a
bare `except Exception` and store a hardcoded 85.0 — which is *higher* than
the real computed range (~60-70), so a crashing clip outranked a scored one
and the failure left no trace in the log. resolve_virality_stats is the
observable, honest wrapper: passthrough on success, selector-derived fallback
on failure, always logged.
"""

from services import virality_detector


def test_resolve_virality_stats_passes_through_on_success(monkeypatch):
    real = {"virality_score_100": 63.4, "virality_score": 0.634, "tier": "Standard Clip"}
    monkeypatch.setattr(virality_detector, "compute_composite_virality",
                        lambda *a, **k: dict(real))
    out = virality_detector.resolve_virality_stats(0.8, "hello", "clip.mp4")
    assert out["virality_score_100"] == 63.4
    assert out.get("fallback") is not True


def test_resolve_virality_stats_falls_back_to_selector_score_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("cv2 could not read the vertical clip")
    monkeypatch.setattr(virality_detector, "compute_composite_virality", boom)
    out = virality_detector.resolve_virality_stats(0.67, "hello", "clip.mp4")
    # 0.67 (the selector's own composite) scaled to 100, NOT the old constant 85.
    assert out["virality_score_100"] == 67.0
    assert out["fallback"] is True


def test_resolve_virality_stats_fallback_never_uses_the_inverted_85(monkeypatch):
    def boom(*a, **k):
        raise ValueError("bad")
    monkeypatch.setattr(virality_detector, "compute_composite_virality", boom)
    # A mediocre selector score must not be inflated to 85 on failure.
    out = virality_detector.resolve_virality_stats(0.55, "hello", "clip.mp4")
    assert out["virality_score_100"] < 85.0
    assert out["virality_score_100"] == 55.0


def test_resolve_virality_stats_falls_back_to_neutral_when_no_selector_score(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad")
    monkeypatch.setattr(virality_detector, "compute_composite_virality", boom)
    out = virality_detector.resolve_virality_stats(None, "hello", "clip.mp4")
    # No signal at all -> the neutral base (0.8) scaled, still derived not the 85 literal.
    assert out["virality_score_100"] == 80.0
    assert out["fallback"] is True
