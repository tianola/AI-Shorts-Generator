"""
Thumbnail generation service.
Extracts representative frames and adds text overlays.

Supports 3 visual templates that rotate per clip_index % 3:
  0 — Neon Glow    : darkened frame + colored glow text
  1 — Bold Split   : color-blocked top zone + white text inside block
  2 — Minimal Clean: frosted semi-transparent bar + clean white text
"""
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont, ImageEnhance, ImageFilter
from config import settings
from utils.logger import job_info, job_error, job_warning


def _probe_duration(video_path: str) -> float:
    """Return video duration in seconds (0.0 on failure)."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return 0.0


def _wrap_title(text: str, max_chars: int = 12, max_lines: int = 4) -> str:
    """Wrap + truncate a title so it fits on a thumbnail without overflowing."""
    words = str(text or "Viral Moment").split()
    lines, current = [], []
    for word in words:
        current.append(word)
        if len(" ".join(current)) > max_chars:
            lines.append(" ".join(current[:-1]))
            current = [word]
    if current:
        lines.append(" ".join(current))
    # Truncate to max_lines with ellipsis
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max_chars - 1].rstrip() + "..."
    return "\n".join(lines) if lines else "Viral Moment"


def _load_font(font_path: str, size: int) -> ImageFont.FreeTypeFont:
    """Load a TrueType font, falling back to default on failure."""
    try:
        return ImageFont.truetype(font_path, size)
    except (IOError, OSError):
        return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, font_path: str, text: str,
              initial_size: int, max_width: int) -> Tuple[ImageFont.FreeTypeFont, int, int]:
    """Auto-shrink font until the widest line fits max_width.
    Returns (font, text_width, text_height).
    """
    fs = initial_size
    while fs > 40:
        font = _load_font(font_path, fs)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
        except AttributeError:
            tw, th = font.getsize(text)  # type: ignore[attr-defined]
        if tw <= max_width:
            return font, tw, th
        fs -= 4
    font = _load_font(font_path, fs)
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return font, bbox[2] - bbox[0], bbox[3] - bbox[1]
    except AttributeError:
        tw, th = font.getsize(text)  # type: ignore[attr-defined]
        return font, tw, th


def _get_dominant_color(img: Image.Image) -> Tuple[int, int, int]:
    """Extract the dominant color from an image (darkened 60% for contrast)."""
    try:
        small = img.convert("RGB").resize((50, 50))
        quantized = small.quantize(1).convert("RGB")
        r, g, b = quantized.getpixel((0, 0))
        # Darken by 60% so text stays readable
        return (int(r * 0.4), int(g * 0.4), int(b * 0.4))
    except Exception:
        return (20, 20, 60)


# ---------------------------------------------------------------------------
# Template implementations
# ---------------------------------------------------------------------------

def _apply_neon_glow(img: Image.Image, display_text: str, font_path: str,
                     font_size: int, job_id: int) -> Image.Image:
    """
    Template 0 — Neon Glow
    Darkened frame + hot-pink or cyan glow text, multi-layer shadow.
    Alternates pink/cyan by job_id parity.
    """
    # Darken the background frame
    img = ImageEnhance.Brightness(img.convert("RGB")).enhance(0.55)
    draw = ImageDraw.Draw(img)

    margin = 60
    max_width = img.width - margin * 2
    font, text_width, text_height = _fit_font(draw, font_path, display_text, font_size, max_width)

    x = (img.width - text_width) // 2
    y = margin

    # Neon color: hot-pink or cyan alternating by job_id parity
    neon_color = (255, 45, 120) if (job_id % 2 == 0) else (0, 230, 255)
    shadow_color = (10, 10, 30)

    # Multi-layer glow (large offsets, soft shadow)
    for radius in [8, 6, 4]:
        for dx, dy in [(-radius, -radius), (-radius, radius),
                       (radius, -radius), (radius, radius),
                       (0, -radius), (0, radius),
                       (-radius, 0), (radius, 0)]:
            draw.multiline_text((x + dx, y + dy), display_text,
                                font=font, fill=shadow_color, align="center")

    # Main neon text
    draw.multiline_text((x, y), display_text, font=font, fill=neon_color, align="center")
    return img


def _apply_bold_split(img: Image.Image, display_text: str, font_path: str,
                      font_size: int) -> Image.Image:
    """
    Template 1 — Bold Split
    Top 32% filled with dominant color block, white text centered inside block.
    Bottom 68% is the original frame.
    """
    img = img.convert("RGB")
    dominant = _get_dominant_color(img)
    draw = ImageDraw.Draw(img)

    split_y = int(img.height * 0.32)

    # Fill top block
    draw.rectangle([(0, 0), (img.width, split_y)], fill=dominant)

    # Fit text inside the block area
    block_max_width = img.width - 80
    font, text_width, text_height = _fit_font(draw, font_path, display_text,
                                              font_size, block_max_width)

    x = (img.width - text_width) // 2
    # Vertically center text within the block
    y = (split_y - text_height) // 2
    y = max(16, y)

    # Thin black outline for legibility
    for dx, dy in [(-2, -2), (-2, 2), (2, -2), (2, 2)]:
        draw.multiline_text((x + dx, y + dy), display_text,
                            font=font, fill=(0, 0, 0), align="center")

    # White text
    draw.multiline_text((x, y), display_text, font=font,
                        fill=(255, 255, 255), align="center")
    return img


def _apply_minimal_clean(img: Image.Image, display_text: str, font_path: str,
                          font_size: int) -> Image.Image:
    """
    Template 2 — Minimal Clean
    Frosted semi-transparent bar behind text, no outline, clean white text.
    """
    img = img.convert("RGBA")
    draw_probe = ImageDraw.Draw(img)

    margin = 80
    bar_padding = 24
    max_width = img.width - margin * 2
    font, text_width, text_height = _fit_font(draw_probe, font_path, display_text,
                                              font_size, max_width)

    x = (img.width - text_width) // 2
    y = margin

    # Build frosted bar as separate RGBA layer
    bar_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    bar_draw = ImageDraw.Draw(bar_layer)

    bar_x0 = 0
    bar_y0 = y - bar_padding
    bar_x1 = img.width
    bar_y1 = y + text_height + bar_padding

    # Draw filled bar with alpha=160 (semi-transparent)
    bar_draw.rectangle([(bar_x0, bar_y0), (bar_x1, bar_y1)], fill=(0, 0, 0, 160))

    # Composite bar onto image
    img = Image.alpha_composite(img, bar_layer)

    # Draw text on top (no outline for the clean look)
    draw = ImageDraw.Draw(img)
    draw.multiline_text((x, y), display_text, font=font,
                        fill=(255, 255, 255, 255), align="center")

    return img.convert("RGB")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_frame(
    video_path: str,
    output_path: str,
    timestamp: float = 1.0,
    width: int = 1080,
    height: int = 1920
) -> str:
    """
    Extract a frame from a video at a specific timestamp.

    Args:
        video_path: Path to the video file
        output_path: Path to save the extracted frame
        timestamp: Time in seconds to extract the frame
        width: Target width of the frame
        height: Target height of the frame

    Returns:
        Path to the extracted frame

    Raises:
        Exception: If frame extraction fails
    """
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Clamp timestamp to a safe range so short clips (or past-EOF values) don't
    # produce an empty/black frame.
    duration = _probe_duration(video_path)
    ts = max(0.1, min(float(timestamp), duration - 0.1 if duration > 0.3 else 0.1))

    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-ss", str(ts),
        "-vframes", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
        "-q:v", "2",
        output_path
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Frame extracted to {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        raise Exception(f"Frame extraction failed: {error_msg}")


def add_text_overlay(
    image_path: str,
    output_path: str,
    text: str,
    font_size: int = 120,
    font_path: str = None,
    template_index: int = 0,
    job_id: int = 0,
) -> str:
    """
    Add text overlay to an image using one of 3 visual templates.

    Args:
        image_path:     Path to the input image
        output_path:    Path to save the output image
        text:           Text to overlay
        font_size:      Base font size in pixels (auto-shrinks to fit)
        font_path:      Path to font file (defaults to Montserrat ExtraBold)
        template_index: Visual style (0=Neon Glow, 1=Bold Split, 2=Minimal Clean)
        job_id:         Used for neon color alternation in template 0

    Returns:
        Path to the output image

    Raises:
        Exception: If text overlay fails
    """
    if font_path is None:
        from services.font_resolve import find_font
        font_path = find_font(bold=True)
        if not font_path:
            _fallback = os.path.join(settings.FONTS_DIR, "Montserrat-ExtraBold.ttf")
            font_path = _fallback if os.path.exists(_fallback) else ""

    try:
        img = Image.open(image_path)
        display_text = _wrap_title(text)

        # Warn if font is missing (templates will fall back to default)
        if not font_path or not os.path.exists(font_path):
            job_warning(job_id, f"Font not found at '{font_path}', using default")
            font_path = ""

        template = template_index % 3

        if template == 0:
            result = _apply_neon_glow(img, display_text, font_path,
                                      font_size=108, job_id=job_id)
        elif template == 1:
            result = _apply_bold_split(img, display_text, font_path,
                                       font_size=96)
        else:  # template == 2
            result = _apply_minimal_clean(img, display_text, font_path,
                                          font_size=104)

        result.save(output_path, quality=95)
        job_info(job_id, f"Thumbnail template {template} applied → {output_path}")
        return output_path

    except Exception as e:
        raise Exception(f"Text overlay failed: {str(e)}")


def generate_thumbnail(
    video_path: str,
    output_path: str,
    title: str,
    timestamp: float = 1.0,
    clip_index: int = 0,
    job_id: int = 0,
) -> Optional[str]:
    """
    Generate a thumbnail for a clip.

    Args:
        video_path:  Path to the video file
        output_path: Path to save the thumbnail
        title:       Title text to overlay
        timestamp:   Time in seconds to extract the frame (default: 1s)
        clip_index:  Index of the clip in the batch; drives template_index = clip_index % 3
        job_id:      Job identifier (used for neon color alternation)

    Returns:
        Path to the generated thumbnail, or None if generation fails

    Note:
        Best-effort: logs and returns None on failure without raising.
    """
    try:
        temp_frame = output_path.replace(".jpg", "_temp.jpg")
        extract_frame(video_path, temp_frame, timestamp)

        add_text_overlay(
            temp_frame,
            output_path,
            title,
            template_index=clip_index % 3,
            job_id=job_id,
        )

        if os.path.exists(temp_frame):
            os.remove(temp_frame)

        return output_path

    except Exception as e:
        job_error(job_id, f"Thumbnail generation failed: {str(e)}")
        return None
