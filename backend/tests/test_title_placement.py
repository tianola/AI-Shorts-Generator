"""Title card placement: high by default, lower only when the high strip is busy.

Run from backend/:  python -m pytest tests/test_title_placement.py -q
"""
import numpy as np
import pytest

from services import title_placement as tp
from services.visual_popup import build_popup_filtergraph_fragment


def _frame(busy_rows=()):
    """A flat grey frame with a noisy (busy) band over each (top_frac, bottom_frac) given."""
    rng = np.random.default_rng(0)
    frame = np.full((tp.SAMPLE_H, tp.SAMPLE_W), 128, dtype=np.uint8)
    for top, bottom in busy_rows:
        a, b = int(top * tp.SAMPLE_H), int(bottom * tp.SAMPLE_H)
        frame[a:b] = rng.integers(0, 255, size=(b - a, tp.SAMPLE_W), dtype=np.uint8)
    return frame


def test_flat_strip_scores_zero_and_noise_scores_high():
    flat = tp.strip_busyness([_frame()], 0.17, 0.15)
    noisy = tp.strip_busyness([_frame([(0.05, 0.3)])], 0.17, 0.15)
    assert flat == 0.0
    assert noisy > 50


def test_default_kept_when_its_strip_is_plain():
    scores = {0.17: 2.0, 0.3333: 0.5, 0.45: 0.1}
    assert tp.choose_center(scores, 0.17, [0.3333, 0.45]) == 0.17


def test_moves_off_a_busy_strip_to_the_emptiest_clear_fallback():
    frames = [_frame([(0.05, 0.3)])]  # something sits high on the frame
    scores = {f: tp.strip_busyness(frames, f, 0.15) for f in (0.17, 0.3333, 0.45)}
    assert tp.choose_center(scores, 0.17, [0.3333, 0.45]) in (0.3333, 0.45)


def test_stays_high_when_everything_is_busy():
    """A screen recording that is text all over keeps the title high instead of hopping."""
    frames = [_frame([(0.0, 1.0)])]
    scores = {f: tp.strip_busyness(frames, f, 0.15) for f in (0.17, 0.3333, 0.45)}
    assert tp.choose_center(scores, 0.17, [0.3333, 0.45]) == 0.17


def test_disabled_or_failed_sampling_keeps_the_default(monkeypatch):
    assert tp.pick_title_center("x.mp4", 0, 10, "[0:v]null[vsingle]", 0.15, 0.17,
                                [0.3333], enabled=False) == 0.17

    def boom(*a, **k):
        raise RuntimeError("no ffmpeg")
    monkeypatch.setattr(tp, "sample_frames", boom)
    assert tp.pick_title_center("x.mp4", 0, 10, "[0:v]null[vsingle]", 0.15, 0.17,
                                [0.3333]) == 0.17


def test_parse_fracs_skips_junk_and_out_of_range():
    assert tp.parse_fracs("0.3333, x, 0.45, 2") == [0.3333, 0.45]


def test_sampling_graph_splits_a_source_used_twice():
    frag = "[0:v]scale=1080:-2[a];[0:v]boxblur=10[b];[b][a]overlay[vsingle]"
    graph = tp._frag_for_sampling(frag)
    assert graph.startswith("[0:v]split=2[tpsplit0][tpsplit1];")
    assert graph.endswith("overlay[tp_v]")
    assert "[vsingle]" not in graph


@pytest.mark.parametrize("center", [0.17, 0.3333, 0.45, 0.6])
def test_popup_never_overlaps_the_title(center):
    title_h, popup_h = 300, 430
    top = tp.popup_top_below_title(center, title_h, popup_height_px=popup_h)
    title_top = center * 1920 - title_h / 2
    title_bottom = center * 1920 + title_h / 2
    assert top + popup_h <= title_top or top >= title_bottom
    assert top >= 150  # clear of the platform's top bar


def test_popup_goes_below_a_high_title():
    assert tp.popup_top_below_title(0.17, 300) >= 0.17 * 1920 + 150


def test_popup_fragment_uses_the_given_top():
    _, _, overlay = build_popup_filtergraph_fragment(1, 2.0, position="upper_third", top_y=500)
    assert "y='500+25*" in overlay
    _, _, default = build_popup_filtergraph_fragment(1, 2.0, position="upper_third")
    assert "y='290+25*" in default
