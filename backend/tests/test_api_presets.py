import pytest
from database.session import SessionLocal, init_database
from database.models import Clip, Job
from services.captions import PRESETS

def test_models_have_style_and_layout_fields():
    init_database()
    db = SessionLocal()
    try:
        # Verify Clip attributes exist
        c = Clip(
            job_id=1,
            index=0,
            start=0.0,
            end=10.0,
            caption_style="hormozi",
            layout_mode="single_speaker"
        )
        assert c.caption_style == "hormozi"
        assert c.layout_mode == "single_speaker"
    finally:
        db.close()

def test_preset_names_match_api():
    assert "hormozi" in PRESETS
    assert "capcut_aqua" in PRESETS
    assert "mrbeast" in PRESETS
    assert "cyber_neon" in PRESETS
    assert "clean_box" in PRESETS
    assert "underline_wave" in PRESETS
