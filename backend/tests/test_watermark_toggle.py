"""The watermark pill + source credit must be switchable off.

The channel is moving away from re-cut third-party content, and a burned-in
"@handle · Original: @source" pill is both a distribution penalty on Shorts and
a "reused content" tell. render_title_frame drew it unconditionally; the
WATERMARK_ENABLED flag now gates the whole block.
"""
import numpy as np

from config import settings
from services.motion_title import render_title_frame


def _card(**kw):
    return render_title_frame("GROCERY STORE CHAOS", size=(270, 480), **kw)


def test_watermark_disabled_ignores_handle_and_source(monkeypatch):
    monkeypatch.setattr(settings, "WATERMARK_ENABLED", False, raising=False)
    plain = _card()
    branded = _card(watermark_handle="@someone", source_channel="@source")
    assert np.array_equal(plain, branded), "watermark still drawn while disabled"


def test_watermark_enabled_draws_the_handle(monkeypatch):
    monkeypatch.setattr(settings, "WATERMARK_ENABLED", True, raising=False)
    plain = _card()
    branded = _card(watermark_handle="@someone", source_channel="@source")
    assert not np.array_equal(plain, branded), "watermark not drawn while enabled"
