"""
Caption generation service.
Creates ASS subtitles with word-level karaoke highlighting.
"""
import json
import os
from typing import List, Dict, Optional
from dataclasses import dataclass
from config import settings
from utils.logger import job_info

@dataclass
class CaptionStyle:
    """Dataclass for caption styling configuration."""
    font_name: str = "Montserrat ExtraBold"
    font_size: int = 110
    primary_color: str = "#FFFFFF"  # White
    highlight_color: str = "#FF4D8D"  # Pink highlight
    outline_color: str = "#000000"  # Black
    back_color: str = "#00000000"  # Transparent
    bold: bool = True
    italic: bool = False
    border_style: int = 1
    outline: int = 8
    shadow: int = 4
    alignment: int = 2  # Center
    margin_v: int = 180  # Safer lower region for vertical shorts
    margin_l: int = 40
    margin_r: int = 40
    multicolor: bool = True
    jump: bool = True

# Default TikTok-style multicolor jump look
DEFAULT_STYLE = CaptionStyle(
    font_name="Montserrat ExtraBold",
    font_size=48,
    primary_color="#FFFFFF",
    highlight_color="#FFE500",
    outline_color="#000000",
    back_color="#00000000",
    bold=True,
    border_style=1,
    outline=4,
    shadow=2,
    alignment=2,
    margin_v=440,
    margin_l=80,
    margin_r=80,
    multicolor=True,
    jump=True,
)

def ass_color(hex_color: str) -> str:
    """Convert hex color to ASS format (BGR order)."""
    # Remove # if present
    hex_color = hex_color.lstrip('#')

    # Convert to RGB
    if len(hex_color) == 6:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    else:
        # Default to white
        r, g, b = 255, 255, 255

    # ASS uses BGR order and &H prefix
    return f"&H{b:02X}{g:02X}{r:02X}"

def ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format (H:MM:SS.cc)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:06.2f}"

def escape_ass(text: str) -> str:
    """Escape special characters for ASS format."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )

def create_caption_block(
    words: List[Dict],
    style: CaptionStyle = DEFAULT_STYLE
) -> str:
    """
    Create a single ASS caption block (Dialogue line) from a group of words.
    Uses a TikTok-like multicolor bounce effect by default to keep the text within
    the safe area and make it pop on vertical videos.
    """
    if not words:
        return ""

    start = words[0]["start"]
    end = words[-1]["end"]

    colors = [
        "#FFE500", "#00F0FF", "#FF2D55", "#34D399", "#A78BFA", "#F97316"
    ]

    # Standard bottom-center alignment with border and shadow
    # NO \pos tag so ASS wraps lines within MarginL/MarginR boundaries
    text = r"{\an2\bord4\shad2\be1\blur0.2}"

    base_fs = style.font_size
    peak_fs = int(base_fs * 1.15)

    for i, w in enumerate(words):
        word = escape_ass(w["word"].strip())
        if not word:
            continue

        word_start = max((w["start"] - start) * 1000.0, 0)
        word_end = max((w["end"] - start) * 1000.0, word_start + 120)
        color = colors[i % len(colors)] if style.multicolor else style.highlight_color
        base_color = ass_color(style.primary_color)
        active_color = ass_color(color)

        if style.jump:
            # Color highlight + quick pop scale effect (without \pos / \move)
            pop_tag = (
                r"{\c" + active_color
                + r"\fs" + str(base_fs)
                + r"\t(" + f"{int(word_start)},{int(word_start + 60)},\\fs{peak_fs}\\c" + active_color + r")"
                + r"\t(" + f"{int(word_end)},{int(word_end + 60)},\\fs{base_fs}\\c" + base_color + r")}"
            )
            text += pop_tag + word
        else:
            text += (
                r"{\c" + active_color + r"\fs" + str(base_fs)
                + r"\t(" + f"{int(word_start)},{int(word_start + 60)},\\c" + active_color + r")"
                + r"\t(" + f"{int(word_end)},{int(word_end + 60)},\\c" + base_color + r")}"
            )
            text += word

        if i < len(words) - 1:
            text += " "

    return (
        "Dialogue: 0,"
        + ass_time(start)
        + ","
        + ass_time(end)
        + ",Default,,0,0,0,,"
        + text.strip()
        + "\n"
    )

def generate_ass_header(style: CaptionStyle = DEFAULT_STYLE) -> str:
    """Generate ASS file header with style definitions."""
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding

Style: Default,{style.font_name},{style.font_size},{ass_color(style.primary_color)},{ass_color(style.highlight_color)},{ass_color(style.outline_color)},{ass_color(style.back_color)},{int(style.bold)},{int(style.italic)},{style.border_style},{style.outline},{style.shadow},{style.alignment},{style.margin_l},{style.margin_r},{style.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text

"""

def create_captions(
    words_json: str,
    output_ass: str,
    group_size: int = None,
    style: CaptionStyle = DEFAULT_STYLE
) -> str:
    """
    Create ASS captions from word-level timestamps.

    Args:
        words_json: Path to words.json file or JSON string
        output_ass: Path to output .ass file
        group_size: Number of words per caption group (defaults to CAPTION_GROUP_SIZE)
        style: CaptionStyle to use

    Returns:
        Path to output file
    """
    if group_size is None:
        group_size = settings.CAPTION_GROUP_SIZE

    # Load words
    try:
        if os.path.exists(words_json):
            with open(words_json, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = json.loads(words_json)
    except Exception as e:
        raise Exception(f"Failed to load words: {str(e)}")

    # Normalize data format
    if isinstance(data, dict) and "words" in data:
        words = data["words"]
    else:
        words = data

    # Clean and validate words
    clean_words = []
    for item in words:
        word = item.get("word", item.get("text", ""))
        start = item.get("start", item.get("start_time", 0))
        end = item.get("end", item.get("end_time", 0))

        try:
            start = float(start)
            end = float(end)
        except (ValueError, TypeError):
            continue

        if end <= start:
            continue

        clean_words.append({
            "word": word,
            "start": start,
            "end": end
        })

    # Group words
    events = []
    for i in range(0, len(clean_words), group_size):
        block = clean_words[i:i + group_size]
        if block:
            events.append(create_caption_block(block, style))

    # Ensure output directory exists
    os.makedirs(os.path.dirname(output_ass) if os.path.dirname(output_ass) else ".", exist_ok=True)

    # Write ASS file
    with open(output_ass, "w", encoding="utf-8") as f:
        f.write(generate_ass_header(style))
        for event in events:
            f.write(event)

    job_info(0, f"ASS captions created: {output_ass}")
    return output_ass