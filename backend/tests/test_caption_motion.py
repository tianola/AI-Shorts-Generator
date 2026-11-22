import dataclasses
import json
import re

import pytest

from services.captions import (
    CAPTION_MOTIONS,
    PRESETS,
    ass_time,
    apply_motion,
    create_captions,
    is_emphasis_word,
)
from services.editing.profiles import (
    CAPTION_MOTION_BY_PROFILE,
    PROFILES,
    caption_motion_for,
    profile_for_tone,
)

MOTIONS = [None, "kinetic", "pop", "cinematic", "emphasis"]
WORD_TEXTS = ["This", "is", "money", "we", "make", "300", "dollars", "for", "everyone", "watch", "this"]
WORD_STEP = 0.42   # seconds between word starts
WORD_LEN = 0.38    # seconds each word is spoken


def _words(texts, step=WORD_STEP, length=WORD_LEN):
    return [{"word": w, "start": i * step, "end": i * step + length} for i, w in enumerate(texts)]


def _build(tmp_path, name, words, motion=None, style_name="mrbeast"):
    words_file = tmp_path / f"{name}_words.json"
    words_file.write_text(json.dumps(words, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / f"{name}.ass"
    create_captions(str(words_file), str(out), style_name=style_name, motion=motion)
    return out.read_text(encoding="utf-8")


def _dialogues(ass_text):
    rows = []
    for line in ass_text.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        head = line.split(",", 9)
        rows.append({
            "layer": int(head[0].split(":")[1]),
            "start": head[1],
            "end": head[2],
            "body": head[9],
        })
    return rows


def _active(rows, index):
    """The Layer 1 (active) event of the word at `index`: its start is that word's start time."""
    want = ass_time(index * WORD_STEP)
    found = [r for r in rows if r["layer"] == 1 and r["start"] == want]
    assert len(found) == 1, f"expected one active event for word {index}, got {len(found)}"
    return found[0]


def _text_of(event):
    return event["body"].rsplit("}", 1)[-1]


# 1. apply_motion

def test_apply_motion_unknown_or_empty_returns_same_preset():
    base = PRESETS["mrbeast"]
    for motion in (None, "", "no_such_motion"):
        assert apply_motion(base, motion) is base


def test_cinematic_is_still_with_a_fade():
    s = apply_motion(PRESETS["mrbeast"], "cinematic")
    assert s.jump is False
    assert s.pop_scale == 100
    assert s.fade_ms > 0
    assert s.emphasis_scale == 0


def test_pop_is_bigger_with_a_fade():
    s = apply_motion(PRESETS["mrbeast"], "pop")
    assert s.pop_scale >= 135
    assert s.fade_ms > 0
    assert s.jump is True


def test_emphasis_reserves_gap_for_the_harder_pop():
    from services.captions import _caption_gaps
    base = PRESETS["mrbeast"]
    s = apply_motion(base, "emphasis")
    # Plain words keep the preset's peak; only emphasis words go to 145, and the gaps reserve room for it.
    assert s.emphasis_scale == 145
    assert s.pop_scale < 145
    assert _caption_gaps([300.0, 300.0], 20.0, s)[0] > _caption_gaps([300.0, 300.0], 20.0, base)[0]


def test_kinetic_keeps_preset_jump_and_pop_when_jump_is_true():
    base = PRESETS["mrbeast"]
    assert base.jump is True
    s = apply_motion(base, "kinetic")
    assert s.jump is base.jump
    assert s.pop_scale == base.pop_scale


def test_kinetic_keeps_jump_false_on_a_still_preset():
    still = dataclasses.replace(PRESETS["clean_box"], jump=False)
    s = apply_motion(still, "kinetic")
    assert s.jump is False


@pytest.mark.parametrize("motion", sorted(CAPTION_MOTIONS))
def test_karaoke_false_wins_for_every_motion(motion):
    s = apply_motion(PRESETS["mrbeast"], motion, karaoke=False)
    assert s.jump is False
    assert s.pop_scale == 100
    assert s.emphasis_scale == 0


def test_apply_motion_never_mutates_input():
    base = PRESETS["mrbeast"]
    before = dataclasses.asdict(base)
    for motion in sorted(CAPTION_MOTIONS):
        result = apply_motion(base, motion)
        assert result is not base
        assert dataclasses.asdict(base) == before


# 2. is_emphasis_word

@pytest.mark.parametrize("word", ["money", "λεφτά", "300", "$3.5"])
def test_emphasis_words_true(word):
    assert is_emphasis_word(word) is True


@pytest.mark.parametrize("word", ["this", "the", ""])
def test_plain_words_false(word):
    assert is_emphasis_word(word) is False


# 3. create_captions with motions

@pytest.mark.parametrize("motion", MOTIONS)
def test_no_word_moves_outside_the_frame(tmp_path, motion):
    ass = _build(tmp_path, f"pos_{motion}", _words(WORD_TEXTS), motion=motion)
    xs = [float(x) for x in re.findall(r"\\pos\((-?[\d.]+),", ass)]
    assert xs, "expected positioned events"
    assert all(0 <= x <= 1080 for x in xs)


def test_cinematic_fades_in_without_moving(tmp_path):
    ass = _build(tmp_path, "cinematic", _words(WORD_TEXTS), motion="cinematic")
    rows = _dialogues(ass)
    assert any("\\fad(" in r["body"] for r in rows)
    assert not any("\\t(" in r["body"] for r in rows)


def test_pop_fades_in_and_pops(tmp_path):
    ass = _build(tmp_path, "pop", _words(WORD_TEXTS), motion="pop")
    rows = _dialogues(ass)
    assert any("\\fad(" in r["body"] for r in rows)
    assert any("\\t(" in r["body"] for r in rows)


@pytest.mark.parametrize("motion", [None, "kinetic"])
def test_kinetic_and_none_have_no_fade(tmp_path, motion):
    ass = _build(tmp_path, f"nofade_{motion}", _words(WORD_TEXTS), motion=motion)
    assert "\\fad(" not in ass


def _peak_fscx(ass):
    values = [int(v) for v in re.findall(r"\\fscx(\d+)", ass)]
    return max(values)


def test_peak_scale_follows_motion(tmp_path):
    words = _words(WORD_TEXTS)
    none_ass = _build(tmp_path, "peak_none", words, motion=None)
    kinetic_ass = _build(tmp_path, "peak_kinetic", words, motion="kinetic")
    pop_ass = _build(tmp_path, "peak_pop", words, motion="pop")
    emph_ass = _build(tmp_path, "peak_emph", words, motion="emphasis")

    assert _peak_fscx(none_ass) == PRESETS["mrbeast"].pop_scale == 130
    assert _peak_fscx(kinetic_ass) == PRESETS["mrbeast"].pop_scale == 130
    assert _peak_fscx(pop_ass) >= 135
    assert _peak_fscx(emph_ass) == 145


def test_emphasis_word_pops_harder_than_plain_word(tmp_path):
    ass = _build(tmp_path, "emph_words", _words(WORD_TEXTS), motion="emphasis")
    rows = _dialogues(ass)
    money = _active(rows, WORD_TEXTS.index("money"))
    this = _active(rows, 0)
    assert "\\fscx145" in money["body"]
    assert "\\fscx145" not in this["body"]


@pytest.mark.parametrize("motion", MOTIONS)
def test_fade_only_on_a_words_first_event(tmp_path, motion):
    ass = _build(tmp_path, f"fade_{motion}", _words(WORD_TEXTS), motion=motion)
    counts = {}
    for r in _dialogues(ass):
        if "\\fad(" not in r["body"]:
            continue
        # Active events are keyed by their start, the lead-in (Layer 0) events by their end.
        key = r["start"] if r["layer"] == 1 else r["end"]
        counts[key] = counts.get(key, 0) + 1
    assert all(n <= 1 for n in counts.values())


def test_none_and_kinetic_output_identical_for_mrbeast(tmp_path):
    words = _words(WORD_TEXTS)
    none_ass = _build(tmp_path, "same_none", words, motion=None)
    kinetic_ass = _build(tmp_path, "same_kinetic", words, motion="kinetic")
    assert none_ass == kinetic_ass


def test_default_settings_write_no_emoji_into_ass_text(tmp_path):
    ass = _build(tmp_path, "emoji_default", _words(WORD_TEXTS), motion="emphasis")
    assert "💰" not in ass and "🤯" not in ass and "⚠" not in ass


# 4. profiles

@pytest.mark.parametrize("profile_id, expected", [
    ("dynamic", "kinetic"),
    ("educational", "kinetic"),
    ("humorous", "pop"),
    ("dramatic", "emphasis"),
    ("cinematic", "cinematic"),
    ("documentary", "cinematic"),
])
def test_caption_motion_for_each_profile(profile_id, expected):
    assert caption_motion_for(PROFILES[profile_id]) == expected


def test_every_profile_motion_is_a_known_motion():
    for profile_id in CAPTION_MOTION_BY_PROFILE:
        assert CAPTION_MOTION_BY_PROFILE[profile_id] in CAPTION_MOTIONS


def test_every_profile_has_a_caption_motion():
    for profile_id in PROFILES:
        assert profile_id in CAPTION_MOTION_BY_PROFILE


def test_somber_tone_gets_cinematic_motion():
    assert caption_motion_for(profile_for_tone("somber_tragic")) == "cinematic"


def test_humorous_tone_gets_pop_motion():
    assert caption_motion_for(profile_for_tone("humorous_entertainment")) == "pop"


# 5. Greek words

def test_greek_words_with_emphasis_motion(tmp_path):
    words = _words(["λεφτά", "αλήθεια", "ποτέ", "ξανά"])
    ass = _build(tmp_path, "greek", words, motion="emphasis")
    assert "Style: Default,Segoe UI Black," in ass
    rows = _dialogues(ass)
    active = _active(rows, 0)
    assert _text_of(active) == "ΛΕΦΤΑ"
    assert "\\fscx145" in active["body"]
