"""Cross-platform font resolution for ffmpeg drawtext and PIL.

Provides a single source of truth for font paths so every service
(render, broll, thumbnail) picks a font that actually exists on
the current OS (Windows host or Docker/Linux container).
"""
import os
from typing import Optional

# ---------------------------------------------------------------------------
# Font candidates — order matters: first hit wins.
# ---------------------------------------------------------------------------
_WINDOWS_FONTS = r"C:\Windows\Fonts"

_CANDIDATES_BOLD = [
    # Windows
    os.path.join(_WINDOWS_FONTS, "impact.ttf"),
    os.path.join(_WINDOWS_FONTS, "arialbd.ttf"),
    os.path.join(_WINDOWS_FONTS, "arial.ttf"),
    # Linux / Docker — Montserrat ExtraBold (bundled in image)
    "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
    # Linux — system fallbacks
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]

_CANDIDATES_REGULAR = [
    # Windows
    os.path.join(_WINDOWS_FONTS, "arial.ttf"),
    os.path.join(_WINDOWS_FONTS, "segoeui.ttf"),
    # Linux / Docker
    "/usr/share/fonts/truetype/montserrat/Montserrat-VariableFont_wght.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]


def find_font(bold: bool = True) -> str:
    """Return the absolute path of the first font file that exists on disk.

    If nothing is found, returns an empty string — callers should handle
    this gracefully (e.g. omit fontfile and let ffmpeg/PIL use its default).
    """
    candidates = _CANDIDATES_BOLD if bold else _CANDIDATES_REGULAR
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


def ffmpeg_font_path(font_path: str) -> str:
    """Convert a filesystem path for use inside an ffmpeg ``drawtext`` filter.

    Inside single-quoted ffmpeg filter values the colon ``:`` must be
    escaped as ``\\:``.  On Linux the colon is rare so this is mostly
    needed for Windows drive letters (``C:``).

    Example::

        ffmpeg_font_path("C:\\Windows\\Fonts\\impact.ttf")
        # → "C\\:/Windows/Fonts/impact.ttf"
    """
    normalized = font_path.replace("\\", "/")
    # Escape colons so ffmpeg doesn't treat them as option separators
    return normalized.replace(":", "\\:").replace("'", "\\'")
