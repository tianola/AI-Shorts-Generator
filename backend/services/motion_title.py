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
from utils.logger import job_info, job_error, logger


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


def _headline_upper(text: str) -> str:
    """Uppercase the way Greek is set in capitals: without the tonos.

    str.upper() turns "ερώτηση" into "ΕΡΏΤΗΣΗ"; capitals do not carry the accent,
    and on a headline the stray mark reads as a typo. The dialytika is kept.
    """
    import unicodedata

    decomposed = unicodedata.normalize("NFD", text.upper())
    return unicodedata.normalize("NFC", decomposed.replace("́", ""))


def _wrap_title(title: str, max_chars: int = 22, max_lines: int = 2) -> list:
    """Wrap + truncate a title into at most `max_lines` clean uppercase lines."""
    words = _headline_upper(str(title or "Viral Moment").strip()).split()
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
        dropped = " ".join(lines[max_lines:]).strip()
        lines = lines[:max_lines]
        # Words past the line budget used to vanish silently, so the published
        # frame read "SPACED PRACTICE / BEATS A 5-HOUR" for a title ending in
        # "CRAM". Show the cut instead.
        if dropped:
            budget = max_chars - 4
            if len(lines[-1]) > budget:
                lines[-1] = lines[-1][:budget].rstrip()
            lines[-1] = (lines[-1] + " ...").strip()
        elif len(lines[-1]) > max_chars - 1:
            lines[-1] = lines[-1][: max_chars - 1].rstrip() + "..."
    if not lines:
        lines = ["VIRAL MOMENT"]
    return lines


# Headline type: point sizes at 1080 px width, and the colour of the tease word.
HEADLINE_MAX_PT = 92
HEADLINE_MIN_PT = 46
HEADLINE_HIGHLIGHT = (255, 229, 0)

# Heavy display faces, first hit wins. The bundled Montserrat has no Greek
# glyphs and PIL does not fall back, so it is only offered to a Latin headline;
# the Windows faces after it all carry Greek.
_HEADLINE_FONTS_LATIN_ONLY = [
    os.path.join(os.path.dirname(__file__), "..", "fonts", "Montserrat-ExtraBold.ttf"),
    "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
]
_HEADLINE_FONTS = [
    r"C:\Windows\Fonts\seguibl.ttf",   # Segoe UI Black
    r"C:\Windows\Fontsriblk.ttf",    # Arial Black
    r"C:\Windows\Fonts\impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _headline_font_path(text: str) -> str:
    """The heaviest available face that can actually draw this headline."""
    latin_only = all(ord(ch) < 0x250 for ch in text)
    candidates = (_HEADLINE_FONTS_LATIN_ONLY if latin_only else []) + _HEADLINE_FONTS
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    return ""


def _widest_line(lines: list, font, space_w: float) -> float:
    return max(
        sum(font.getlength(word) for word in line) + space_w * (len(line) - 1)
        for line in lines
    )


def _balanced_lines(words: list, line_count: int, font, space_w: float) -> list:
    """Split words into `line_count` lines whose widest line is as narrow as possible.

    Greedy wrapping fills the first lines and leaves whatever is left on the
    last, which is how "THINK IF YOU / REALLY THINK ABOUT / IT" happened. Trying
    every break position (a headline is under a dozen words) gives lines of
    even length instead.
    """
    from itertools import combinations

    line_count = max(1, min(line_count, len(words)))
    if line_count == 1:
        return [list(words)]
    best, best_width = None, None
    for breaks in combinations(range(1, len(words)), line_count - 1):
        edges = (0,) + breaks + (len(words),)
        candidate = [words[edges[i]:edges[i + 1]] for i in range(line_count)]
        width = _widest_line(candidate, font, space_w)
        if best_width is None or width < best_width:
            best, best_width = candidate, width
    return best


def _highlight_words(words: list) -> set:
    """The word(s) drawn in the accent colour: the concrete thing being teased.

    A number is the hook of a headline that has one ("40,000", "3", "90%").
    Without a number the longest word is the best cheap guess at the subject.
    """
    numbers = {word for word in words if any(ch.isdigit() for ch in word)}
    if numbers:
        return numbers
    longest = max(words, key=len, default="")
    return {longest} if len(longest) >= 5 else set()


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

    # The wrap budget (18 chars, three lines) still decides what is shown and
    # marks a cut with "..."; the line breaks themselves are then rebalanced
    # below so a headline never ends on a one-word orphan line.
    words = " ".join(_wrap_title(title, max_chars=18, max_lines=3)).split()
    font_path = _headline_font_path(" ".join(words)) or _find_font(bold=True)
    max_text_w = int(W * 0.90)

    font = ImageFont.load_default()
    lines = [words]
    fs = int(HEADLINE_MAX_PT * scale)
    # Largest type that fits, using more lines before giving up size: a headline
    # is read in the first second, so it is as big as the frame allows.
    while True:
        try:
            font = ImageFont.truetype(font_path, fs)
        except Exception:
            break
        space_w = font.getlength(" ")
        fitted = None
        for line_count in range(1, min(3, len(words)) + 1):
            candidate = _balanced_lines(words, line_count, font, space_w)
            if _widest_line(candidate, font, space_w) <= max_text_w:
                fitted = candidate
                break
        if fitted is not None or fs <= HEADLINE_MIN_PT * scale:
            lines = fitted or _balanced_lines(words, min(3, len(words)), font, space_w)
            break
        fs -= int(4 * scale)

    space_w = font.getlength(" ") if hasattr(font, "getlength") else fs * 0.3
    line_h = int(fs * 1.16)
    y0 = int(H * 0.085)
    stroke_w = max(2, int(fs * 0.085))
    highlight = _highlight_words(words)

    def _line_positions(line):
        """(word, x) for one centred line."""
        width = sum(font.getlength(word) for word in line) + space_w * (len(line) - 1)
        x = (W - width) / 2.0
        placed = []
        for word in line:
            placed.append((word, x))
            x += font.getlength(word) + space_w
        return placed

    # Soft drop shadow under the whole block, so the type lifts off bright footage.
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    y_curr = y0
    for line in lines:
        for word, x in _line_positions(line):
            sd.text((x + int(3 * scale), y_curr + int(5 * scale)), word, font=font,
                    fill=(0, 0, 0, 200), stroke_width=stroke_w, stroke_fill=(0, 0, 0, 200))
        y_curr += line_h
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=int(7 * scale)))
    img.alpha_composite(shadow_layer)

    # White extra-bold type with a heavy black stroke; the one word that carries
    # the tease is yellow. No rim, no rules, no ornament - the words are the design.
    y_curr = y0
    for line in lines:
        for word, x in _line_positions(line):
            fill = HEADLINE_HIGHLIGHT if word in highlight else text_color
            draw.text((x, y_curr), word, font=font, fill=(*fill, 255),
                      stroke_width=stroke_w, stroke_fill=(0, 0, 0, 255))
        y_curr += line_h

    # -----------------------------------------------------------------------
    # PRO CENTERED WATERMARK & BRANDING PILL (settings.WATERMARK_HANDLE + Logo Avatar)
    # -----------------------------------------------------------------------
    if settings.WATERMARK_ENABLED:
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
            avatar_size = int(24 * scale)
            pad_x = int(10 * scale)

            # Right edge, vertically centred: the top strip belongs to the headline.
            # Capped narrower than the old top-centre pill because at mid-height it
            # overlaps the action instead of the empty sky.
            max_cap_w = int(W * 0.60)
            text_budget = max_cap_w - (2 * pad_x + avatar_size + int(8 * scale))

            # Truncate to the shared budget so the text can never spill past the
            # capsule edge the way it did when only the handle was measured.
            h_box = wdraw.textbbox((0, 0), wm_text, font=pill_font)
            h_w = h_box[2] - h_box[0]
            while h_w > text_budget and len(wm_text) > 5:
                wm_text = wm_text[:-1] + "..."
                h_box = wdraw.textbbox((0, 0), wm_text, font=pill_font)
                h_w = h_box[2] - h_box[0]
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
                # The handle is the branding; the source credit gives way first.
                while s_w > text_budget - h_w and len(s_text) > 12:
                    s_text = s_text[:-4] + "..."
                    s_box = wdraw.textbbox((0, 0), s_text, font=sub_font)
                    s_w = s_box[2] - s_box[0]

            capsule_w = pad_x + avatar_size + int(8 * scale) + h_w + s_w + pad_x
            capsule_h = avatar_size + int(10 * scale)

            bx = W - capsule_w - int(28 * scale)
            by = int(H * 0.5) - capsule_h // 2

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
    # Scale-pop with overshoot: 0.9 -> pop_scale -> 1.0. The sin() arc has to
    # reach PI to land back on zero; a shorter phase leaves the card oversized
    # for the rest of the clip (see build_single_pass_filter).
    if breathe:
        scale_expr = (
            f"0.9+0.10*({fade_expr})"
            f"+({pop_scale}-1.0)*sin(clip((t-{start_delay})/0.6,0,1)*PI)"
            f"+0.015*sin(2*PI*t/2.4)"
        )
    else:
        scale_expr = (
            f"0.9+0.10*({fade_expr})"
            f"+({pop_scale}-1.0)*sin(clip((t-{start_delay})/0.6,0,1)*PI)"
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