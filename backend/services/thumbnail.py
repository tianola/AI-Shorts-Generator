"""
Thumbnail generation service.
Extracts representative frames and adds text overlays.
"""
import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFont
from config import settings
from utils.logger import job_info, job_error, job_warning

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

    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-ss", str(timestamp),
        "-vframes", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black",
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
    font_path: str = None
) -> str:
    """
    Add text overlay to an image.

    Args:
        image_path: Path to the input image
        output_path: Path to save the output image
        text: Text to overlay
        font_size: Font size in pixels
        font_path: Path to font file (defaults to Montserrat ExtraBold)

    Returns:
        Path to the output image

    Raises:
        Exception: If text overlay fails
    """
    if font_path is None:
        from services.font_resolve import find_font
        font_path = find_font(bold=True)
        if not font_path:
            # Last resort: try project fonts directory
            _fallback = os.path.join(settings.FONTS_DIR, "Montserrat-ExtraBold.ttf")
            font_path = _fallback if os.path.exists(_fallback) else ""

    try:
        # Open image
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)

        # Load font (fallback to default if not found)
        try:
            font = ImageFont.truetype(font_path, font_size)
        except IOError:
            font = ImageFont.load_default()
            job_warning(0, f"Font not found at {font_path}, using default")

        # Calculate text position (centered at top)
        try:
            bbox = draw.textbbox((0, 0), text, font=font)
            text_width = bbox[2] - bbox[0]
            text_height = bbox[3] - bbox[1]
        except AttributeError:
            # Fallback for older Pillow versions
            text_width, text_height = font.getsize(text)
        margin = 100
        x = (img.width - text_width) // 2
        y = margin

        # Draw text with outline (white text with black outline)
        outline_color = (0, 0, 0)  # Black outline
        fill_color = (255, 255, 255)  # White text

        # Draw outline (4 directions)
        for dx, dy in [(-2, -2), (-2, 2), (2, -2), (2, 2)]:
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)

        # Draw main text
        draw.text((x, y), text, font=font, fill=fill_color)

        # Save
        img.save(output_path, quality=95)
        job_info(0, f"Text overlay added to {output_path}")

        return output_path

    except Exception as e:
        raise Exception(f"Text overlay failed: {str(e)}")

def generate_thumbnail(
    video_path: str,
    output_path: str,
    title: str,
    timestamp: float = 1.0
) -> Optional[str]:
    """
    Generate a thumbnail for a clip.

    Args:
        video_path: Path to the video file
        output_path: Path to save the thumbnail
        title: Title text to overlay
        timestamp: Time in seconds to extract the frame (default: 1 second in)

    Returns:
        Path to the generated thumbnail, or None if generation fails

    Note:
        This is best-effort. If thumbnail generation fails, it logs the error
        and returns None without raising an exception (per spec requirements).
    """
    try:
        # Extract frame
        temp_frame = output_path.replace(".jpg", "_temp.jpg")
        extract_frame(video_path, temp_frame, timestamp)

        # Add text overlay
        add_text_overlay(temp_frame, output_path, title)

        # Clean up temp file
        if os.path.exists(temp_frame):
            os.remove(temp_frame)

        return output_path

    except Exception as e:
        job_error(0, f"Thumbnail generation failed: {str(e)}")
        return None