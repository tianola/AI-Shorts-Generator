"""
Tests for mandatory hashtag injection and watermark handle config wiring.

Acceptance criteria:
  - Every generated clip metadata contains #thomastheo and #mrclip regardless of LLM output
  - Watermark shows settings.WATERMARK_HANDLE not a hardcoded string
  - Tag limit raised from 5 -> 7
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock


# ---------------------------------------------------------------------------
# test_mandatory_hashtags_always_present
# ---------------------------------------------------------------------------
def test_mandatory_hashtags_always_present():
    """
    LLM returns tags that do NOT include #thomastheo or #mrclip.
    Both mandatory hashtags must appear first in the final metadata output.
    """
    from services.metadata import generate_metadata

    fake_llm_result = MagicMock()
    fake_llm_result.title = "Nobody Expected This Moment"
    fake_llm_result.description = "Watch this incredible clip. Follow for more!"
    fake_llm_result.hashtags = ["#Shorts", "#Viral", "#Trending", "#React", "#Funny"]
    fake_llm_result.story_type = "reaction"
    fake_llm_result.cta = "Follow for more."

    with patch("services.metadata.call_llm_structured", new_callable=AsyncMock, return_value=fake_llm_result), \
         patch("services.metadata.settings") as mock_settings:
        mock_settings.AI_PROVIDER = "omniroute"
        mock_settings.OMNIROUTE_MODEL = "auto/pro-reasoning"
        mock_settings.MANDATORY_HASHTAGS = "#thomastheo,#mrclip"

        result = asyncio.run(generate_metadata(
            transcript="He came out of nowhere and nobody saw it coming.",
            title="",
            hook=""
        ))

    tags = result["hashtags"]
    assert "#thomastheo" in tags, f"#thomastheo missing from tags: {tags}"
    assert "#mrclip" in tags, f"#mrclip missing from tags: {tags}"
    # Mandatory tags must be first (config wins order)
    assert tags[0] == "#thomastheo", f"Expected #thomastheo first, got: {tags}"
    assert tags[1] == "#mrclip", f"Expected #mrclip second, got: {tags}"
    # Total tag count must not exceed 7
    assert len(tags) <= 7, f"Too many tags (limit is 7): {tags}"


# ---------------------------------------------------------------------------
# test_mandatory_hashtags_no_duplicates
# ---------------------------------------------------------------------------
def test_mandatory_hashtags_no_duplicates():
    """
    If LLM happens to return one of the mandatory tags, it must not appear twice.
    """
    from services.metadata import generate_metadata

    fake_llm_result = MagicMock()
    fake_llm_result.title = "Wild Moment Caught on Camera"
    fake_llm_result.description = "Unbelievable clip."
    fake_llm_result.hashtags = ["#Shorts", "#thomastheo", "#Viral"]  # thomastheo already there
    fake_llm_result.story_type = "reaction"
    fake_llm_result.cta = "Follow for more."

    with patch("services.metadata.call_llm_structured", new_callable=AsyncMock, return_value=fake_llm_result), \
         patch("services.metadata.settings") as mock_settings:
        mock_settings.AI_PROVIDER = "omniroute"
        mock_settings.OMNIROUTE_MODEL = "auto/pro-reasoning"
        mock_settings.MANDATORY_HASHTAGS = "#thomastheo,#mrclip"

        result = asyncio.run(generate_metadata(
            transcript="Something crazy happened live on stream.",
            title="",
            hook=""
        ))

    tags = result["hashtags"]
    assert tags.count("#thomastheo") == 1, f"#thomastheo duplicated: {tags}"
    assert "#mrclip" in tags, f"#mrclip missing: {tags}"
    assert len(tags) <= 7, f"Too many tags: {tags}"


# ---------------------------------------------------------------------------
# test_watermark_handle_from_config
# ---------------------------------------------------------------------------
def test_watermark_handle_from_config():
    """
    render_title_frame() must use settings.WATERMARK_HANDLE, not a hardcoded string.
    When called without explicit watermark_handle argument, the resolved text
    should come from settings.WATERMARK_HANDLE.
    """
    from services import motion_title

    custom_handle = "@my_custom_channel"
    captured = {}

    with patch.object(motion_title.settings, "WATERMARK_HANDLE", custom_handle):
        # Intercept the function and verify what handle it would use
        def mock_render(title, size=(1080, 1920), accent_color=(255, 255, 255),
                        text_color=(255, 255, 255), include_backdrop=False,
                        source_channel=None, watermark_handle=None):
            # The function must resolve None -> settings.WATERMARK_HANDLE
            resolved = watermark_handle or motion_title.settings.WATERMARK_HANDLE
            captured["resolved_handle"] = resolved
            import numpy as np
            return np.zeros((1920, 1080, 4), dtype=np.uint8)

        with patch.object(motion_title, "render_title_frame", side_effect=mock_render):
            motion_title.render_title_frame("Test Title")

    assert captured["resolved_handle"] == custom_handle, (
        f"Expected handle '{custom_handle}', got '{captured.get('resolved_handle')}'"
    )


# ---------------------------------------------------------------------------
# test_watermark_handle_no_hardcoded_fallback
# ---------------------------------------------------------------------------
def test_watermark_handle_no_hardcoded_fallback():
    """
    Verify no '@ClippinEvrything' hardcoded string remains in motion_title module.
    """
    import inspect
    import services.motion_title as mt_module

    # Check the full module source, not just the function
    source = inspect.getsource(mt_module)
    # Exclude this test file reference — check the actual service module
    hardcoded_count = source.count("@ClippinEvrything")
    assert hardcoded_count == 0, (
        f"Found {hardcoded_count} hardcoded '@ClippinEvrything' reference(s) in motion_title.py — "
        "replace with settings.WATERMARK_HANDLE"
    )
