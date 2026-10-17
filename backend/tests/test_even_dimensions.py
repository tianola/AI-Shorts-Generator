"""
Test that build_vertical_filter always produces even output dimensions.

ffmpeg's crop filter requires even width and height. When the source aspect
ratio doesn't match the target 9:16 exactly, a scale with
force_original_aspect_ratio=decrease can produce odd dimensions (e.g. 1077x1920
from a 1920x1080 source). The filter chain must end with a crop to the exact
target dimensions to guarantee even output.
"""


def test_face_tracked_filter_produces_even_dimensions(monkeypatch):
    """The face_tracked path must end with a crop to target_width:target_height.

    Without a trailing crop, scale=...:decrease produces odd width (1077) when
    the source aspect ratio doesn't match 9:16 exactly.
    """
    from services import vertical, focus_tracking

    # Mock _probe_size to return a 1920x1080 source (the case that produced 1077)
    monkeypatch.setattr(vertical, "_probe_size", lambda path: (1920, 1080))

    # Mock build_face_crop_filter to return a crop with even dimensions
    # (focus_tracking already rounds to even, so this is realistic)
    def fake_face_crop(samples, src_w, src_h, target_w, target_h, focus_boxes=None):
        return "crop=606:1080:100:0"  # 606 is even, from 1080 * 9/16 rounded

    monkeypatch.setattr(focus_tracking, "build_face_crop_filter", fake_face_crop)

    # Build the filter with a fake analysis that has samples
    # (otherwise it falls back to static)
    import services.reframe_plan as reframe_plan
    analysis = {
        "samples": [(0.0, [(100, 100, 200, 300)])],  # one face sample
        "tracks": [], "n_samples": 1, "src_w": 1920, "src_h": 1080,
        "two_shot": None, "speech": 0.7, "motion": {}, "focus": None,
    }
    monkeypatch.setattr(reframe_plan, "analyse_clip", lambda path, sample_interval=0.5: analysis)
    monkeypatch.setattr(reframe_plan, "choose_focus", lambda *args, **kwargs: {"kind": "person", "boxes": [(0.0, (100, 100, 200, 300))], "reason": "test"})

    frag = vertical.build_vertical_filter(
        "dummy.mp4",
        mode="face_tracked",
        target_width=1080,
        target_height=1920,
        diarization=None,
        attention=None,
    )

    # The filter must end with a crop to the exact target dimensions.
    assert frag.endswith("[vsingle]")
    # Extract the part before [vsingle]
    filter_body = frag.replace("[0:v]", "").replace("[vsingle]", "")
    # The last operation must be crop=1080:1920
    assert filter_body.endswith("crop=1080:1920"), (
        f"face_tracked filter must end with crop=1080:1920 to guarantee even "
        f"dimensions, got: {filter_body}"
    )


def test_static_filter_produces_even_dimensions():
    """The static path already has the trailing crop — this pins it."""
    from services.vertical import build_vertical_filter

    frag = build_vertical_filter(
        "dummy.mp4",
        mode="static",
        target_width=1080,
        target_height=1920,
    )

    assert frag.endswith("[vsingle]")
    filter_body = frag.replace("[0:v]", "").replace("[vsingle]", "")
    assert "crop=1080:1920" in filter_body


def test_fit_blur_filter_produces_even_dimensions():
    """The fit_blur path must also end with a crop to target dimensions."""
    from services.vertical import build_vertical_filter

    frag = build_vertical_filter(
        "dummy.mp4",
        mode="fit_blur",
        target_width=1080,
        target_height=1920,
    )

    assert frag.endswith("[vsingle]")
    filter_body = frag.replace("[0:v]", "").replace("[vsingle]", "")
    # fit_blur uses a different structure (background + foreground), but the
    # final output must still be exactly target_width:target_height
    assert "1080:1920" in filter_body
