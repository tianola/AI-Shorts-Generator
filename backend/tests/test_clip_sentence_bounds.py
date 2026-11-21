"""Clips end where the sentence ends, not where a transcript segment or a length cap does.

Job 113: five of ten clips stopped mid-sentence ("...he'll have lost nearly").
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.clip_bounds import TAIL_PAD, load_words, snap_to_sentences
from worker import clip_audio_fades


def _words(text, start=0.0, step=0.4, pauses=None):
    pauses = pauses or {}
    out, t = [], start
    for i, token in enumerate(text.split()):
        out.append({"word": " " + token, "start": round(t, 3), "end": round(t + 0.3, 3)})
        t += step + pauses.get(i, 0.0)
    return out


def _spoken(words, window):
    s, e = window
    return [w["word"].strip() for w in words if s <= w["start"] and w["end"] <= e]


def test_the_end_moves_forward_to_finish_the_sentence():
    words = _words("Intro words here. If the company fails he will have lost nearly "
                   "one and a half million dollars. So what is in it for him?")
    cut_at = next(w["end"] for w in words if w["word"] == " nearly")
    window = snap_to_sentences(words[3]["start"], cut_at, words, min_len=2, max_len=60)
    assert _spoken(words, window)[-1] == "dollars."
    assert _spoken(words, window)[0] == "If"


def test_a_sentence_past_the_cap_ends_the_clip_at_the_previous_sentence():
    words = _words("One two three. Four five six. " + "long " * 200 + "end.")
    window = snap_to_sentences(0.0, 30.0, words, min_len=1, max_len=20)
    assert _spoken(words, window)[-1] == "six."
    assert window[1] - window[0] <= 20 + 0.2


def test_a_comma_and_a_breath_do_not_end_the_sentence():
    words = _words("Companies that rival nations in markets and even politics, but with none "
                   "of the regulation. Next part starts here.", pauses={8: 0.9})
    politics = next(w for w in words if w["word"] == " politics,")
    window = snap_to_sentences(0.0, politics["end"], words, min_len=1, max_len=60)
    assert _spoken(words, window)[-1] == "regulation."


def test_unpunctuated_speech_ends_on_a_pause():
    words = _words("we went there and it was great " + "then we kept talking " * 40, pauses={6: 1.2})
    window = snap_to_sentences(0.0, 2.0, words, min_len=1, max_len=30)
    assert _spoken(words, window)[-1] == "great"


def test_the_last_word_keeps_its_tail_without_reaching_the_next_word():
    words = _words("First sentence ends here. Second one.", step=0.5)
    window = snap_to_sentences(0.0, 1.0, words, min_len=1, max_len=60)
    here = next(w for w in words if w["word"] == " here.")
    nxt = words[words.index(here) + 1]
    assert here["end"] < window[1] <= min(here["end"] + TAIL_PAD, nxt["start"])


def test_too_short_to_reach_the_minimum_is_rejected():
    words = _words("Tiny. Clip.")
    assert snap_to_sentences(0.0, 0.5, words, min_len=20, max_len=60) is None


def test_load_words_reads_the_job_file(tmp_path):
    path = tmp_path / "words.json"
    path.write_text(json.dumps([{"word": " b", "start": 1, "end": 1.2},
                                {"word": " a", "start": 0, "end": 0.2}, {"bad": 1}]))
    assert [w["word"] for w in load_words(str(path))] == [" a", " b"]
    assert load_words(str(tmp_path / "missing.json")) == []


def test_cut_audio_fades_in_and_out_inside_the_clip():
    chain = clip_audio_fades(20.0)
    assert "afade=t=in:st=0" in chain and "afade=t=out:st=19.750:d=0.250" in chain
    assert "afade=t=out:st=0.250:d=0.250" in clip_audio_fades(0.5) or "d=0.125" in clip_audio_fades(0.5)
