"""The headline card must not swallow words off the edge.

Job 106 clip #0 published as "Spaced Practice Beats a 5-Hour Cram" but the frame
read "SPACED PRACTICE / BEATS A 5-HOUR" - "CRAM" fell onto a third line, the
wrap kept two, and nothing said so. A cut the viewer cannot see is a lie on the
thumbnail of the video.
"""
import pytest

from services.motion_title import _wrap_title


def test_a_title_that_fits_three_lines_keeps_every_word():
    lines = _wrap_title("Spaced Practice Beats a 5-Hour Cram", max_chars=18, max_lines=3)
    assert " ".join(lines).split() == ["SPACED", "PRACTICE", "BEATS", "A", "5-HOUR", "CRAM"]


def test_a_title_that_cannot_fit_says_so_instead_of_losing_words():
    long_title = "Interleaving Practice Makes Your Brain Rebuild Every Single Schema"
    lines = _wrap_title(long_title, max_chars=18, max_lines=2)

    assert len(lines) == 2
    assert lines[-1].endswith("..."), (
        f"words past the line budget vanished silently: {lines}"
    )


def test_the_headline_card_uses_the_three_line_budget():
    """The call site, not just the helper: 2 lines is what dropped 'CRAM'."""
    import inspect

    from services import motion_title

    src = inspect.getsource(motion_title.render_title_frame)
    assert "max_lines=3" in src, "the video headline card still caps at two lines"


@pytest.mark.parametrize("title", ["", "   "])
def test_an_empty_title_still_renders_something(title):
    assert _wrap_title(title, max_chars=18, max_lines=3) == ["VIRAL MOMENT"]
