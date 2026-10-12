import json
import os
from services.captions import (
    create_captions,
    create_caption_block,
    _group_words_by_natural_phrasing,
    PRESETS,
    inject_emojis,
    CaptionPreset,
)

def test_emoji_injection():
    assert "💰" in inject_emojis("millionaire")
    assert "🔥" in inject_emojis("viral")
    assert "⚠️" in inject_emojis("warning")
    assert "🚀" in inject_emojis("rocket")
    assert "🤯" in inject_emojis("secret")
    assert inject_emojis("normal") == "normal"

def test_all_presets_available():
    expected = ["hormozi", "capcut_aqua", "mrbeast", "cyber_neon", "clean_box", "underline_wave"]
    for p in expected:
        assert p in PRESETS
        assert isinstance(PRESETS[p], CaptionPreset)

def test_group_words_dynamic_4_5_words():
    # 10 natural words -> should split into two groups of 5 words each
    raw_list = ["This", "is", "a", "great", "day", "to", "learn", "how", "to", "code"]
    words = [
        {"word": w, "start": float(i) * 0.3, "end": float(i) * 0.3 + 0.25}
        for i, w in enumerate(raw_list)
    ]
    groups = _group_words_by_natural_phrasing(words, max_group_size=5, max_chars=30)
    assert len(groups) == 2
    assert len(groups[0]) == 5
    assert len(groups[1]) == 5

def test_group_words_char_limit_break():
    # Long words that exceed 28 chars should break at 3 or 4 words
    words = [
        {"word": "Unbelievable", "start": 0.0, "end": 0.5},
        {"word": "transformations", "start": 0.5, "end": 1.0},
        {"word": "require", "start": 1.0, "end": 1.4},
        {"word": "extraordinary", "start": 1.4, "end": 1.9},
        {"word": "effort", "start": 1.9, "end": 2.3},
    ]
    groups = _group_words_by_natural_phrasing(words, max_group_size=5, max_chars=28)
    assert len(groups) >= 2
    assert len(groups[0]) in (3, 4)

def test_group_words_hard_punctuation():
    # Terminal punctuation should split phrases cleanly
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.3},
        {"word": "world!", "start": 0.3, "end": 0.6},
        {"word": "This", "start": 0.6, "end": 0.9},
        {"word": "is", "start": 0.9, "end": 1.1},
        {"word": "great.", "start": 1.1, "end": 1.4},
    ]
    groups = _group_words_by_natural_phrasing(words, max_group_size=5, max_chars=28)
    assert len(groups) == 2
    assert [w["word"] for w in groups[0]] == ["Hello", "world!"]
    assert [w["word"] for w in groups[1]] == ["This", "is", "great."]

def test_group_words_speech_pause():
    # Gap > 0.35s between words indicates natural pause
    words = [
        {"word": "Wait", "start": 0.0, "end": 0.3},
        {"word": "a", "start": 0.3, "end": 0.5},
        {"word": "second", "start": 0.5, "end": 0.8},
        # 0.6s pause here
        {"word": "listen", "start": 1.4, "end": 1.8},
        {"word": "to", "start": 1.8, "end": 2.0},
        {"word": "this", "start": 2.0, "end": 2.3},
    ]
    groups = _group_words_by_natural_phrasing(words, max_group_size=5, max_chars=28)
    assert len(groups) == 2
    assert len(groups[0]) == 3
    assert len(groups[1]) == 3

def test_create_caption_block_stable_text_and_active_word():
    words = [
        {"word": "Create", "start": 0.0, "end": 0.3},
        {"word": "viral", "start": 0.3, "end": 0.6},
        {"word": "content", "start": 0.6, "end": 0.9},
        {"word": "fast", "start": 0.9, "end": 1.2},
    ]
    style = PRESETS["hormozi"]
    ass_block = create_caption_block(words, style=style)
    lines = [line for line in ass_block.strip().split("\n") if line.startswith("Dialogue:")]
    assert len(lines) >= 4

    # Layer 1 active lines have bold \b1 and 1.5x+ popup scale (\fscx155 or \fscx160)
    active_lines = [l for l in lines if l.startswith("Dialogue: 1,")]
    assert len(active_lines) == 4
    for l in active_lines:
        assert "\\b1" in l
        assert "\\pos(" in l
        assert "\\fscx" in l

def test_create_caption_block_no_jump():
    words = [
        {"word": "Clean", "start": 0.0, "end": 0.3},
        {"word": "subtitles", "start": 0.3, "end": 0.6},
    ]
    style = CaptionPreset(name="test_no_jump", jump=False, pop_scale=100)
    ass_block = create_caption_block(words, style=style)
    lines = [line for line in ass_block.strip().split("\n") if line.startswith("Dialogue:")]
    active_lines = [l for l in lines if l.startswith("Dialogue: 1,")]
    assert len(active_lines) == 2
    for line in lines:
        assert "\\t(" not in line  # No scale pop transform when jump is False

def test_create_captions_generates_valid_ass(tmp_path):
    words = [
        {"word": "This", "start": 0.0, "end": 0.3},
        {"word": "secret", "start": 0.3, "end": 0.7},
        {"word": "makes", "start": 0.7, "end": 1.0},
        {"word": "money", "start": 1.0, "end": 1.4},
    ]
    words_file = str(tmp_path / "words.json")
    with open(words_file, "w", encoding="utf-8") as f:
        json.dump(words, f)
    
    out_ass = str(tmp_path / "test.ass")
    res = create_captions(words_file, out_ass, style_name="capcut_aqua")
    
    assert res == out_ass and os.path.exists(out_ass)
    with open(out_ass, "r", encoding="utf-8") as f:
        content = f.read()
    assert "[Script Info]" in content
    assert "PlayResX: 1080" in content
    assert "PlayResY: 1920" in content
    assert "Dialogue:" in content
    assert ("💰" in content) or ("🤯" in content)
