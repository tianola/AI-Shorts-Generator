"""Automatic SFX layering for short-form clips with 100% voice preservation."""
import os
import subprocess
import random
from config import settings


# Curated royalty-free SFX categories mapped to clip vibes
SFX_LIBRARY = {
    "whoosh": ["whoosh_1", "whoosh_2", "swoosh"],
    "pop": ["pop_1", "pop_2", "click"],
    "impact": ["impact_1", "impact_2", "thump"],
    "riser": ["riser_1", "riser_2", "buildup"],
    "transition": ["swish", "glitch_transition", "camera_shutter"],
    "energy": ["energy_burst", "electric_zap", "power_up"],
}

# Keywords in title/transcript that trigger specific SFX
SFX_KEYWORD_MAP = {
    "whoosh": ["fast", "quick", "speed", "rush", "zoom", "swipe", "slide", "transition", "fly"],
    "pop": ["pop", "click", "snap", "burst", "key", "tip", "secret", "hack", "point"],
    "impact": ["heavy", "hard", "power", "shock", "insane", "crazy", "boom", "never", "truth", "money", "crash"],
    "riser": ["build", "rise", "grow", "intense", "climax", "peak", "tension", "wait"],
    "transition": ["change", "switch", "shift", "next", "reveal", "then", "transform", "morph"],
    "energy": ["energy", "electric", "power", "lightning", "spark", "charge", "surge", "fire"],
}


def pick_sfx_for_clip(title: str, transcript: str, style_prompt: str = "") -> list[str]:
    """Pick 1-2 subtle SFX categories based on clip content."""
    text = f"{title} {transcript} {style_prompt}".lower()
    chosen = []

    for category, keywords in SFX_KEYWORD_MAP.items():
        if any(kw in text for kw in keywords):
            if category not in chosen:
                chosen.append(category)

    # Default fallback if nothing matched
    if not chosen:
        chosen = ["whoosh", "pop"]

    # Limit to 2 categories max to avoid clutter
    chosen_cats = chosen[:2]

    # Pick one random SFX from each category
    result = []
    for cat in chosen_cats:
        if cat in SFX_LIBRARY:
            result.append(random.choice(SFX_LIBRARY[cat]))

    return result


def _find_sfx_file(sfx_name: str) -> str | None:
    """Locate SFX file in the assets/sfx directory (flat or in subdirs)."""
    roots = [
        settings.STORAGE_ROOT,
        os.path.dirname(settings.STORAGE_ROOT),
        os.getcwd(),
    ]
    seen = set()
    for base in roots:
        sfx_dir = os.path.join(base, "assets", "sfx")
        if not os.path.exists(sfx_dir):
            continue
        for ext in [".wav", ".mp3", ".ogg"]:
            # Check flat dir first
            candidate = os.path.join(sfx_dir, f"{sfx_name}{ext}")
            if candidate not in seen:
                seen.add(candidate)
                if os.path.exists(candidate):
                    return candidate
            
            # Check subdirs (whoosh, pop, impact, etc.)
            for cat_dir in os.listdir(sfx_dir):
                cat_path = os.path.join(sfx_dir, cat_dir)
                if os.path.isdir(cat_path):
                    candidate_sub = os.path.join(cat_path, f"{sfx_name}{ext}")
                    if candidate_sub not in seen:
                        seen.add(candidate_sub)
                        if os.path.exists(candidate_sub):
                            return candidate_sub
    return None


def mix_sfx_into_video(
    video_path: str,
    output_path: str,
    sfx_list: list[str],
    duck_original: float = 1.0,
    sfx_volume: float = 0.25
) -> str:
    """
    Mix subtle SFX into video keeping speech at 100% natural volume (0dB).
    Voice is never ducked or muffled.
    """
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

    # Resolve actual SFX file paths
    sfx_files = []
    for sfx_name in sfx_list:
        p = _find_sfx_file(sfx_name)
        if p:
            sfx_files.append(p)

    if not sfx_files:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path

    # Probe video duration
    probe_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path
    ]
    try:
        dur_res = subprocess.run(probe_cmd, capture_output=True, text=True)
        video_dur = float(dur_res.stdout.strip()) if dur_res.stdout.strip() else 0
    except Exception:
        video_dur = 0

    # Calculate SFX timestamps: start, mid
    timestamps = [0.3, max(1.5, video_dur / 2.0)] if video_dur > 3.0 else [0.3]
    filter_parts = [f"[0:a]volume={duck_original}[orig]"]
    sfx_labels = []

    for i, ts in enumerate(timestamps):
        if i >= len(sfx_files):
            break
        sfx_idx = i + 1
        delay_ms = int(ts * 1000)
        filter_parts.append(
            f"[{sfx_idx}:a]adelay={delay_ms}|{delay_ms},volume={sfx_volume}[sfx{i}]"
        )
        sfx_labels.append(f"[sfx{i}]")

    all_inputs = "[orig]" + "".join(sfx_labels)
    filter_parts.append(
        f"{all_inputs}amix=inputs={1 + len(sfx_labels)}:duration=first:dropout_transition=0[aout]"
    )
    filter_complex = ";".join(filter_parts)

    cmd = ["ffmpeg", "-y", "-i", video_path]
    for sfx in sfx_files[:len(timestamps)]:
        cmd.extend(["-i", sfx])

    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path
    ])

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return output_path
    except subprocess.CalledProcessError:
        import shutil
        shutil.copy2(video_path, output_path)
        return output_path


def add_automatic_sfx(
    video_path: str,
    output_path: str,
    title: str = "",
    transcript: str = "",
    style_prompt: str = ""
) -> str:
    """Convenience function: pick SFX and mix them with 100% natural speech preservation."""
    sfx_list = pick_sfx_for_clip(title, transcript, style_prompt)
    return mix_sfx_into_video(video_path, output_path, sfx_list, duck_original=1.0, sfx_volume=0.25)
