"""
Video rendering service.
Burns captions into videos using FFmpeg and libass.
"""
import subprocess
import json
import os
from config import settings
from utils.logger import job_info, job_error

def get_video_size(video_path: str) -> tuple:
    """
    Get video dimensions using ffprobe.

    Args:
        video_path: Path to video file

    Returns:
        Tuple of (width, height)

    Raises:
        Exception: If video size cannot be determined
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        video_path
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)

        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                return (stream.get("width"), stream.get("height"))

        # No video stream found — return fallback vertical size
        job_error(0, "ffprobe returned no video streams; using fallback size 1080x1920")
        return (1080, 1920)

    except Exception as e:
        # On probe failure, return fallback size rather than raising so the pipeline can continue
        job_error(0, f"Failed to get video size: {str(e)}; using fallback 1080x1920")
        return (1080, 1920)

def mux_tts(
    video_path: str,
    tts_path: str,
    output_path: str
) -> str:
    """
    Mix TTS voice-over into the video while keeping the full clip length.

    The TTS (a short hook, typically a few seconds) is layered on top of the
    original clip audio (ducked) so the finished short keeps its intended
    duration instead of collapsing to the length of the voice file.

    Args:
        video_path: Path to input video
        tts_path: Path to tts audio (mp3)
        output_path: Path to output video
    Returns:
        Path to muxed output video
    """
    # Ensure output dir exists
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    if not os.path.exists(tts_path):
        raise Exception(f"TTS audio not found: {tts_path}")

    has_audio = _has_audio_stream(video_path)

    if has_audio:
        # Layer the hook voice-over on top of the ducked original audio.
        # amix duration=longest keeps the output as long as the video's own
        # audio, so the clip is never truncated to the TTS length.
        cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-i", tts_path,
            "-map", "0:v",
            "-filter_complex",
            "[1:a]adelay=600|600,volume=1.0[t];[0:a]volume=0.35[orig];"
            "[t][orig]amix=inputs=2:duration=longest:dropout_transition=0[a]",
            "-map", "[a]",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]
    else:
        # No original audio: pad the TTS with silence to match the video so
        # the output keeps its full length.
        video_duration = _probe_duration(video_path)
        cmd = [
            "ffmpeg",
            "-y",
            "-i", video_path,
            "-i", tts_path,
            "-map", "0:v",
            "-map", "1:a",
            "-af", f"apad=whole_dur={max(video_duration - 0.6, 1):.3f},adelay=600|600",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            output_path
        ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        job_info(0, f"Muxed TTS into video: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"TTS mux failed: {error_msg}")
        raise Exception(f"TTS mux failed: {error_msg}")


def _has_audio_stream(video_path: str) -> bool:
    """Return True when the video file contains at least one audio stream."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=index",
        "-of", "csv=p=0",
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return bool(result.stdout.strip())
    except Exception:
        return True  # Assume audio exists; the amix path is the common case.


def _probe_duration(video_path: str) -> float:
    """Return the video duration in seconds (0.0 on failure)."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        video_path
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def add_title_overlay(
    video_path: str,
    title: str,
    output_path: str,
    font_size: int = 52
) -> str:
    """Add a TikTok-style top title overlay with dark band, 3D yellow text, and energy strip."""
    from services.font_resolve import find_font, ffmpeg_font_path

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    display_title = str(title or "Viral Moment")[:50].upper()

    # Word-wrap into at most 2 clean lines with max ~16-18 chars per line to prevent horizontal overflow
    words = display_title.split()
    lines = []
    current_line = []
    for word in words:
        current_line.append(word)
        if len(" ".join(current_line)) > 16:
            lines.append(" ".join(current_line))
            current_line = []
    if current_line:
        lines.append(" ".join(current_line))

    display_text = "\n".join(lines[:2])

    # Adaptive font size — safe and perfectly scaled for 1080x1920
    length = len(display_title)
    if length < 20:
        fs = 58
    elif length < 35:
        fs = 48
    else:
        fs = 40

    # Write title to temp file — avoids ALL drawtext inline escaping issues
    title_file = output_path + ".title.txt"
    with open(title_file, "w", encoding="utf-8") as f:
        f.write(display_text)

    title_file_ff = ffmpeg_font_path(title_file)
    font = find_font(bold=True)
    font_ff = ffmpeg_font_path(font) if font else ""
    font_opt = f"fontfile='{font_ff}':" if font_ff else ""

    # TikTok-style filter chain:
    # 1) Dark translucent band at top
    # 2) Yellow accent strip at bottom of band
    # 3) Single clean text layer: yellow fill + black border + subtle shadow
    #    (rendered ONCE — no stacked layers, so the title never looks doubled)
    subtitle_filter = (
        f"drawbox=x=0:y=80:w=iw:h=210:color=black@0.80:t=fill,"
        f"drawbox=x=0:y=285:w=iw:h=6:color=0xFFE500:t=fill,"
        f"drawtext=textfile='{title_file_ff}':"
        f"{font_opt}"
        f"fontcolor=0xFFE500:fontsize={fs}:"
        f"x=(w-text_w)/2:y=110:"
        f"borderw=5:bordercolor=0x000000:"
        f"shadowx=3:shadowy=3:shadowcolor=0x000000@0.6"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", subtitle_filter,
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
        job_info(0, f"Title overlay added: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"Title overlay failed: {error_msg}")
        raise Exception(f"Title overlay failed: {error_msg}")
    finally:
        # Clean up temp title file
        try:
            os.remove(title_file)
        except OSError:
            pass


def burn_captions(
    video_path: str,
    captions_path: str,
    output_path: str,
) -> str:
    """
    Burn ASS captions into a video using FFmpeg and libass.
    Uses the embedded styles from the .ass file for exact positioning and font sizing.
    """
    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Build subtitle filter — normalize path for ffmpeg and escape single quotes
    captions_escaped = captions_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    subtitle_filter = f"subtitles='{captions_escaped}'"

    # FFmpeg command with settings from config
    cmd = [
        "ffmpeg",
        "-y",
        "-i", video_path,
        "-vf", subtitle_filter,
        "-map", "0:v",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-fps_mode", "cfr",
        "-preset", settings.FFMPEG_PRESET,
        "-crf", str(settings.FFMPEG_CRF),
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-pix_fmt", "yuv420p",
        output_path
    ]

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True
        )
        job_info(0, f"Captions burned in: {output_path}")
        return output_path
    except subprocess.CalledProcessError as e:
        err = e.stderr
        if isinstance(err, (bytes, bytearray)):
            error_msg = err.decode(errors='ignore')
        else:
            error_msg = str(err) if err else str(e)
        job_error(0, f"Caption burn-in failed: {error_msg}")
        raise Exception(f"Caption burn-in failed: {error_msg}")