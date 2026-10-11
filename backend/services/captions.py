"""
Caption generation service with OpusClip-grade animated typography and auto-emoji magic.
Creates ASS subtitles with karaoke highlighting, Bézier pop scaling, and high-conversion TikTok styles.
"""
import json
import os
import re
import copy
from typing import List, Dict, Optional
from dataclasses import dataclass
from config import settings
from utils.logger import job_info


@dataclass
class CaptionPreset:
    """Preset styling configuration for TikTok & OpusClip-style subtitles."""
    name: str
    font_name: str = "Montserrat ExtraBold"
    font_size: int = 50
    primary_color: str = "#FFFFFF"     # Inactive text color
    highlight_color: str = "#FFE500"   # Active word color
    secondary_colors: Optional[List[str]] = None
    outline_color: str = "#000000"
    back_color: str = "#00000000"
    bold: bool = True
    italic: bool = False
    border_style: int = 1              # 1 = outline + shadow, 3 = opaque box
    outline: int = 5
    shadow: int = 2
    alignment: int = 2                 # 2 = Bottom center
    margin_v: int = 440                # Vertical safe zone offset
    margin_l: int = 100
    margin_r: int = 100
    jump: bool = True
    pop_scale: int = 120               # Percentage scale for pop animation (120%)
    add_emojis: bool = True


# 6 Curated High-Engagement Presets
PRESETS: Dict[str, CaptionPreset] = {
    "hormozi": CaptionPreset(
        name="Hormozi Viral",
        font_name="Montserrat ExtraBold",
        font_size=54,
        primary_color="#FFFFFF",
        highlight_color="#FFE500",      # Alex Hormozi Iconic Yellow
        secondary_colors=["#22C55E", "#00F0FF", "#FF3B30"],
        outline_color="#000000",
        border_style=1,
        outline=7,
        shadow=3,
        jump=True,
        pop_scale=125,
        add_emojis=True,
    ),
    "capcut_aqua": CaptionPreset(
        name="CapCut Aqua Glow",
        font_name="Montserrat ExtraBold",
        font_size=50,
        primary_color="#FFFFFF",
        highlight_color="#00F0FF",      # Neon Aqua
        secondary_colors=["#38BDF8", "#818CF8", "#F43F5E"],
        outline_color="#001824",
        border_style=1,
        outline=5,
        shadow=3,
        jump=True,
        pop_scale=120,
        add_emojis=True,
    ),
    "mrbeast": CaptionPreset(
        name="MrBeast Impact",
        font_name="Montserrat ExtraBold",
        font_size=56,
        primary_color="#FFFFFF",
        highlight_color="#FFE500",      # Bright Yellow base
        secondary_colors=[
            "#FFE500",                  # 🟡 Vibrant Yellow
            "#00F0FF",                  # 🔵 Electric Blue / Cyber Cyan
            "#22C55E",                  # 🟢 Neon Green
            "#FF007F",                  # 🟣 Cyber Neon / Hot Pink
            "#FF9500",                  # 🟠 Neon Orange
            "#38BDF8",                  # 💧 Neon Sky Blue
        ],
        outline_color="#000000",
        border_style=1,
        outline=8,
        shadow=4,
        jump=True,
        pop_scale=130,
        add_emojis=True,
    ),
    "cyber_neon": CaptionPreset(
        name="Cyber Neon",
        font_name="Montserrat ExtraBold",
        font_size=50,
        primary_color="#E0E7FF",
        highlight_color="#FF007F",      # Hot Neon Pink
        secondary_colors=["#A855F7", "#06B6D4", "#FACC15"],
        outline_color="#18002E",
        border_style=1,
        outline=5,
        shadow=3,
        jump=True,
        pop_scale=122,
        add_emojis=True,
    ),
    "clean_box": CaptionPreset(
        name="Clean Minimal Box",
        font_name="Montserrat ExtraBold",
        font_size=46,
        primary_color="#FFFFFF",
        highlight_color="#FFE500",
        outline_color="#000000",
        back_color="#80000000",         # Semi-transparent dark rounded box
        border_style=3,
        outline=2,
        shadow=0,
        jump=True,
        pop_scale=115,
        add_emojis=False,
    ),
    "underline_wave": CaptionPreset(
        name="Underline Wave",
        font_name="Montserrat ExtraBold",
        font_size=48,
        primary_color="#FFFFFF",
        highlight_color="#38BDF8",
        outline_color="#000000",
        border_style=1,
        outline=4,
        shadow=2,
        jump=True,
        pop_scale=115,
        add_emojis=True,
    ),
}

# Alias for backward compatibility
CaptionStyle = CaptionPreset
DEFAULT_STYLE = PRESETS["mrbeast"]


# Context Emoji Keyword Map (English + Greek keywords)
EMOJI_KEYWORD_MAP = {
    "💰": [
        "money", "cash", "dollar", "profit", "rich", "millionaire", "billionaire", "cost", "price", "paid",
        "λεφτά", "χρήματα", "κέρδος", "πλούσιος", "ευρώ", "δολλάριο"
    ],
    "🔥": [
        "fire", "viral", "insane", "crazy", "trending", "lit", "hot", "epic", "hype",
        "φωτιά", "τρελό", "απίστευτο", "καυτό", "viral"
    ],
    "🚀": [
        "rocket", "scale", "grow", "future", "launch", "moon", "explode", "growth",
        "εκτόξευση", "ανάπτυξη", "μέλλον", "πύραυλος"
    ],
    "⚠️": [
        "danger", "warning", "stop", "mistake", "careful", "never", "avoid", "risk", "worst", "don't",
        "κίνδυνος", "προσοχή", "λάθος", "πρόσεχε", "ποτέ", "σταμάτα"
    ],
    "🤯": [
        "secret", "mind", "shocked", "unbelievable", "reveal", "truth", "discovered", "magic", "hidden",
        "μυστικό", "σοκ", "αλήθεια", "αποκάλυψη", "μαγικό"
    ],
    "⚡": [
        "fast", "quick", "speed", "instant", "rapid", "now", "flash",
        "γρήγορα", "άμεσα", "αστραπή", "ταχύτητα"
    ],
    "👀": [
        "look", "watch", "see", "listen", "hear", "notice",
        "δες", "κοίτα", "άκου"
    ],
    "👑": [
        "king", "queen", "best", "goat", "top", "leader", "boss", "champion",
        "βασιλιάς", "κορυφή", "ηγέτης", "πρωταθλητής"
    ],
    "🎯": [
        "target", "goal", "focus", "exact", "strategy", "plan",
        "στόχος", "στρατηγική", "πλάνο"
    ],
}


def inject_emojis(word: str) -> str:
    """Inject contextual emoji if word matches high-impact triggers."""
    clean_w = re.sub(r"[^\w\s]", "", word).lower().strip()
    if not clean_w:
        return word

    for emoji, keywords in EMOJI_KEYWORD_MAP.items():
        if clean_w in keywords:
            return f"{word} {emoji}"
    return word


def ass_color(hex_color: str) -> str:
    """Convert hex color (#RRGGBB) to ASS format (&HBBGGRR&)."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 6:
        r = int(hex_color[0:2], 16)
        g = int(hex_color[2:4], 16)
        b = int(hex_color[4:6], 16)
    elif len(hex_color) == 8:
        # Include alpha if present (&HAABBGGRR)
        a = int(hex_color[0:2], 16)
        r = int(hex_color[2:4], 16)
        g = int(hex_color[4:6], 16)
        b = int(hex_color[6:8], 16)
        return f"&H{a:02X}{b:02X}{g:02X}{r:02X}&"
    else:
        r, g, b = 255, 255, 255

    return f"&H{b:02X}{g:02X}{r:02X}&"


def ass_time(seconds: float) -> str:
    """Convert seconds to ASS time format (H:MM:SS.cc)."""
    # Work in integer centiseconds: formatting seconds with %6.2f rounds
    # 59.995 up to "60.00" and pads values under 10 with spaces, so libass
    # got an out-of-range, whitespace-padded timestamp.
    cs = max(0, int(round(float(seconds) * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    return f"{h}:{m:02d}:{cs // 100:02d}.{cs % 100:02d}"


def escape_ass(text: str) -> str:
    """Escape special characters for ASS format."""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\n", "\\N")
    )


from PIL import ImageFont


def _get_caption_font(style: CaptionPreset):
    """Load a truetype font for layout calculations with robust cross-platform fallbacks."""
    from services.font_resolve import find_font
    font_candidates = [
        find_font(bold=True),
        os.path.join(settings.STORAGE_ROOT, "assets", "fonts", "Montserrat-ExtraBold.ttf"),
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets", "fonts", "Montserrat-ExtraBold.ttf"),
        "C:/Windows/Fonts/montserrat-extrabold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/segoeuib.ttf",
        "/usr/share/fonts/truetype/montserrat/Montserrat-ExtraBold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ]
    for p in font_candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, style.font_size)
            except Exception:
                pass
    try:
        return ImageFont.load_default()
    except Exception:
        return None


def create_caption_block(
    words: List[Dict],
    style: CaptionPreset = DEFAULT_STYLE
) -> str:
    """
    Create ASS caption dialogue lines for a phrase block with isolated word-by-word
    popup scaling and jump. 
    
    Rules:
      - ONLY the currently spoken active word takes the accent highlight color and pops/jumps.
      - ALL other words remain pure crisp white (bold), 100% static with ZERO sentence jitter or collision.
      - Inter-word spacing is dynamically padded so pop scaling never overlaps neighboring words.
    """
    if not words:
        return ""

    # Active word color palette: multi-color dynamic cycling if secondary_colors provided
    palette = style.secondary_colors if (style.secondary_colors and len(style.secondary_colors) > 0) else [style.highlight_color]
    base_color = ass_color(style.primary_color if style.primary_color else "#FFFFFF")
    outline_color = ass_color(style.outline_color if style.outline_color else "#000000")

    block_start = float(words[0].get("start", words[0].get("start_time", 0.0)))
    block_end = float(words[-1].get("end", words[-1].get("end_time", block_start + 0.5)))

    # Pre-process word strings
    processed_words = []
    for w in words:
        raw_word = w.get("word", w.get("text", "")).strip()
        if style.add_emojis:
            raw_word = inject_emojis(raw_word)
        word_escaped = escape_ass(raw_word)
        processed_words.append(word_escaped)

    num_words = len(words)
    font = _get_caption_font(style)

    # Calculate exact horizontal center coordinates for each word with natural inter-word spacing
    y_base = 1920 - style.margin_v  # default ~1480px in 1080x1920 space
    if font and hasattr(font, "getlength"):
        widths = [font.getlength(pw) if pw else 0.0 for pw in processed_words]
        space_w = font.getlength(" ")
    else:
        # Approximate measurement fallback
        widths = [len(pw) * (style.font_size * 0.52) for pw in processed_words]
        space_w = style.font_size * 0.30

    total_w = sum(widths) + (num_words - 1) * space_w
    start_x = max((1080.0 - total_w) / 2.0, float(style.margin_l))

    word_centers = []
    curr_x = start_x
    for i in range(num_words):
        cx = curr_x + widths[i] / 2.0
        word_centers.append(cx)
        curr_x += widths[i] + space_w

    events = []

    for i in range(num_words):
        w_curr = words[i]
        cx = word_centers[i]
        word_str = processed_words[i]
        if not word_str:
            continue

        w_start = float(w_curr.get("start", w_curr.get("start_time", block_start)))
        if i < num_words - 1:
            w_next = words[i + 1]
            next_start = float(w_next.get("start", w_next.get("start_time", w_start + 0.1)))
            cur_end = float(w_curr.get("end", w_curr.get("end_time", w_start + 0.1)))
            w_end = max(cur_end, next_start)
        else:
            w_end = float(w_curr.get("end", w_curr.get("end_time", w_start + 0.2)))

        # 1. Inactive period BEFORE word is spoken (Layer 0, 100% scale, pure white bold)
        if w_start > block_start:
            events.append(
                f"Dialogue: 0,{ass_time(block_start)},{ass_time(w_start)},Default,,0,0,0,,"
                f"{{\\an5\\pos({cx:.1f},{y_base})\\bord{style.outline}\\shad{style.shadow}\\c{base_color}\\3c{outline_color}\\b1\\fscx100\\fscy100}}{word_str}\n"
            )

        # 2. ACTIVE WORD (Layer 1: vibrant highlight color, scale pop & upward jump)
        dur_ms = max(int((w_end - w_start) * 1000), 120)
        jump_y = 16  # upward jump in pixels

        # Dynamic / random color selection from secondary colors
        color_idx = (i + int(block_start * 3.7) + (len(word_str) * 2)) % len(palette)
        active_color = ass_color(palette[color_idx])

        if style.jump:
            # Punchy scale-pop & upward jump on higher layer (foreground z-index)
            # The preset's pop_scale drives the peak; hardcoding 115/132 made
            # every preset render identically.
            peak = max(100, int(style.pop_scale))
            settle_x = 100 + round((peak - 100) * 0.30)
            settle_y = 100 + round((peak - 100) * 0.80)
            settle_t = min(100, max(45, dur_ms // 4))
            return_t = max(dur_ms - 40, settle_t + 1)
            pop_tag = (
                f"{{\\an5\\pos({cx:.1f},{y_base})\\bord{style.outline + 1}\\shad{style.shadow + 1}"
                f"\\c{active_color}\\3c{outline_color}\\b1"
                f"\\t(0,{settle_t},\\fscx{peak}\\fscy{peak}\\pos({cx:.1f},{y_base - jump_y}))"
                f"\\t({settle_t},{return_t},\\fscx{settle_x}\\fscy{settle_y}\\pos({cx:.1f},{y_base - jump_y + 4}))"
                f"\\t({return_t},{dur_ms},\\fscx100\\fscy100\\pos({cx:.1f},{y_base}))"
            )
        else:
            pop_tag = (
                f"{{\\an5\\pos({cx:.1f},{y_base})\\bord{style.outline + 1}\\shad{style.shadow}"
                f"\\c{active_color}\\3c{outline_color}\\b1\\fscx100\\fscy100"
            )

        events.append(
            f"Dialogue: 1,{ass_time(w_start)},{ass_time(w_end)},Default,,0,0,0,,"
            f"{pop_tag}}}{word_str}\n"
        )

        # 3. Inactive period AFTER word has been spoken (Layer 0, 100% scale, returns to pure white bold until block ends)
        if w_end < block_end:
            events.append(
                f"Dialogue: 0,{ass_time(w_end)},{ass_time(block_end)},Default,,0,0,0,,"
                f"{{\\an5\\pos({cx:.1f},{y_base})\\bord{style.outline}\\shad{style.shadow}\\c{base_color}\\3c{outline_color}\\b1\\fscx100\\fscy100}}{word_str}\n"
            )

    return "".join(events)


def generate_ass_header(style: CaptionPreset = DEFAULT_STYLE) -> str:
    """Generate ASS file header with exact PlayRes and font styles."""
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


def _group_words_by_natural_phrasing(
    words: List[Dict],
    max_group_size: int = 5,
    max_chars: int = 30
) -> List[List[Dict]]:
    """
    Group words dynamically into punchy 4-5 word phrases (or 3-4 if words are long),
    respecting word character limits (24-32 char cap, default 30), punctuation, and natural speech pauses.
    """
    if not words:
        return []

    groups = []
    current_group = []
    current_chars = 0

    for i, item in enumerate(words):
        raw_word = item.get("word", item.get("text", "")).strip()
        word_len = len(raw_word)
        added_len = word_len if not current_group else (1 + word_len)

        # If adding this word exceeds character limit (24-32 char cap) and we already have 3+ words, break
        if current_group and (current_chars + added_len > max_chars) and len(current_group) >= 3:
            groups.append(current_group)
            current_group = []
            current_chars = 0
            added_len = word_len

        current_group.append(item)
        current_chars += added_len

        # Check punctuation triggers
        # Hard terminal punctuation (. ? !): strong sentence boundary
        has_hard_punct = any(p in raw_word for p in [".", "?", "!"])
        # Soft clause punctuation (, ; :): natural pause
        has_soft_punct = any(p in raw_word for p in [",", ";", ":"])

        # Check pause trigger: gap > 0.35s between words indicates natural speech break
        has_pause = False
        if i < len(words) - 1:
            next_start = float(words[i + 1].get("start", words[i + 1].get("start_time", 0)))
            cur_end = float(item.get("end", item.get("end_time", 0)))
            if next_start - cur_end > 0.35:
                has_pause = True

        # Group break criteria:
        # 1. Reached max group size (default 5, targeting 4-5 words)
        # 2. Hard punctuation if group already has >= 2 words (or pause / end of words)
        # 3. Soft punctuation if group already has >= 3 words (or pause)
        # 4. Long pause if group has >= 2 words
        should_break = False
        if len(current_group) >= max_group_size:
            should_break = True
        elif has_hard_punct and (len(current_group) >= 2 or has_pause or i == len(words) - 1):
            should_break = True
        elif has_soft_punct and (len(current_group) >= 3 or has_pause):
            should_break = True
        elif has_pause and len(current_group) >= 2:
            should_break = True

        if should_break:
            groups.append(current_group)
            current_group = []
            current_chars = 0

    if current_group:
        groups.append(current_group)

    return groups


def create_captions(
    words_json: str,
    output_ass: str,
    group_size: int = None,
    style_name: str = "hormozi",
    style: CaptionPreset = None,
    karaoke: Optional[bool] = None,
) -> str:
    """
    Create ASS captions from word-level timestamps with chosen TikTok preset.

    Args:
        words_json: Path to words.json file or JSON string
        output_ass: Path to output .ass file
        group_size: Max words per caption group
        style_name: Key in PRESETS ('hormozi', 'capcut_aqua', 'mrbeast', etc.)
        style: Optional direct CaptionPreset override
        karaoke: Force/disable word-by-word karaoke pop highlighting.
            True  -> every preset renders with the animated jump/pop highlight
            False -> every preset renders with plain color highlighting (no pop)
            None  -> follow the preset's own `jump` setting (default)

    Returns:
        Path to output .ass file
    """
    if style is None:
        style = PRESETS.get(style_name, DEFAULT_STYLE)

    # Word-by-word karaoke overlay: honor the explicit frontend override.
    if karaoke is not None:
        style = copy.deepcopy(style)
        style.jump = bool(karaoke)
        if karaoke:
            style.pop_scale = max(style.pop_scale, 120)
        else:
            style.pop_scale = 100

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

    if isinstance(data, dict) and "words" in data:
        words = data["words"]
    else:
        words = data

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

    # Group words cleanly using natural phrasing (3-5 words default)
    word_groups = _group_words_by_natural_phrasing(clean_words, max_group_size=group_size)
    events = []
    for block in word_groups:
        if block:
            events.append(create_caption_block(block, style))

    os.makedirs(os.path.dirname(output_ass) if os.path.dirname(output_ass) else ".", exist_ok=True)

    with open(output_ass, "w", encoding="utf-8") as f:
        f.write(generate_ass_header(style))
        for event in events:
            f.write(event)

    job_info(0, f"ASS captions created with preset '{style.name}': {output_ass}")
    return output_ass
