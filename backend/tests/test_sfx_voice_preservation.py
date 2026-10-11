import pytest
import inspect
import os
from config import settings
from services.sfx import mix_sfx_into_video, pick_sfx_for_clip

def test_sfx_directory_populated():
    """Assert that each category dir has >=1 audio file."""
    sfx_dir = os.path.join(settings.STORAGE_ROOT, "assets", "sfx")
    categories = ["whoosh", "impact", "pop", "suspense", "crowd"]
    for cat in categories:
        cat_path = os.path.join(sfx_dir, cat)
        assert os.path.isdir(cat_path), f"Missing category: {cat}"
        files = [f for f in os.listdir(cat_path) if f.endswith(('.wav', '.mp3'))]
        assert len(files) >= 1, f"Category {cat} is empty"

def test_sfx_ducking_default_is_unity():
    """Verify default duck_original preserves 100% of speech volume."""
    sig = inspect.signature(mix_sfx_into_video)
    assert sig.parameters["duck_original"].default == 1.0
    assert sig.parameters["sfx_volume"].default <= 0.3

def test_pick_sfx_keywords():
    chosen = pick_sfx_for_clip("Crazy Secret Reveal", "this will explode your growth")
    assert len(chosen) >= 1
    assert any(c in ["whoosh_1", "whoosh_2", "swoosh", "pop_1", "pop_2", "click", "impact_1", "impact_2", "thump", "swish", "glitch_transition", "camera_shutter"] for c in chosen)
