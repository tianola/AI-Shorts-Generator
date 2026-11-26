"""
Tests for accent-insensitive Greek matching:
- services.captions: emoji keyword injection and display text per preset
- services.tone_detector: somber safety gate and tone classification

Both modules fold accents and case before matching, so the Greek cases here
use accented, uppercase, inflected and accent-free spellings of the same words.
"""
import pytest
from services.captions import PRESETS, inject_emojis, _display_word, EMOJI_KEYWORD_MAP
from services.tone_detector import TONE_TAXONOMY, classify_content_tone, is_transcript_somber


MONEY = "💰"
WARNING = next(emoji for emoji, keywords in EMOJI_KEYWORD_MAP.items() if "don't" in keywords)


# =====================
# 1. inject_emojis
# =====================

def test_inject_english_keyword_adds_money_emoji():
    assert MONEY in inject_emojis("money")


def test_inject_greek_accented_keyword_adds_money_emoji():
    assert MONEY in inject_emojis("λεφτά")


def test_inject_greek_uppercase_without_accents_adds_money_emoji():
    assert MONEY in inject_emojis("ΛΕΦΤΑ")


def test_inject_greek_inflected_form_adds_money_emoji():
    assert MONEY in inject_emojis("λεφτών")


@pytest.mark.parametrize("word", ["hello", "τα"])
def test_inject_non_keyword_is_unchanged(word):
    assert inject_emojis(word) == word


def test_inject_apostrophe_keyword_matches():
    assert WARNING in inject_emojis("don't")


# =====================
# 2. _display_word per preset
# =====================

@pytest.mark.parametrize("preset_name", ["mrbeast", "hormozi"])
def test_uppercase_preset_emojis_greek_accented_word(preset_name, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "CAPTION_EMOJI_MODE", "text")
    style = PRESETS[preset_name]
    assert style.uppercase is True
    assert MONEY in _display_word({"word": "λεφτά"}, style)


@pytest.mark.parametrize("preset_name", ["mrbeast", "hormozi"])
def test_sticker_mode_keeps_the_caption_text_emoji_free_but_still_finds_the_emoji(preset_name):
    from services.captions import emoji_for
    style = PRESETS[preset_name]
    assert MONEY not in _display_word({"word": "λεφτά"}, style)
    assert emoji_for("ΛΕΦΤΑ") == MONEY


def test_clean_box_preset_adds_no_emoji():
    style = PRESETS["clean_box"]
    assert style.add_emojis is False
    assert _display_word({"word": "λεφτά"}, style) == "λεφτά"
    assert _display_word({"word": "money"}, style) == "money"


# =====================
# 3. is_transcript_somber
# =====================

def test_somber_english_is_flagged():
    assert is_transcript_somber("he died in a plane crash") is True


def test_somber_greek_accented_is_flagged():
    assert is_transcript_somber("ο θάνατος του πατέρα μου") is True


def test_somber_greek_inflected_accent_free_is_flagged():
    assert is_transcript_somber("πέθανε χθες") is True


def test_somber_greek_uppercase_is_flagged():
    assert is_transcript_somber("ΚΗΔΕΙΑ") is True


def test_harmless_greek_sentence_is_not_flagged():
    assert is_transcript_somber("θα πάμε στη θάλασσα") is False


def test_harmless_english_sentence_is_not_flagged():
    assert is_transcript_somber("we went to the beach") is False


def test_greek_hurry_word_is_not_flagged():
    assert is_transcript_somber("βιασύνη") is False


# =====================
# 4. classify_content_tone
# =====================

SOMBER_TEXT = "He died after the cancer diagnosis while in intensive care"


def test_somber_text_is_classified_somber_and_blocked():
    result = classify_content_tone(SOMBER_TEXT)
    assert result["tone_category"] == "somber_tragic"
    assert result["popup_eligible"] is False
    assert result["max_popups"] == 0


def test_somber_result_fields_equal_taxonomy_entry():
    result = classify_content_tone(SOMBER_TEXT)
    profile = TONE_TAXONOMY[result["tone_category"]]
    assert result["popup_eligible"] == profile["popup_eligible"]
    assert result["style_profile"] == profile["style_profile"]
    assert result["max_popups"] == profile["max_popups"]


def test_said_does_not_match_tech_keyword_ai():
    result = classify_content_tone("she said that it was good")
    assert result["tone_category"] == "casual_podcast"


def test_greek_business_sentence_is_business_strategy():
    result = classify_content_tone("οι μετοχές και οι επενδύσεις ανέβηκαν")
    assert result["tone_category"] == "business_strategy"


def test_empty_text_defaults_to_casual_podcast_stock_minimal():
    result = classify_content_tone("")
    assert result["tone_category"] == "casual_podcast"
    assert result["style_profile"] == "stock_minimal"
    # Deliberate: with no text to judge, the fallback is the cautious one popup, not the taxonomy's two.
    assert result["max_popups"] == 1
