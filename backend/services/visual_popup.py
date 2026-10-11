"""
Visual Pop-up Service for Modern Vertical Short Videos.
Generates polished Pillow visual cards with rounded corners, drop shadows,
and builds FFmpeg spring-animated scale-pop filtergraphs positioned safely in the upper quadrant.
"""
import os
from typing import Tuple, Dict, Any, Optional
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from loguru import logger


def create_styled_popup_card(
    raw_image_path: str,
    output_path: str,
    target_size: Tuple[int, int] = (480, 340),
    radius: int = 24,
    border_width: int = 4,
    border_color: Tuple[int, int, int, int] = (255, 255, 255, 240),
    shadow_radius: int = 14,
    shadow_offset: Tuple[int, int] = (0, 10),
    shadow_color: Tuple[int, int, int, int] = (0, 0, 0, 150),
) -> str:
    """
    Renders a raw photo or meme into a studio-quality visual card:
    - High-quality lanczos cropping to target aspect ratio
    - Antialiased rounded corners
    - Clean solid border
    - Soft Gaussian drop shadow on expanded canvas
    
    Returns:
        output_path: Absolute or relative path to generated PNG card.
    """
    if not os.path.isfile(raw_image_path):
        raise FileNotFoundError(f"Raw image not found: {raw_image_path}")

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    with Image.open(raw_image_path) as raw:
        # 1. Fit and crop raw image to target dimensions
        img = ImageOps.fit(raw.convert("RGBA"), target_size, Image.Resampling.LANCZOS)

        # 2. Apply rounded corner alpha mask
        mask = Image.new("L", target_size, 0)
        draw_mask = ImageDraw.Draw(mask)
        draw_mask.rounded_rectangle([(0, 0), target_size], radius=radius, fill=255)
        img.putalpha(mask)

        # 3. Add crisp border
        if border_width > 0:
            draw_img = ImageDraw.Draw(img)
            draw_img.rounded_rectangle(
                [
                    (border_width // 2, border_width // 2),
                    (target_size[0] - border_width // 2, target_size[1] - border_width // 2),
                ],
                radius=radius,
                outline=border_color,
                width=border_width,
            )

        # 4. Canvas with padding for drop shadow
        pad = shadow_radius * 2 + abs(shadow_offset[1])
        canvas_size = (target_size[0] + pad * 2, target_size[1] + pad * 2)
        canvas = Image.new("RGBA", canvas_size, (0, 0, 0, 0))

        # 5. Render Gaussian Drop Shadow
        shadow_mask = Image.new("RGBA", canvas_size, (0, 0, 0, 0))
        draw_shadow = ImageDraw.Draw(shadow_mask)
        sx = pad + shadow_offset[0]
        sy = pad + shadow_offset[1]
        draw_shadow.rounded_rectangle(
            [(sx, sy), (sx + target_size[0], sy + target_size[1])],
            radius=radius,
            fill=shadow_color,
        )
        shadow_blurred = shadow_mask.filter(ImageFilter.GaussianBlur(shadow_radius))

        # 6. Composite shadow and card
        canvas.paste(shadow_blurred, (0, 0), shadow_blurred)
        canvas.paste(img, (pad, pad), img)

        canvas.save(output_path, "PNG")
        logger.debug(f"Created styled visual popup card: {output_path} (size: {canvas_size})")
        return output_path


def build_popup_filtergraph_fragment(
    input_idx: int,
    start_time: float,
    duration: float = 2.0,
    position: str = "upper_third",
) -> Tuple[str, str, str]:
    """
    Constructs FFmpeg filter definitions for a smooth spring scale-pop animation.

    Args:
        input_idx: The FFmpeg input stream index for the popup PNG.
        start_time: Start timestamp in seconds.
        duration: Duration of popup visibility in seconds (clamped 1.5-3.0s).
        position: Placement preset ('upper_third' center or 'upper_right').

    Returns:
        Tuple of (prep_filter, anim_label, overlay_expr)
    """
    t_start = round(max(0.0, float(start_time)), 3)
    t_dur = round(max(1.0, min(5.0, float(duration))), 3)
    t_end = round(t_start + t_dur, 3)
    t_settle = round(t_start + 0.15, 3)
    t_fadeout = round(max(t_start, t_end - 0.18), 3)

    # Spring overshoot scale expression: rapid scale up to 1.12x then settle to 1.00x
    # These go inside '...' quoting, where a comma is already literal — a
    # backslash there becomes part of the expression and breaks it.
    scale_expr = (
        f"iw*(0.85+0.27*clip((t-{t_start})/0.15,0,1)-0.12*clip((t-{t_settle})/0.08,0,1))"
    )
    scale_expr_h = (
        f"ih*(0.85+0.27*clip((t-{t_start})/0.15,0,1)-0.12*clip((t-{t_settle})/0.08,0,1))"
    )

    anim_label = f"popup_anim_{input_idx}"
    prep_filter = (
        f"[{input_idx}:v]format=rgba,"
        f"scale=w='{scale_expr}':h='{scale_expr_h}':eval=frame,"
        f"fade=t=in:st={t_start}:d=0.15:alpha=1,"
        f"fade=t=out:st={t_fadeout}:d=0.18:alpha=1[{anim_label}]"
    )

    if position == "upper_right":
        x_expr = f"W-w-50+80*(1-clip((t-{t_start})/0.18,0,1))"
        y_expr = "320"
    else:  # upper_third (center)
        x_expr = "(W-w)/2"
        y_expr = f"290+25*(1-clip((t-{t_start})/0.18,0,1))"

    overlay_expr = (
        f"overlay=x='{x_expr}':y='{y_expr}':"
        f"enable='between(t,{t_start},{t_end})':shortest=1"
    )

    return prep_filter, anim_label, overlay_expr
