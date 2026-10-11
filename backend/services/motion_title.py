"""
Motion-graphic title overlay service (PRO edition).

Replaces the old "grey box + static band" with a full-bleed, professional
motion-graphic title card that stays visible for the ENTIRE clip duration:

  - Full-frame 1080x1920 layout: hard dark gradient backdrop (no grey box),
    big extra-bold uppercase headline (wrapped to 2 lines), thin gold accent
    rules at 4 points, small center diamond.
  - Animated ENTRANCE with ffmpeg eval=frame:
      * scale-pop:  0.85 -> 1.10 -> 1.00 (grow with overshoot)
      * fade-in:    0 -> 255 alpha over the first ~0.45s
      * slide-up:   starts slightly lower, settles to center
    The PNG is fed with -loop 1 so it never disappears after the first frame;
    overlay=shortest=1 keeps the video length unchanged (audio preserved).
  - Slight continuous "breathing" (periodic scale oscillation) keeps it
    feeling alive without being distracting.

Two call paths:
  1. add_motion_title(video, title, output)   -> full ffmpeg run (legacy path)
  2. render_title_frame(title) -> RGBA ndarray that callers (render_single_pass)
     turn into a .png and composite with their own overlay animation.
"""

import os
import subprocess
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageFilter

from config import settings
from utils.logger import job_info, job_error


# ---------------------------------------------------------------------------
# Fonts
# ---------------------------------------------------------------------------
def _find_font(bold: bool = True) -> str:
    try:
        from services.font_resolve import find_font
        path = find_font(bold=bold)
        if path:
            return path
    except Exception:
        pass
    for cand in [
        r"C:\Windows\Fonts\impact.ttf",
        r"C:\Windows\Fonts\arialbd.ttf",
        "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]:
        if os.path.isfile(cand):
            return cand
    return ""


def _wrap_title(title: str, max_chars: int = 22, max_lines: int = 2) -> list:
    """Wrap + truncate a title into at most `max_lines` clean uppercase lines."""
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
    if current:
        lines.append(" ".join(current))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        if len(lines[-1]) > max_chars - 1:
            lines[-1] = lines[-1][: max_chars - 1].rstrip() + "..."
    if not lines:
        lines = ["VIRAL MOMENT"]
    return lines


def _make_backdrop(size=(1080, 1920)) -> Image.Image:
    """Full-frame transparent backdrop — zero dark tint so original video stays bright."""
    return Image.new("RGBA", size, (0, 0, 0, 0))


def render_title_frame(
    title: str,
    size=(1080, 1920),
    accent_color=(255, 255, 255),
    text_color=(255, 255, 255),
    include_backdrop: bool = False,
    source_channel: str = None,
    watermark_handle: str = None,
) -> np.ndarray:
    """
    Render a clean, compact RGBA title card with branding watermark (1080x1920).

    Design:
      - ZERO dark background overlay — keeps original video colors 100% bright and vibrant.
      - Compact, safe font size (46-48pt) centered in the upper safe zone (never overflows).
      - Crisp bold typography with smooth dark outline + soft shadow for legibility.
      - Subtle neon green accent lines.
      - Discreet brand watermark pill: circular avatar logo + settings.WATERMARK_HANDLE.
      - Discreet source credit label when source_channel is provided.
    """
    w, h = size
    scale = 2
    W, H = w * scale, h * scale

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    font_path = _find_font(bold=True)
    fs = int(58 * scale)
    try:
        font = ImageFont.truetype(font_path, fs)
    except Exception:
        font = ImageFont.load_default()

    lines = _wrap_title(title, max_chars=22, max_lines=2)
    max_text_w = int(W * 0.84)
    while fs > 28 * scale:
        try:
            font = ImageFont.truetype(font_path, fs)
        except Exception:
            break
        widest = max(draw.textbbox((0, 0), ln, font=font)[2] - draw.textbbox((0, 0), ln, font=font)[0] for ln in lines)
        if widest <= max_text_w:
            break
        fs -= int(4 * scale)

    line_h = int(fs * 1.25)
    y0 = int(H * 0.11)  # High upper frame (~11% from top) — prominent, larger, cleanly below top bezel

    # Soft drop shadow pass
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    y_curr = y0
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font)
        tw = bb[2] - bb[0]
        x = (W - tw) // 2
        sd.text((x + int(4 * scale), y_curr + int(4 * scale)), ln, font=font, fill=(0, 0, 0, 210))
        y_curr += line_h
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=int(6 * scale)))
    img.alpha_composite(shadow_layer)

    # Crisp text outline
    outline_w = int(4 * scale)
    y_curr = y0
    for ln in lines:
        bb = draw.textbbox((0, 0), ln, font=font)
        tw = bb[2] - bb[0]
        x = (W - tw) // 2
        for dx in range(-outline_w, outline_w + 1, 2):
            for dy in range(-outline_w, outline_w + 1, 2):
                if dx * dx + dy * dy <= outline_w * outline_w:
                    draw.text((x + dx, y_curr + dy), ln, font=font, fill=(0, 0, 0, 255))
        draw.text((x, y_curr), ln, font=font, fill=(*text_color, 255))
        y_curr += line_h

    # Two minimal neon green fading accent lines from center outward
    cx = W // 2
    accent_y = y_curr + int(6 * scale)
    gap = int(12 * scale)
    line_len = int(140 * scale)
    line_th = max(2, int(2.5 * scale))

    # Refined minimal neon green (#22C55E / Emerald Neon, balanced & aesthetic)
    neon_green = (34, 197, 94)

    # Accent lines layer
    accent_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    adraw = ImageDraw.Draw(accent_layer)

    for d in range(line_len):
        prog = d / float(line_len)
        alpha = int(190 * max(0.0, 1.0 - (prog ** 1.25)))
        col = (*neon_green, alpha)

        lx = cx - gap - d
        adraw.line([(lx, accent_y), (lx, accent_y + line_th)], fill=col, width=1)

        rx = cx + gap + d
        adraw.line([(rx, accent_y), (rx, accent_y + line_th)], fill=col, width=1)

    line_shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    lsdraw = ImageDraw.Draw(line_shadow)
    for d in range(line_len):
        prog = d / float(line_len)
        alpha = int(130 * max(0.0, 1.0 - (prog ** 1.25)))
        col = (0, 0, 0, alpha)
        lx = cx - gap - d
        rx = cx + gap + d
        lsdraw.line([(lx, accent_y + int(2 * scale)), (lx, accent_y + line_th + int(2 * scale))], fill=col, width=1)
        lsdraw.line([(rx, accent_y + int(2 * scale)), (rx, accent_y + line_th + int(2 * scale))], fill=col, width=1)

    line_shadow = line_shadow.filter(ImageFilter.GaussianBlur(radius=int(3 * scale)))
    img.alpha_composite(line_shadow)
    img.alpha_composite(accent_layer)

    # -----------------------------------------------------------------------
    # PRO CENTERED WATERMARK & BRANDING PILL (settings.WATERMARK_HANDLE + Logo Avatar)
    # -----------------------------------------------------------------------
    try:
        wm_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        wdraw = ImageDraw.Draw(wm_layer)

        # Look for logo file
        logo_cand_paths = [
            os.path.join(settings.STORAGE_ROOT, "assets", "logo.png"),
            os.path.join(os.path.dirname(__file__), "..", "static", "logo.png"),
            "E:/01-AI-PROJECTS/ai-shorts-generator/storage/assets/logo.png"
        ]
        found_logo_path = None
        for cand in logo_cand_paths:
            if os.path.isfile(cand):
                found_logo_path = cand
                break

        pill_font_size = int(14 * scale)
        sub_font_size = int(12 * scale)
        try:
            pill_font = ImageFont.truetype(font_path, pill_font_size)
            sub_font = ImageFont.truetype(font_path, sub_font_size)
        except Exception:
            pill_font = ImageFont.load_default()
            sub_font = ImageFont.load_default()

        wm_text = watermark_handle or settings.WATERMARK_HANDLE
        # Truncate if too long
        while True:
            h_box = wdraw.textbbox((0, 0), wm_text, font=pill_font)
            h_w = h_box[2] - h_box[0]
            if h_w < int(W * 0.45) or len(wm_text) <= 5:
                break
            wm_text = wm_text[:-1] + "..."
            
        h_h = h_box[3] - h_box[1]

        s_text = ""
        s_w = 0
        if source_channel and str(source_channel).strip():
            clean_src = str(source_channel).strip()
            if not clean_src.startswith("@"):
                clean_src = f"@{clean_src}"
            s_text = f" • Original: {clean_src[:20]}"
            s_box = wdraw.textbbox((0, 0), s_text, font=sub_font)
            s_w = s_box[2] - s_box[0]

        avatar_size = int(24 * scale)
        pad_x = int(10 * scale)
        capsule_w = pad_x + avatar_size + int(8 * scale) + h_w + s_w + pad_x
        # Max width cap to prevent overflow
        max_cap_w = int(W * 0.88)
        if capsule_w > max_cap_w:
            capsule_w = max_cap_w

        capsule_h = avatar_size + int(10 * scale)

        # Center horizontally in the top safe zone (above title)
        bx = (W - capsule_w) // 2
        by = int(50 * scale)  # Y = 100px in 1080x1920

        # Capsule Drop Shadow
        sd_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sdraw = ImageDraw.Draw(sd_layer)
        sdraw.rounded_rectangle(
            [bx, by + int(3 * scale), bx + capsule_w, by + capsule_h + int(3 * scale)],
            radius=capsule_h // 2,
            fill=(0, 0, 0, 175)
        )
        sd_layer = sd_layer.filter(ImageFilter.GaussianBlur(radius=int(5 * scale)))
        img.alpha_composite(sd_layer)

        # Subtle dark glass capsule with delicate translucent border
        wdraw.rounded_rectangle(
            [bx, by, bx + capsule_w, by + capsule_h],
            radius=capsule_h // 2,
            fill=(14, 14, 20, 215),
            outline=(255, 255, 255, 55),
            width=max(1, int(1.2 * scale))
        )

        # Paste circular logo avatar
        if found_logo_path:
            logo_img = Image.open(found_logo_path).convert("RGBA")
            logo_img = logo_img.resize((avatar_size, avatar_size), Image.Resampling.LANCZOS)
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse((0, 0, avatar_size, avatar_size), fill=255)
            wm_layer.paste(logo_img, (bx + pad_x, by + (capsule_h - avatar_size) // 2), mask)

        # Handle text
        tx = bx + pad_x + avatar_size + int(8 * scale)
        ty = by + (capsule_h - h_h) // 2 - int(2 * scale)
        wdraw.text((tx, ty), wm_text, font=pill_font, fill=(255, 255, 255, 245))

        # Source text
        if s_text:
            wdraw.text((tx + h_w, ty + int(1 * scale)), s_text, font=sub_font, fill=(203, 213, 225, 200))

        img.alpha_composite(wm_layer)
    except Exception as e:
        logger.warning(f"Could not render watermark badge: {e}")

    img = img.resize(size, Image.Resampling.LANCZOS)
    return np.asarray(img)


# ---------------------------------------------------------------------------
# ffmpeg overlay with animated entrance that STAYS for the whole clip
# ---------------------------------------------------------------------------
def _ffmpeg_overlay(
    video_path: str,
    png_path: str,
    output_path: str,
    fade: float = 0.45,
    slide: int = 36,
    pop_scale: float = 1.12,
    start_delay: float = 0.0,
    breathe: bool = True,
) -> str:
    """Composite the title PNG over the video with a scale-pop + fade entrance,
    then keep it visible for the FULL clip duration.

    The PNG is looped (-loop 1) and overlay uses shortest=1 (video drives the
    length). The alpha/scale expressions use eval=frame so they animate over
    time. After the entrance the scale gently oscillates (breathing) so the
    title feels alive instead of frozen.
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Entrance: 0..1 ramp over `fade` seconds
    fade_expr = f"clip((t-{start_delay})/{max(fade, 0.1)},0,1)"
    # Slide-up: start lower, settle to 0
    y_expr = f"{slide}*(1-{fade_expr})"
    # Scale-pop with overshoot: 0.9 -> pop_scale -> 1.0
    if breathe:
        scale_expr = (
            f"0.9+0.10*({fade_expr})"
            f"+({pop_scale}-1.0)*sin(clip((t-{start_delay})/0.6,0,1)*PI/1.5)"
            f"+0.015*sin(2*PI*t/2.4)"
        )
    else:
        scale_expr = (
            f"0.9+0.10*({fade_expr})"
            f"+({pop_scale}-1.0)*sin(clip((t-{start_delay})/0.6,0,1)*PI/1.5)"
        )

    def esc(s):
        return s.replace(",", "\\,")

    # scale with eval=frame re-reads iw/ih each frame, then fade alpha in
    filter_complex = (
        f"[1:v]scale=w='iw*({scale_expr})':h='ih*({scale_expr})':eval=frame,"
        f"fade=t=in:st={start_delay}:d={fade}:alpha=1[ttl];"
        f"[0:v][ttl]overlay=x=(W-w)/2:y={esc(y_expr)}:shortest=1:format=auto[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-loop", "1", "-i", png_path,
        "-filter_complex", filter_complex,
        "-map", "[v]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-r", "30",
        "-g", "60",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-color_range", "pc",
        "-colorspace", "bt709",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
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
    Add a full-frame professional motion-graphic title that stays for the whole
    clip. Returns the output path. Raises on failure so the caller can fall back.
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