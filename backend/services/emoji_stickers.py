"""Colour emoji for burned-in captions, drawn as PNG stickers instead of ASS text.

libass cannot draw colour emoji fonts (COLR, CBDT, sbix, SVG), so an emoji written into the
subtitle text comes out as an empty box or a flat outline. Pillow can draw them in colour,
so each emoji is rendered once to a transparent PNG and laid over the video by ffmpeg at
the time and place the caption code worked out.

The PNGs are made on the host from an emoji font that is already installed (Segoe UI Emoji
on Windows, Noto Color Emoji on most Linux images) and cached under STORAGE_ROOT/assets/emoji.
Nothing third-party is shipped in the repository. With no emoji font the stickers are
skipped, and no emoji is drawn at all, which beats a box.

A caption run leaves ``<captions>.ass.emoji.json`` next to the ASS file. The renderers read
it from there, so every path that burns captions picks the stickers up without new arguments.
"""

import json
import os
from typing import Dict, List, Optional, Tuple

from loguru import logger

from config import settings

STICKER_PX = 128
SIDECAR_SUFFIX = ".emoji.json"
# More than this on one clip is clutter and one extra ffmpeg input each.
MAX_STICKERS_PER_CLIP = 8
POP_SECONDS = 0.12

_FONT_CANDIDATES = (
    "C:/Windows/Fonts/seguiemj.ttf",
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/noto-color-emoji/NotoColorEmoji.ttf",
)

_font_checked = False
_font_path: Optional[str] = None


def emoji_font_path() -> Optional[str]:
    """The installed colour emoji font, or None. EMOJI_FONT_PATH overrides the search."""
    global _font_checked, _font_path
    if _font_checked:
        return _font_path
    _font_checked = True
    configured = getattr(settings, "EMOJI_FONT_PATH", None)
    for candidate in ([configured] if configured else []) + list(_FONT_CANDIDATES):
        if candidate and os.path.isfile(candidate):
            _font_path = candidate
            break
    if _font_path is None:
        logger.warning("No colour emoji font found; caption emoji are skipped.")
    return _font_path


def reset_font_cache() -> None:
    """Forget the font lookup. Used by tests."""
    global _font_checked, _font_path
    _font_checked = False
    _font_path = None


def available() -> bool:
    return emoji_font_path() is not None


def _asset_dir() -> str:
    return os.path.join(settings.STORAGE_ROOT, "assets", "emoji")


def _file_name(emoji: str) -> str:
    return "-".join(f"{ord(ch):x}" for ch in emoji) + ".png"


def sticker_png(emoji: str) -> Optional[str]:
    """Path to the PNG for this emoji, rendering and caching it on first use. None if it cannot be drawn."""
    path = os.path.join(_asset_dir(), _file_name(emoji))
    if os.path.isfile(path):
        return path
    font_file = emoji_font_path()
    if not font_file:
        return None
    try:
        from PIL import Image, ImageDraw, ImageFont

        font = ImageFont.truetype(font_file, 109)  # CBDT fonts only load at 109
        canvas = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        ImageDraw.Draw(canvas).text((40, 40), emoji, font=font, embedded_color=True)
        box = canvas.getbbox()
        if not box:
            return None
        glyph = canvas.crop(box)
        side = max(glyph.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(glyph, ((side - glyph.width) // 2, (side - glyph.height) // 2))
        square = square.resize((STICKER_PX, STICKER_PX), Image.LANCZOS)
        os.makedirs(_asset_dir(), exist_ok=True)
        tmp = path + ".tmp.png"
        square.save(tmp)
        os.replace(tmp, path)
        return path
    except Exception as exc:  # a bad font must not stop a render
        logger.warning(f"Could not draw emoji sticker {emoji!r}: {exc}")
        return None


def sidecar_path(captions_path: str) -> str:
    return captions_path + SIDECAR_SUFFIX


def write_sidecar(captions_path: str, stickers: List[Dict]) -> None:
    """Write the anchors, or remove the file when there are none, so a stale list never survives a rebuild."""
    target = sidecar_path(captions_path)
    if not stickers:
        try:
            os.remove(target)
        except OSError:
            pass
        return
    with open(target, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "stickers": stickers[:MAX_STICKERS_PER_CLIP]}, handle, ensure_ascii=False)


def load_stickers(captions_path: Optional[str]) -> List[Dict]:
    """The stickers for a captions file, each with a ``path`` to a PNG that exists. Empty when there are none."""
    if not captions_path:
        return []
    target = sidecar_path(captions_path)
    if not os.path.isfile(target):
        return []
    try:
        with open(target, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    # Valid JSON of the wrong shape ("[]", a string) must not crash a render either.
    if not isinstance(data, dict) or not isinstance(data.get("stickers"), list):
        return []
    result = []
    for item in data["stickers"]:
        if not isinstance(item, dict) or not item.get("emoji"):
            continue
        png = sticker_png(item["emoji"])
        if png:
            result.append({**item, "path": png})
    return result[:MAX_STICKERS_PER_CLIP]


def build_overlay_graph(stickers: List[Dict], first_input_idx: int, cur_v: str) -> Tuple[List[Dict], List[str], str, int]:
    """ffmpeg filter parts that lay each sticker over the video.

    Returns (extra_inputs, filter_parts, new_video_label, next_input_idx). Each sticker is
    its own looped PNG input. It pops in over POP_SECONDS (a short overshoot) and is drawn
    only between its start and end. The centre stays put while the size changes.
    """
    extra_inputs: List[Dict] = []
    parts: List[str] = []
    next_idx = first_input_idx
    for i, st in enumerate(stickers):
        size = max(24, int(st.get("size", 96)))
        start = max(0.0, float(st["start"]))
        end = max(start + 0.2, float(st["end"]))
        cx, cy = float(st["x"]), float(st["y"])
        extra_inputs.append({"path": st["path"], "kind": "emoji"})
        idx = next_idx
        next_idx += 1
        grow = f"clip((t-{start:.3f})/{POP_SECONDS},0,1)*(1+0.18*sin(clip((t-{start:.3f})/{POP_SECONDS * 1.5:.3f},0,1)*PI))"
        parts.append(
            f"[{idx}:v]format=rgba,scale=w='max(2,{size}*{grow})':h='max(2,{size}*{grow})':eval=frame[em{i}]"
        )
        out = f"[vem{i}]"
        parts.append(
            f"{cur_v}[em{i}]overlay=x={cx:.1f}-w/2:y={cy:.1f}-h/2"
            f":enable='between(t,{start:.3f},{end:.3f})':shortest=1:format=auto{out}"
        )
        cur_v = out
    return extra_inputs, parts, cur_v, next_idx
