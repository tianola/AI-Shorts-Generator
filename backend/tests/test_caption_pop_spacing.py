"""The active caption word pops to pop_scale; it must not cover its neighbours.

Job 112 rendered "WASVENTURE CAPITAL" and "ANDTHEREFORE," because every gap was
a fixed 1.5 spaces while a long active word grew by far more than that.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.captions import PRESETS, _get_caption_font, create_caption_block


def _words(*texts):
    return [{"word": t, "start": i * 0.4, "end": i * 0.4 + 0.35} for i, t in enumerate(texts)]


def _centres(block):
    """x centre of each word's active (layer 1) event, in word order."""
    return [float(x) for x in re.findall(r"Dialogue: 1,[^\n]*?\\pos\(([\d.]+),", block)]


def test_a_popped_long_word_clears_both_neighbours():
    style = PRESETS["mrbeast"]
    texts = ["WAS", "VENTURE", "CAPITAL."]
    block = create_caption_block(_words(*texts), style)
    centres = _centres(block)
    font = _get_caption_font(style)
    widths = [font.getlength(t) for t in texts]
    fs = float(re.search(r"\\fs(\d+)", block).group(1))
    # The block may be shrunk to fit; scale the measured widths the same way.
    from services.captions import _ass_font_size
    scale = fs / _ass_font_size(style)
    widths = [w * scale for w in widths]
    peak = style.pop_scale / 100.0

    left_edge_popped = centres[1] - widths[1] * peak / 2
    right_edge_popped = centres[1] + widths[1] * peak / 2
    assert left_edge_popped > centres[0] + widths[0] / 2
    assert right_edge_popped < centres[2] - widths[2] / 2


def test_long_words_move_to_the_next_group_instead_of_shrinking_the_line():
    """Job 113 drew "VALENTINE INITIALLY PASSES." at about half size."""
    from services.captions import MIN_GROUP_FIT, _ass_font_size, _group_words_by_natural_phrasing

    style = PRESETS["mrbeast"]
    texts = "Valentine initially passes on the computer they're working on".split()
    words = [{"word": t, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, t in enumerate(texts)]
    groups = _group_words_by_natural_phrasing(words, max_group_size=3, style=style)
    assert [w["word"] for g in groups for w in g] == texts
    full = _ass_font_size(style)
    for group in groups:
        fs = int(re.search(r"\\fs(\d+)", create_caption_block(group, style)).group(1))
        if len(group) > 1:
            assert fs >= int(full * MIN_GROUP_FIT) - 1, [w["word"] for w in group]


def test_popped_end_words_stay_inside_the_frame():
    style = PRESETS["mrbeast"]
    texts = ["GOVERNMENTS", "AND", "REGULATION"]
    block = create_caption_block(_words(*texts), style)
    centres = _centres(block)
    font = _get_caption_font(style)
    fs = float(re.search(r"\\fs(\d+)", block).group(1))
    from services.captions import _ass_font_size
    scale = fs / _ass_font_size(style)
    peak = style.pop_scale / 100.0
    first = font.getlength(texts[0]) * scale * peak / 2
    last = font.getlength(texts[-1]) * scale * peak / 2
    assert centres[0] - first >= 0
    assert centres[-1] + last <= 1080
