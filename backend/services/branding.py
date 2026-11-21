"""Logo and text branding burned onto a finished Short.

The feature set (logo with size/opacity/position, background key-out, text with
colour/box) follows Clipify AI (github.com/sherlock-dev-ai/Clipify-AI-Video-Clipper-FREE,
CC BY-NC 4.0, non-commercial); the implementation is our own.

Branding is stored once in storage/branding.json and applied as the last render
step, so it never changes what the model or the crop saw.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from config import settings

DEFAULTS: Dict = {
    "enabled": False,
    "logo_enabled": False,
    "logo_path": "",
    "logo_size": 8,          # percent of the video width
    "logo_opacity": 80,      # percent
    "logo_position": "top-right",
    "logo_margin": 40,
    "logo_x": 92, "logo_y": 8,   # percent, used by position "custom"
    "logo_bg_remove": False,
    "logo_bg_color": "#9B5CFF",
    "logo_bg_similarity": 0.22,
    "logo_bg_blend": 0.05,
    "text_enabled": False,
    "text": "",
    "text_position": "bottom-center",
    "text_margin": 110,
    "text_x": 50, "text_y": 75,
    "text_size": 6,
    "text_color": "#FFFFFF",
    "text_bg_enabled": False,
    "text_bg_color": "#000000",
    "text_bg_opacity": 65,
}

_POSITIONS = ("top-left", "top-center", "top-right", "center", "bottom-left",
              "bottom-center", "bottom-right", "custom")
_HEX = re.compile(r"^#?([0-9a-fA-F]{6})$")


def _store_path() -> Path:
    return Path(settings.STORAGE_ROOT) / "branding.json"


def logo_dir() -> Path:
    path = Path(settings.STORAGE_ROOT) / "branding"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hex6(value, default="FFFFFF") -> str:
    match = _HEX.match(str(value or "").strip())
    return match.group(1).upper() if match else default


def _num(value, low, high, default):
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def clean(raw: Optional[dict]) -> dict:
    """A branding config with every field present and inside its range."""
    cfg = dict(DEFAULTS)
    for key, value in (raw or {}).items():
        if key in DEFAULTS:
            cfg[key] = value
    for key in ("enabled", "logo_enabled", "logo_bg_remove", "text_enabled", "text_bg_enabled"):
        cfg[key] = bool(cfg[key])
    for key in ("logo_position", "text_position"):
        if cfg[key] not in _POSITIONS:
            cfg[key] = DEFAULTS[key]
    cfg["logo_size"] = _num(cfg["logo_size"], 3, 20, 8)
    cfg["logo_opacity"] = _num(cfg["logo_opacity"], 10, 100, 80)
    cfg["logo_margin"] = _num(cfg["logo_margin"], 5, 500, 40)
    cfg["text_margin"] = _num(cfg["text_margin"], 20, 500, 110)
    cfg["text_size"] = _num(cfg["text_size"], 3, 12, 6)
    cfg["text_bg_opacity"] = _num(cfg["text_bg_opacity"], 0, 100, 65)
    cfg["logo_bg_similarity"] = _num(cfg["logo_bg_similarity"], 0.01, 1.0, 0.22)
    cfg["logo_bg_blend"] = _num(cfg["logo_bg_blend"], 0.0, 1.0, 0.05)
    for key in ("logo_x", "logo_y", "text_x", "text_y"):
        cfg[key] = _num(cfg[key], 0, 100, DEFAULTS[key])
    cfg["text"] = str(cfg["text"] or "").strip()[:120]
    cfg["logo_path"] = str(cfg["logo_path"] or "")
    return cfg


def load() -> dict:
    try:
        return clean(json.loads(_store_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return clean(None)


def save(raw: dict) -> dict:
    cfg = clean(raw)
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def _xy(position: str, margin: int, w: str, h: str) -> tuple:
    left, right = f"{margin}", f"(W-{w}-{margin})"
    top, bottom = f"{margin}", f"(H-{h}-{margin})"
    centre_x, centre_y = f"(W-{w})/2", f"(H-{h})/2"
    return {
        "top-left": (left, top), "top-center": (centre_x, top), "top-right": (right, top),
        "center": (centre_x, centre_y),
        "bottom-left": (left, bottom), "bottom-center": (centre_x, bottom),
        "bottom-right": (right, bottom),
    }.get(position, (right, top))


def _filter_path(path: str) -> str:
    """A path as ffmpeg's filter parser wants it (forward slashes, escaped colon)."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def _font_file() -> Optional[str]:
    bundled = Path(__file__).resolve().parent.parent / "assets" / "fonts"
    for folder in (bundled, Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"):
        for name in ("Montserrat-ExtraBold.ttf", "seguibl.ttf", "arialbd.ttf"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return None


def build_command(src: str, dst: str, cfg: dict, text_file: Optional[str] = None) -> Optional[List[str]]:
    """The ffmpeg command that burns the configured branding in, or None if there is none."""
    cfg = clean(cfg)
    logo = cfg["logo_path"] if cfg["logo_enabled"] and os.path.isfile(cfg["logo_path"]) else ""
    text = cfg["text"] if cfg["text_enabled"] else ""
    if not logo and not text:
        return None

    inputs = ["-i", src]
    filters: List[str] = []
    current = "0:v"
    if logo:
        inputs += ["-i", logo]
        if cfg["logo_position"] == "custom":
            x = f"(W-w)*{cfg['logo_x'] / 100:.6f}"
            y = f"(H-h)*{cfg['logo_y'] / 100:.6f}"
        else:
            x, y = _xy(cfg["logo_position"], int(cfg["logo_margin"]), "w", "h")
        chain = "[1:v]format=rgba"
        if cfg["logo_bg_remove"]:
            chain += (f",colorkey=0x{_hex6(cfg['logo_bg_color'], '9B5CFF')}:"
                      f"{cfg['logo_bg_similarity']:.3f}:{cfg['logo_bg_blend']:.3f}")
        width = max(32, round(1080 * cfg["logo_size"] / 100))
        chain += f",colorchannelmixer=aa={cfg['logo_opacity'] / 100:.3f},scale={width}:-1[wm]"
        filters.append(chain)
        filters.append(f"[{current}][wm]overlay=x={x}:y={y}:format=auto[vlogo]")
        current = "vlogo"
    if text and text_file:
        if cfg["text_position"] == "custom":
            x = f"(w-tw)*{cfg['text_x'] / 100:.6f}"
            y = f"(h-th)*{cfg['text_y'] / 100:.6f}"
        else:
            x, y = _xy(cfg["text_position"], int(cfg["text_margin"]), "tw", "th")
            x, y = x.replace("W", "w"), y.replace("H", "h")
        size = max(34, round(1080 * cfg["text_size"] / 100))
        font = _font_file()
        font_expr = f":fontfile='{_filter_path(font)}'" if font else ""
        box_alpha = cfg["text_bg_opacity"] / 100
        box = 1 if cfg["text_bg_enabled"] else 0
        filters.append(
            f"[{current}]drawtext=textfile='{_filter_path(text_file)}':fontsize={size}"
            f":fontcolor=0x{_hex6(cfg['text_color'])}{font_expr}:x={x}:y={y}:box={box}"
            f":boxcolor=0x{_hex6(cfg['text_bg_color'], '000000')}@{box_alpha:.2f}:boxborderw=18:fix_bounds=true[vtext]"
        )
        current = "vtext"
    return ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
            "-map", f"[{current}]", "-map", "0:a?", "-c:v", "libx264", "-preset", "fast",
            "-crf", "20", "-c:a", "copy", "-movflags", "+faststart", dst]


def apply_branding(src: str, dst: str, cfg: Optional[dict] = None) -> bool:
    """Write `dst` as `src` with the branding burned in. False when there is nothing to burn."""
    cfg = clean(cfg if cfg is not None else load())
    text_file = None
    if cfg["text_enabled"] and cfg["text"]:
        text_file = str(Path(dst).with_name("_branding_text.txt"))
        Path(text_file).write_text(cfg["text"], encoding="utf-8")
    try:
        command = build_command(src, dst, cfg, text_file)
        if command is None:
            return False
        proc = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if proc.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
            raise RuntimeError(f"Branding render failed: {proc.stderr[-1500:]}")
        return True
    finally:
        if text_file:
            try:
                os.remove(text_file)
            except OSError:
                pass
