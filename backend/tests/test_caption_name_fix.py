"""Caption text fixes from job 112.

Whisper (any size, measured tiny..medium) wrote the Traitorous Eight as "the
traders ate"; a model in context corrects names by word index so timings stay.
Its numbers and hyphenated words came out as separate tokens ("300" ",000").
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services import caption_fix, captions


def _w(*texts):
    return [{"word": t, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, t in enumerate(texts)]


def test_a_misheard_name_is_replaced_word_by_word_with_timings_kept():
    words = _w("The", "traders", "ate", "get", "rich")
    fixed, changed = caption_fix.apply_fixes(words, {1: "Traitorous", 2: "Eight"})
    assert changed == 2
    assert [w["word"] for w in fixed] == ["The", "Traitorous", "Eight", "get", "rich"]
    assert [w["start"] for w in fixed] == [w["start"] for w in words]


def test_a_fix_keeps_the_sentence_end_of_the_word_it_replaces():
    words = _w("lays", "out", "his", "plane.", "Instead")
    fixed, _ = caption_fix.apply_fixes(words, {3: "plan"})
    assert fixed[3]["word"] == "plan."


def test_a_split_name_is_joined_onto_its_first_word():
    words = _w("for", "fair", "child", "and")
    fixed, changed = caption_fix.apply_fixes(words, {1: "Fairchild", 2: ""})
    assert [w["word"] for w in fixed] == ["for", "Fairchild", "and"]
    assert fixed[1]["end"] == words[2]["end"]


def test_a_different_word_is_not_a_correction():
    """free/best offered "token"->"tiny" for "their token $500 input"."""
    assert not caption_fix.sounds_like_a_mishearing("token", "tiny")
    assert caption_fix.sounds_like_a_mishearing("ate", "eight")
    assert caption_fix.sounds_like_a_mishearing("Cliner", "Kleiner")
    words = _w("their", "token", "input")
    assert caption_fix.apply_fixes(words, {1: "tiny"}) == (words, 0)


def test_a_reply_that_rewrites_the_clip_is_ignored():
    words = _w(*"one two three four five six seven eight".split())
    fixes = {i: w["word"].upper() + "x" for i, w in enumerate(words)}
    assert caption_fix.apply_fixes(words, fixes) == (words, 0)


def test_malformed_replies_parse_to_nothing():
    assert caption_fix.parse_fixes("not json", 5) == {}
    assert caption_fix.parse_fixes('{"fixes": [{"i": 9, "word": "x"}]}', 5) == {}
    assert caption_fix.parse_fixes('{"fixes": [{"i": 1, "word": "two words"}]}', 5) == {}
    assert caption_fix.parse_fixes('{"fixes": [{"i": "1", "word": "Kleiner"}]}', 5) == {1: "Kleiner"}


def test_fix_clip_words_rewrites_the_file_from_the_model_reply(tmp_path, monkeypatch):
    path = tmp_path / "words.json"
    path.write_text(json.dumps(_w("met", "Cliner", "today")), encoding="utf-8")

    async def fake_call(prompt, system, model=None, temperature=0.0, max_tokens=0):
        assert "[1]Cliner" in prompt
        return '{"fixes": [{"i": 1, "word": "Kleiner"}]}'

    import services.llm_router as router
    monkeypatch.setattr(router, "call_llm_json", fake_call)
    changed = asyncio.run(caption_fix.fix_clip_words(str(path), title="Fairchild"))
    assert changed == 1
    assert [w["word"] for w in json.loads(path.read_text(encoding="utf-8"))] == ["met", "Kleiner", "today"]


def test_split_number_and_hyphen_tokens_are_glued_in_captions(tmp_path):
    words = _w("get", "300", ",000", "in", "$3", ".5", "face", "-to", "-face", "1990", "2000", "10", "%")
    src = tmp_path / "words.json"
    src.write_text(json.dumps(words), encoding="utf-8")
    out = captions.create_captions(str(src), str(tmp_path / "c.ass"), style_name="mrbeast")
    text = open(out, encoding="utf-8").read()
    for glued in ("300,000", "$3.5", "FACE-TO-FACE", "10%"):
        assert glued in text, glued
    assert ",000" not in text.replace("300,000", "")
    assert "19902000" not in text, "two numbers stay two words"
