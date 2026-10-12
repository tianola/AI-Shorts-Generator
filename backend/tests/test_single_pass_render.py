from services.render import burn_captions

def test_burn_captions_signature():
    import inspect
    sig = inspect.signature(burn_captions)
    assert "video_path" in sig.parameters
    assert "captions_path" in sig.parameters
    assert "output_path" in sig.parameters
