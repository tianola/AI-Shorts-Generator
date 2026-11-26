"""The title claim check (services/title_claims.py): numbers in code, quotes checked against the
transcript, a model verdict per claim, and the replacement when a title is rejected.

No test calls a real model: every verdict comes from a fake async `caller`.
"""
import asyncio
import json
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

from services import title_claims as tc
from services.title_style import derive_from_transcript

TRANSCRIPT = "he missed the good ending because he fell asleep in the car on the way home"


def _caller(payload):
    """A fake model call that returns a JSON string built from `payload` (dict or raw string)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)

    async def caller(prompt):
        return raw

    return caller


def _raising(exc):
    async def caller(prompt):
        raise exc

    return caller


def _check(title, transcript, caller):
    return asyncio.run(tc.verify_title_claims(title, transcript, caller=caller))


# ---------------------------------------------------------------------------
# (a) unsupported_numbers
# ---------------------------------------------------------------------------
def test_digits_in_the_title_match_spoken_digits_in_the_clip():
    assert tc.unsupported_numbers("Only 25 seconds left", "there were twenty five seconds left") == []


def test_ten_thousand_in_words_matches_10000_and_10_000_in_digits():
    assert tc.unsupported_numbers("He lost 10,000 in one hand", "he lost ten thousand in one hand") == []
    assert tc.unsupported_numbers("He lost ten thousand in one hand", "he lost 10,000 in one hand") == []
    assert tc.unsupported_numbers("He lost ten thousand in one hand", "he lost 10000 in one hand") == []


def test_twenty_five_in_words_matches_25():
    assert tc.unsupported_numbers("He hit 25 points", "he hit twenty five points") == []


def test_a_year_is_never_flagged():
    assert tc.unsupported_numbers("The best moment of 2024", "nothing about dates here") == []


def test_a_number_the_clip_never_says_is_flagged():
    assert tc.unsupported_numbers("He won 500 dollars", "he won a lot of money") == [500.0]


def test_a_non_latin_transcript_is_not_judged():
    greek = "Ο άνδρας έχασε δέκα χιλιάδες ευρώ στο τραπέζι"
    assert tc.unsupported_numbers("He lost 10000 euros", greek) == []


# ---------------------------------------------------------------------------
# (b) quote_in_transcript
# ---------------------------------------------------------------------------
def test_an_exact_quote_is_in_the_transcript():
    assert tc.quote_in_transcript("he missed the good ending", TRANSCRIPT) is True


def test_a_near_exact_quote_is_in_the_transcript():
    # one word of nine differs: 8 of 9 words match, above the 0.7 overlap
    assert tc.quote_in_transcript("he missed the great ending because he fell asleep", TRANSCRIPT) is True


def test_an_unrelated_quote_is_not_in_the_transcript():
    assert tc.quote_in_transcript("pizza prices are going up", TRANSCRIPT) is False


def test_a_quote_under_two_words_is_never_in_the_transcript():
    assert tc.quote_in_transcript("missed", TRANSCRIPT) is False
    assert tc.quote_in_transcript("", TRANSCRIPT) is False


# ---------------------------------------------------------------------------
# (c) verify_title_claims with a fake caller
# ---------------------------------------------------------------------------
def test_all_supported_with_real_quotes_is_accepted_and_checked():
    payload = {"claims": [
        {"claim": "he missed the ending", "verdict": "supported", "quote": "he missed the good ending"},
    ]}
    result = _check("He missed the good ending", TRANSCRIPT, _caller(payload))
    assert result["accepted"] is True
    assert result["checked"] is True
    assert result["claims"][0]["verdict"] == "supported"


def test_an_unsupported_claim_rejects_the_title_with_a_reason():
    payload = {"claims": [
        {"claim": "he won the lottery", "verdict": "unsupported", "quote": ""},
    ]}
    result = _check("He won the lottery", TRANSCRIPT, _caller(payload))
    assert result["accepted"] is False
    assert result["checked"] is True
    assert result["reasons"] and "he won the lottery" in result["reasons"][0]


def test_a_supported_verdict_without_the_quote_in_the_transcript_is_downgraded():
    payload = {"claims": [
        {"claim": "he won the lottery", "verdict": "supported", "quote": "he won the lottery yesterday"},
    ]}
    result = _check("He won the lottery", TRANSCRIPT, _caller(payload))
    assert result["claims"][0]["verdict"] == "unclear"
    assert result["claims"][0]["quote"] == ""
    assert result["accepted"] is True


def test_a_caller_that_raises_keeps_the_title_unchecked():
    result = _check("He missed the good ending", TRANSCRIPT, _raising(RuntimeError("gateway down")))
    assert result["accepted"] is True
    assert result["checked"] is False


def test_a_number_the_clip_never_says_is_a_hint_to_the_model_not_a_verdict():
    seen = {}

    async def caller(prompt):
        seen["prompt"] = prompt
        return json.dumps({"claims": [
            {"claim": "he lost the money", "verdict": "supported", "quote": "he fell asleep in the car"}]})

    result = _check("He lost $10,000 in the car", TRANSCRIPT, caller)
    assert result["numbers_not_spoken"] == [10000.0]
    assert "10000" in seen["prompt"]
    assert result["accepted"] is True          # the model decides, not the code


def test_a_number_the_model_finds_unsupported_rejects_the_title():
    payload = {"claims": [{"claim": "he lost $10,000", "verdict": "unsupported", "quote": ""}]}
    result = _check("He lost $10,000 in the car", TRANSCRIPT, _caller(payload))
    assert result["accepted"] is False


def test_a_number_alone_never_rejects_when_the_model_call_fails():
    result = _check("He lost $10,000 in the car", TRANSCRIPT, _raising(RuntimeError("down")))
    assert result["accepted"] is True
    assert result["checked"] is False


def test_zero_and_one_are_never_judged():
    assert tc.unsupported_numbers("This 1 trick beats a 0 plan", TRANSCRIPT) == []


def test_figures_split_by_a_word_by_word_transcript_still_match():
    assert tc.unsupported_numbers("$10.8 Trillion Risk", "it adds up to $10 .8 trillion more than") == []


def test_a_scale_word_alone_is_not_a_figure():
    assert tc.unsupported_numbers("Meta wiped a billion-dollar empire, $360 million", "it was $360 million total") == []


def test_an_empty_title_is_accepted_without_a_check():
    result = _check("", TRANSCRIPT, _caller({"claims": []}))
    assert result["accepted"] is True
    assert result["checked"] is False


def test_an_empty_transcript_is_accepted_without_a_check():
    result = _check("He missed the good ending", "   ", _caller({"claims": []}))
    assert result["accepted"] is True
    assert result["checked"] is False


def test_malformed_json_from_the_caller_keeps_the_title_unchecked():
    result = _check("He missed the good ending", TRANSCRIPT, _caller("this is not json {"))
    assert result["accepted"] is True
    assert result["checked"] is False


# ---------------------------------------------------------------------------
# (d) resolve_title
# ---------------------------------------------------------------------------
def test_a_rejected_title_is_replaced_by_the_clips_own_words():
    payload = {"claims": [{"claim": "he won the lottery", "verdict": "unsupported", "quote": ""}]}
    title = "He won the lottery"
    final, record = asyncio.run(tc.resolve_title(title, TRANSCRIPT, caller=_caller(payload),
                                                 rewrite_caller=_raising(RuntimeError("down"))))

    expected = derive_from_transcript(TRANSCRIPT, "")
    assert expected
    assert final == expected
    assert record["replaced"] is True
    assert record["original_title"] == title
    assert record["final_title"] == expected
    assert record["accepted"] is False


def test_an_accepted_title_is_returned_unchanged():
    payload = {"claims": [
        {"claim": "he missed the ending", "verdict": "supported", "quote": "he missed the good ending"},
    ]}
    title = "He missed the good ending"
    final, record = asyncio.run(tc.resolve_title(title, TRANSCRIPT, caller=_caller(payload)))
    assert final == title
    assert record["accepted"] is True
    assert "replaced" not in record


def test_a_rejected_title_with_an_empty_replacement_keeps_the_title(monkeypatch):
    monkeypatch.setattr(tc, "derive_from_transcript", lambda transcript, topic="": "")
    payload = {"claims": [{"claim": "he won the lottery", "verdict": "unsupported", "quote": ""}]}
    title = "He won the lottery"
    final, record = asyncio.run(tc.resolve_title(title, TRANSCRIPT, caller=_caller(payload),
                                                 rewrite_caller=_caller("{}")))
    assert final == title
    assert record["accepted"] is False
    assert "replaced" not in record


def test_a_rejected_title_is_rewritten_as_a_hook_that_passes_the_check():
    verdicts = iter([
        {"claims": [{"claim": "he won the lottery", "verdict": "unsupported", "quote": ""}]},
        {"claims": [{"claim": "he missed the ending", "verdict": "supported",
                     "quote": "he missed the good ending"}]},
    ])

    async def caller(prompt):
        return json.dumps(next(verdicts))

    final, record = asyncio.run(tc.resolve_title(
        "He won the lottery", TRANSCRIPT, caller=caller,
        rewrite_caller=_caller({"title": "Why did he miss the ending?"})))
    assert final == "Why did he miss the ending?"
    assert record["replaced"] is True and record["rewritten"] is True
    assert record["original_title"] == "He won the lottery"


def test_a_rewrite_that_also_fails_falls_back_to_the_clips_words():
    payload = {"claims": [{"claim": "x", "verdict": "unsupported", "quote": ""}]}
    final, record = asyncio.run(tc.resolve_title(
        "He won the lottery", TRANSCRIPT, caller=_caller(payload),
        rewrite_caller=_caller({"title": "He won a car too"})))
    assert final == derive_from_transcript(TRANSCRIPT, "")
    assert "rewritten" not in record
