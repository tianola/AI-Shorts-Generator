"""The follow-crop across cuts, and the look of the headline and captions.

Job 111 was a multi-camera podcast: the edit cuts between a single shot of each
speaker. The crop was pinned to one person's track, and a sample without that
track was skipped "to hold through an occlusion" - so for the whole of the other
speaker's shot the crop stayed where the first speaker had been, on a mic arm.
Inside a shot it also glided between positions, so a cut became a slow pan.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import captions, focus_tracking, motion_title  # noqa: E402

SRC_W, SRC_H = 1920, 1080
LEFT = (200, 200, 400, 800)      # a person on the left of the frame
RIGHT = (1300, 200, 400, 800)    # a different shot: a person on the right


def _person(box):
    return {"cls": 0, "name": "person", "conf": 0.9, "box": box}


def _crop_x_at(trajectory, ts):
    """Crop x in force at `ts`: the last keyframe at or before it."""
    current = trajectory[0][1]
    for point in trajectory:
        if point[0] <= ts + 1e-6:
            current = point[1]
    return current


def test_a_cut_to_another_speaker_moves_the_crop_onto_them():
    samples = [(i * 0.5, [_person(LEFT)]) for i in range(6)] + \
              [(3.0 + i * 0.5, [_person(RIGHT)]) for i in range(6)]
    focus = [(i * 0.5, LEFT) for i in range(6)]          # the track ends at the cut

    trajectory = focus_tracking.compute_smooth_yolo_trajectory(
        samples, SRC_W, SRC_H, focus_boxes=focus)

    crop_w = trajectory[0][3]
    right_centre = RIGHT[0] + RIGHT[2] / 2
    x_after_cut = _crop_x_at(trajectory, 4.0)
    assert x_after_cut <= right_centre <= x_after_cut + crop_w, (
        "the crop stayed on the previous shot's position after the cut")


def test_a_cut_is_a_jump_not_a_pan():
    samples = [(i * 0.5, [_person(LEFT)]) for i in range(4)] + \
              [(2.0 + i * 0.5, [_person(RIGHT)]) for i in range(4)]

    trajectory = focus_tracking.compute_smooth_yolo_trajectory(samples, SRC_W, SRC_H)

    before = [p for p in trajectory if p[0] < 2.0]
    at_cut = [p for p in trajectory if abs(p[0] - 2.0) < 1e-6][0]
    hold = before[-1]
    assert hold[1] == before[0][1], "the old position is held right up to the cut"
    assert 2.0 - hold[0] <= focus_tracking.CUT_HOLD_SECONDS + 1e-6
    assert at_cut[1] != hold[1]
    assert _crop_x_at(trajectory, 3.5) == at_cut[1], "it lands in one step, without easing"


def test_a_focus_hidden_inside_one_shot_still_holds_its_position():
    """Same shot, the speaker briefly undetected while a bystander stays in frame:
    that is an occlusion, and the crop must not jump to the bystander."""
    bystander = (1500, 300, 200, 500)
    samples = [
        (0.0, [_person(LEFT), _person(bystander)]),
        (0.5, [_person(bystander)]),
        (1.0, [_person(LEFT), _person(bystander)]),
    ]
    focus = [(0.0, LEFT), (1.0, LEFT)]

    trajectory = focus_tracking.compute_smooth_yolo_trajectory(
        samples, SRC_W, SRC_H, focus_boxes=focus)

    assert [p[0] for p in trajectory] == [0.0, 1.0]
    assert trajectory[0][1] == trajectory[1][1]


def test_people_staying_in_place_are_not_a_shot_change():
    assert not focus_tracking._is_shot_change([LEFT], [(210, 205, 400, 800)])
    assert focus_tracking._is_shot_change([LEFT], [RIGHT])
    assert not focus_tracking._is_shot_change([], [RIGHT]), "no people before: nothing to compare"


# ---------------------------------------------------------------- headline

def test_a_headline_never_ends_on_a_one_word_orphan():
    from PIL import ImageFont

    font = ImageFont.truetype(motion_title._headline_font_path("ABC"), 80)
    words = "THINK IF YOU REALLY THINK ABOUT IT".split()
    lines = motion_title._balanced_lines(words, 3, font, font.getlength(" "))
    assert [word for line in lines for word in line] == words
    assert min(len(" ".join(line)) for line in lines) >= 8, lines


def test_the_number_in_a_headline_is_the_highlighted_word():
    assert motion_title._highlight_words("HE LOST 40,000 IN ONE WEEKEND".split()) == {"40,000"}
    assert motion_title._highlight_words("THE ONE QUESTION THAT GOT HIM FIRED".split()) == {"QUESTION"}


def test_greek_capitals_drop_the_tonos():
    assert motion_title._headline_upper("η ερώτηση που τον απέλυσε") == "Η ΕΡΩΤΗΣΗ ΠΟΥ ΤΟΝ ΑΠΕΛΥΣΕ"
    assert motion_title._headline_upper("προϊόν") == "ΠΡΟΪΟΝ", "the dialytika is not an accent"


def test_a_greek_headline_gets_a_face_that_has_greek_glyphs():
    path = motion_title._headline_font_path("Η ΕΡΩΤΗΣΗ")
    assert "montserrat" not in path.lower(), "Montserrat has no Greek and PIL does not fall back"


# ---------------------------------------------------------------- captions

def test_the_ass_font_size_draws_the_preset_at_its_em_size():
    """libass sizes a face by its line height, so the header number is larger than
    the em size the preset asks for - by the face's own ratio."""
    style = captions.PRESETS["mrbeast"]
    ratio = captions._line_height_ratio(captions._caption_font_file(style))
    assert ratio > 1.0
    assert captions._ass_font_size(style) == round(style.font_size * ratio)
    assert f",{captions._ass_font_size(style)}," in captions.generate_ass_header(style)


def test_a_long_caption_group_is_shrunk_to_fit_the_frame():
    style = captions.PRESETS["mrbeast"]
    short = captions.create_caption_block(
        [{"word": "a", "start": 0.0, "end": 0.3}, {"word": "b", "start": 0.3, "end": 0.6}], style)
    long = captions.create_caption_block(
        [{"word": "extraordinarily", "start": 0.0, "end": 0.3},
         {"word": "complicated", "start": 0.3, "end": 0.6},
         {"word": "circumstances", "start": 0.6, "end": 0.9}], style)

    def size(block):
        return int(re.search(r"\\fs(\d+)\\pos", block).group(1))

    assert size(short) == captions._ass_font_size(style)
    assert size(long) < size(short)


def test_greek_captions_switch_to_a_face_with_greek_glyphs(tmp_path):
    import json

    words = [{"word": "Αυτό", "start": 0.0, "end": 0.4}, {"word": "είναι", "start": 0.4, "end": 0.8}]
    words_file = tmp_path / "words.json"
    words_file.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    out = captions.create_captions(str(words_file), str(tmp_path / "out.ass"), style_name="mrbeast")
    content = open(out, encoding="utf-8").read()
    assert f"Style: Default,{captions.GREEK_CAPABLE_FONT}," in content
    assert "ΑΥΤΟ" in content and "ΕΙΝΑΙ" in content, "capitals, without the tonos"


def test_the_subtitles_filter_is_given_the_bundled_fonts():
    from services.render import _fontsdir_option

    assert _fontsdir_option().startswith(":fontsdir='")
    assert os.path.isfile(os.path.join(captions.FONTS_DIR, "Montserrat-ExtraBold.ttf"))
