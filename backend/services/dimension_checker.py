"""
dimension_checker.py
===================

Validates video dimensions for YouTube Shorts compatibility (9:16 aspect ratio).

Functions:
- check_dimensions(): Analyzes video width/height and validates 9:16 aspect ratio
- Uses ffprobe to extract video metadata efficiently
- Returns structured video info with validation results
"""

import subprocess
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


def check_dimensions(video_path: str) -> Dict[str, Optional[float]]:
    """
    Analyze video dimensions and validate 9:16 aspect ratio.
    
    Args:
        video_path: Path to video file
        
    Returns:
        Dict with: width, height, aspect_ratio, is_valid_916
        Returns None for values if analysis fails
        
    Raises:
        RuntimeError: If ffprobe fails or video is invalid
    """
    if not video_path or not isinstance(video_path, str):
        raise ValueError("video_path must be a non-empty string")
    
    try:
        # Use ffprobe to get video dimensions
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path],
            capture_output=True, text=True, timeout=30
        )
        
        if probe.returncode != 0:
            error_msg = probe.stderr.strip() if probe.stderr else "Unknown ffprobe error"
            logger.error(f"ffprobe failed for {video_path}: {error_msg}")
            raise RuntimeError(f"ffprobe failed: {error_msg}")
        
        # Parse output format: "1920,1080"
        output = probe.stdout.strip()
        if not output:
            raise RuntimeError(f"No video stream found in {video_path}")
        
        try:
            width_str, height_str = output.split(',')
            width = int(width_str)
            height = int(height_str)
        except ValueError as e:
            raise RuntimeError(f"Invalid dimension format from ffprobe: {output} - {e}")
        
        if width <= 0 or height <= 0:
            raise RuntimeError(f"Invalid dimensions: {width}x{height}")
        
        # Calculate aspect ratio and validate 9:16
        aspect_ratio = width / height
        # 9:16 = 0.5625, but allow some tolerance for rounding
        is_916 = (aspect_ratio >= 0.55 and aspect_ratio <= 0.58)
        
        result = {
            "width": width,
            "height": height,
            "aspect_ratio": aspect_ratio,
            "is_valid_916": is_916
        }
        
        logger.info(f"Video {video_path}: {width}x{height} ({aspect_ratio:.3f}) - 9:16: {'✓' if is_916 else '✗'}")
        return result
        
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffprobe timeout for {video_path}")
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found - please install FFmpeg")
    except Exception as e:
        raise RuntimeError(f"Dimension check failed for {video_path}: {e}")


def log_dimension_warning(video_path: str, dimensions: Dict[str, Optional[float]]) -> None:
    """
    Log a warning when video doesn't meet 9:16 requirements.
    
    Args:
        video_path: Path to the video file
        dimensions: Result from check_dimensions()
    """
    if not dimensions.get("is_valid_916", False):
        width = dimensions.get("width")
        height = dimensions.get("height") 
        aspect = dimensions.get("aspect_ratio")
        
        warning_msg = f"Video has non-9:16 dimensions: {width}x{height} ({aspect:.3f})"
        logger.warning(f"[dimension_checker] {warning_msg} - {video_path}")
        
        # Additional context for YouTube Shorts
        if aspect and aspect > 0.7:
            logger.warning(f"[dimension_checker] Video appears to be horizontal ({aspect:.3f}) - may not perform well on YouTube Shorts")
        elif aspect and aspect < 0.5:
            logger.warning(f"[dimension_checker] Video appears to be very tall ({aspect:.3f}) - unusual for Shorts format")