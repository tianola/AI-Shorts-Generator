"""
Comprehensive test suite for Contextual Visual Popups:
- Tone detection & safety gating
- Popup card styling (Pillow)
- FFmpeg filtergraph fragment assembly
- Single-pass filter integration with popup parameters
"""
import os
import tempfile
import pytest
from PIL import Image
from services.tone_detector import is_transcript_somber, classify_content_tone
from services.visual_popup import create_styled_popup_card, build_popup_filtergraph_fragment
from services.render import build_single_pass_filter


# =====================
# 1. Tone Gating Tests
# =====================

def test_tone_safety_blocklist_somber():
    """Negative / somber transcripts must be blocked."""
    assert is_transcript_somber("He sadly died in the tragic accident") is True
    assert is_transcript_somber("The patient is in intensive care with cancer") is True
    assert is_transcript_somber("Victims of the devastating earthquake") is True
    assert is_transcript_somber("She was murdered in cold blood") is True


def test_tone_safety_blocklist_clear():
    """Casual / energetic transcripts must pass."""
    assert is_transcript_somber("This new AI tool builds full apps in seconds") is False
    assert is_transcript_somber("Here is the secret to 10x your productivity") is False
    assert is_transcript_somber("Bitcoin hit an all time high today") is False
    assert is_transcript_somber("") is False


def test_tone_classification_comedy():
    result = classify_content_tone("This is hilarious, the joke was so funny and wild lol")
    assert result["popup_eligible"] is True
    assert result["style_profile"] == "meme_bouncy"
    assert result["tone_category"] == "humorous_entertainment"


def test_tone_classification_tech():
    result = classify_content_tone("Let me show you how to build a Python app with AI models")
    assert result["popup_eligible"] is True
    assert result["style_profile"] == "stock_minimal"
    assert result["tone_category"] == "tech_educational"


def test_tone_classification_business():
    result = classify_content_tone("The market crashed and Bitcoin dropped 30 percent")
    assert result["popup_eligible"] is True
    assert result["style_profile"] == "stock_minimal"
    assert result["tone_category"] == "business_strategy"


def test_tone_classification_somber_overrides():
    result = classify_content_tone("He died after the cancer diagnosis while in intensive care")
    assert result["popup_eligible"] is False
    assert result["style_profile"] == "none"
    assert result["tone_category"] == "somber_tragic"


def test_tone_classification_empty():
    result = classify_content_tone("")
    assert result["popup_eligible"] is True


def test_tone_classification_documentary():
    result = classify_content_tone("This is a documentary about the historical parliament decisions")
    assert result["popup_eligible"] is False
    assert result["tone_category"] == "documentary_serious"


# =====================
# 2. Popup Card Tests (Pillow)
# =====================

def test_popup_card_renders_valid_png():
    """Card renderer produces a valid RGBA PNG."""
    d = tempfile.mkdtemp(prefix="popup_card_")
    raw_path = os.path.join(d, "raw_test.png")
    card_out = os.path.join(d, "popup_card.png")
    try:
        # Create a simple test image
        img = Image.new("RGB", (640, 480), color=(70, 130, 180))
        img.save(raw_path, "PNG")

        result = create_styled_popup_card(raw_path, card_out)
        assert result == card_out
        assert os.path.isfile(card_out)
        assert os.path.getsize(card_out) > 100

        # Validate output is valid RGBA PNG
        with Image.open(card_out) as out_img:
            assert out_img.mode == "RGBA"
            assert out_img.width > 480  # Includes shadow padding
            assert out_img.height > 340  # Includes shadow padding
    finally:
        for f in [raw_path, card_out]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(d)


def test_popup_card_with_custom_params():
    """Card renderer respects custom size and border configuration."""
    d = tempfile.mkdtemp(prefix="popup_custom_")
    raw_path = os.path.join(d, "raw.png")
    card_out = os.path.join(d, "card.png")
    try:
        img = Image.new("RGB", (800, 600), color=(255, 0, 0))
        img.save(raw_path, "PNG")

        result = create_styled_popup_card(
            raw_path, card_out,
            target_size=(400, 400),
            radius=16,
            border_width=6,
        )
        assert os.path.isfile(result)
        with Image.open(result) as out:
            assert out.mode == "RGBA"
    finally:
        for f in [raw_path, card_out]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(d)


def test_popup_card_missing_input_raises():
    """Card renderer raises FileNotFoundError for missing input."""
    with pytest.raises(FileNotFoundError):
        create_styled_popup_card("/nonexistent_path.png", "/tmp/out.png")


# =====================
# 3. FFmpeg Filtergraph Fragment Tests
# =====================

def test_popup_filtergraph_upper_third():
    prep, label, overlay = build_popup_filtergraph_fragment(
        input_idx=2,
        start_time=3.5,
        duration=2.0,
        position="upper_third",
    )
    assert "[2:v]format=rgba" in prep
    assert "scale=w=" in prep
    assert "eval=frame" in prep
    assert "popup_anim_2" in label
    assert "(W-w)/2" in overlay
    assert "between(t" in overlay
    assert "3.5" in overlay
    assert "5.5" in overlay


def test_popup_filtergraph_upper_right():
    prep, label, overlay = build_popup_filtergraph_fragment(
        input_idx=3,
        start_time=5.0,
        duration=2.5,
        position="upper_right",
    )
    assert "[3:v]format=rgba" in prep
    assert "popup_anim_3" in label
    assert "W-w-50" in overlay
    assert "320" in overlay


def test_popup_filtergraph_timing():
    """Verify start/end time math in filtergraph fragment."""
    _, _, overlay = build_popup_filtergraph_fragment(
        input_idx=1,
        start_time=10.0,
        duration=3.0,
    )
    assert "10.0" in overlay
    assert "13.0" in overlay


# =====================
# 4. Single-Pass Filter Integration Tests
# =====================

def test_single_pass_filter_with_popup():
    """build_single_pass_filter includes popup inputs and filtergraph."""
    d = tempfile.mkdtemp(prefix="popup_filter_")
    dummy_popup = os.path.join(d, "dummy_popup.png")
    try:
        Image.new("RGBA", (560, 440), (0, 0, 0, 0)).save(dummy_popup, "PNG")

        extra, fc, _, _ = build_single_pass_filter(
            vertical_frag="[0:v]crop=607:1080:656:0,scale=1080:1920[vsingle]",
            popup_png=dummy_popup,
            popup_start=2.5,
            popup_duration=2.0,
        )
        # Check popup was added to extra inputs
        popup_entries = [e for e in extra if e.get("kind") == "popup"]
        assert len(popup_entries) == 1
        assert popup_entries[0]["path"] == dummy_popup
        # Check filtergraph includes popup animation
        assert "popup_anim" in fc
        assert "between(t" in fc
    finally:
        if os.path.exists(dummy_popup):
            os.remove(dummy_popup)
        os.rmdir(d)


def test_single_pass_filter_without_popup():
    """build_single_pass_filter works normally without popup."""
    extra, fc, _, _ = build_single_pass_filter(
        vertical_frag="[0:v]crop=607:1080:656:0,scale=1080:1920[vsingle]",
    )
    # No popup entries
    popup_entries = [e for e in extra if e.get("kind") == "popup"]
    assert len(popup_entries) == 0
    assert "popup_anim" not in fc


def test_single_pass_filter_with_popup_list():
    """build_single_pass_filter accepts a popups list."""
    d = tempfile.mkdtemp(prefix="popup_list_")
    p1 = os.path.join(d, "p1.png")
    p2 = os.path.join(d, "p2.png")
    try:
        Image.new("RGBA", (560, 440)).save(p1, "PNG")
        Image.new("RGBA", (560, 440)).save(p2, "PNG")

        popups = [
            {"png": p1, "start": 2.0, "duration": 2.0, "position": "upper_third"},
            {"png": p2, "start": 6.0, "duration": 1.5, "position": "upper_right"},
        ]
        extra, fc, _, _ = build_single_pass_filter(
            vertical_frag="[0:v]crop=607:1080:656:0,scale=1080:1920[vsingle]",
            popups=popups,
        )
        popup_entries = [e for e in extra if e.get("kind") == "popup"]
        assert len(popup_entries) == 2
        assert "popup_anim_1" in fc
        assert "popup_anim_2" in fc
    finally:
        for f in [p1, p2]:
            if os.path.exists(f):
                os.remove(f)
        os.rmdir(d)
