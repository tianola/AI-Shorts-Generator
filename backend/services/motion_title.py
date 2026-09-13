"""
Motion-graphic title overlay service.

Replaces the old "grey box + solid band" ffmpeg drawtext with a clean,
professional motion-graphic title card:

  - Renders a transparent 1080x1920 frame with PIL + NumPy
  - Design: uppercase letter-spaced headline, thin gradient accent bar,
    soft drop shadow, subtle top glow, no solid grey box
  - Animated with ffmpeg: fade-in + slight upward slide (alpha ramp)
  - Composites over the clip preserving audio
"""
import os
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from config import settings
from utils.logger import job_info, job_error


def _find_font(bold: bool = True) -> str:
    try:
        from services.font_resolve import find_font
        path = find_font(bold=bold)
        if path:
            return path
    except Exception:
        pass
    # Fallbacks
    for cand in [
        r"C:\Windows\Fonts\impact.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.isfile(cand):
            return cand
    return ""


def _wrap_title(title: str, max_chars: int = 26, max_lines: int = 2) -> list:
    """Wrap + truncate a title into at most `max_lines` clean lines."""
    words = str(title or "Viral Moment").strip().upper().split()
    if not words:
        words = ["VIRAL", "MOMENT"]
    lines, current = [], []
    for word in words:
        candidate = " ".join(current + [word])
        if len(candidate) > max_chars and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current = [word] if not current else current + [word]
    # ensure current entries flushed
    if current:
        joined = " ".join(current)
        if len(joined) > max_chars and len(lines) < max_lines:
            lines.append(joined)
        elif current:
            lines.append(joined)
    # Truncate overflow with ellipsis
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > max_chars - 1:
            lines[-1] = lines[-1][: max_chars - 1].rstrip() + "..."
    if not lines:
        lines = ["VIRAL MOMENT"]
    return lines


def _make_alpha_gradient(size, direction=1):
    """Return a vertical gradient alpha used for a soft top glow (0..1)."""
    h = size[1]
    ramp = np.linspace(1.0, 0.0, h) if direction == 1 else np.linspace(0.0, 1.0, h)
    return np.tile(ramp[:, None], (1, size[0]))


def render_title_frame(
    title: str,
    size=(1080, 1920),
    accent_color=(255, 214, 0),
    text_color=(255, 255, 255),
) -> np.ndarray:
    """
    Render an RGB(A) title frame (RGBA) with a clean motion-graphic design.

    Design:
      - Centered headline, uppercase, letter-spaced, bold font
      - Soft dark gradient at the top for legibility (NOT a solid box)
      - Thin accent rule under the text (thermometer-style), with a small dot
      - Generated at high-res then downscaled for crisp edges
    """
    w, h = size
    # High-res canvas 2x for antialiasing
    scale = 2
    W, H = w * scale, h * scale

    # Transparent RGBA base
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ---- Letter-spaced uppercase text ----
    font_path = _find_font(bold=True)
    fs = int(64 * scale)
    try:
        font = ImageFont.truetype(font_path, fs)
    except Exception:
        font = ImageFont.load_default()

    lines = _wrap_title(title)
    # Auto-shrink font until widest line fits
    max_text_w = int(W * 0.92)
    while fs > 28 * scale:
        try:
            font = ImageFont.truetype(font_path, fs)
        except Exception:
            break
        widest = 0
        for ln in lines:
            bb = draw.textbbox((0, 0), ln, font=font)
            widest = max(widest, bb[2] - bb[0])
        if widest <= max_text_w:
            break
        fs -= int(4 * scale)

    # Letter spacing
    spacing = int(2 * scale)
    line_h = int(fs * 1.35)

    # Layout: stacked centered
    block_h = len(lines) * line_h
    y0 = int(H * 0.15)  # near top-third
    y = y0

    # Soft top-to-bottom dark gradient for legibility
    grad = np.zeros((H, W, 4), dtype=np.uint8)
    alpha = (np.linspace(1.0, 0.0, H) ** 1.5)[:, None] * 0.55
    grad[..., 0] = 0
    grad[..., 1] = 0
    grad[..., 2] = 0
    grad[..., 3] = (alpha * 255).clip(0, 255).astype(np.uint8)
    grad_img = Image.fromarray(grad, "RGBA")
    img.alpha_composite(grad_img)

    # ---- Draw each text line with letter-spacing ----
    def draw_spaced_text(draw, x_center, y, text, font, fill):
        total_w = 0
        for ch in text:
            bb = draw.textbbox((0, 0), ch, font=font)
            total_w += (bb[2] - bb[0]) + spacing
        x = x_center - total_w // 2
        for ch in text:
            bb = draw.textbbox((0, 0), ch, font=font)
            draw.text((x - bb[0], y - bb[1]), ch, font=font, fill=fill)
            x += (bb[2] - bb[0]) + spacing

    # Soft shadow pass (offset + blur) — draw text on separate layer, blur, composite
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    y_s = y
    for ln in lines:
        draw_spaced_text(sd, W // 2 + int(6 * scale), y_s + int(6 * scale), ln, font, (0, 0, 0, 190))
        y_s += line_h
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=int(6 * scale)))
    img.alpha_composite(shadow_layer)

    # Main text pass
    y_main = y
    for ln in lines:
        draw_spaced_text(draw, W // 2, y_main, ln, font, (*text_color, 255))
        y_main += line_h

    # ---- Accent rule + dot under the text ----
    rule_y = y_main + int(18 * scale)
    accent = (*accent_color, 255)
    # thin wide bar
    bar_w = int(W * 0.30)
    bar_h = int(4 * scale)
    draw.rounded_rectangle(
        [W // 2 - bar_w // 2, rule_y, W // 2 + bar_w // 2, rule_y + bar_h],
        radius=bar_h // 2, fill=accent,
    )
    # small dot at center
    dot_r = int(8 * scale)
    draw.ellipse(
        [W // 2 - dot_r, rule_y - dot_r, W // 2 + dot_r, rule_y + dot_r],
        fill=accent,
    )

    # Downscale to target
    img = img.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(img)


def _ffmpeg_overlay(
    video_path: str,
    png_path: str,
    output_path: str,
    fade: float = 0.35,
    slide: int = 40,
    start_delay: float = 0.0,
) -> str:
    """Composite the title PNG over the video with fade+slide animation."""
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Fade-in using alpha expression; slide up slight offset.
    # overlay enable between (start_delay) and (start_delay+1.2)s
    fade_expr = f"clip((t-{start_delay})/{max(fade,0.1)},0,1)"
    y_expr = f"{slide}*(1-{fade_expr})"  # start lower, slide up to 0

    # Escape every comma inside expression values so ffmpeg's filtergraph keeps
    # them as function args (not option separators). When passing via
    # subprocess (no shell), the string goes straight to ffmpeg.
    def esc(s):
        return s.replace(",", "\\,")

    vf = (
        f"[0:v][1:v]overlay=x=(W-w)/2:y={esc(y_expr)},"
        f"format=yuv420p"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", png_path,
        "-filter_complex", vf,
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-r", "30",
        "-g", "60",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors="ignore")
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"Motion title overlay failed: {error_msg}")
        raise Exception(f"Motion title overlay failed: {error_msg}")


def add_motion_title(
    video_path: str,
    title: str,
    output_path: str,
    accent_color=(255, 214, 0),
) -> str:
    """
    Add a professional motion-graphic title to a clip.

    Returns the output path. Raises on failure so the caller can fall back.
    """
    try:
        import numpy as _np  # noqa
    except Exception as e:
        job_error(0, f"numpy not available for motion title: {e}")
        raise

    job_info(0, f"Rendering motion title: {title!r}")
    frame = render_title_frame(title, accent_color=accent_color)
    png_path = output_path + ".title.png"
    try:
        Image.fromarray(frame, "RGBA").save(png_path)
        result = _ffmpeg_overlay(video_path, png_path, output_path)
        job_info(0, f"Motion title added: {output_path}")
        return result
    finally:
        try:
            if os.path.exists(png_path):
                os.remove(png_path)
        except OSError:
            pass