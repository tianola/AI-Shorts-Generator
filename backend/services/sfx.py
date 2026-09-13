"""Automatic SFX layering for short-form clips."""
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
	"ambient": ["ambient_1", "ambient_2", "room_tone"],
	"energy": ["energy_burst", "electric_zap", "power_up"],
}

# Keywords in title/transcript that trigger specific SFX
SFX_KEYWORD_MAP = {
	"whoosh": ["fast", "quick", "speed", "rush", "fly", "zoom", "swipe", "slide", "transition"],
	"pop": ["pop", "click", "snap", "burst", "explode", "bang", "hit", "strike"],
	"impact": ["heavy", "hard", "strong", "power", "force", "crash", "smash", "drop", "beat"],
	"riser": ["build", "rise", "grow", "intense", "climax", "peak", "crescendo", "tension"],
	"transition": ["change", "switch", "shift", "transform", "morph", "cut", "edit", "next"],
	"energy": ["energy", "electric", "power", "lightning", "spark", "voltage", "charge", "surge"],
	"ambient": ["calm", "peace", "quiet", "soft", "gentle", "whisper", "silence", "still"],
}


def pick_sfx_for_clip(title: str, transcript: str, style_prompt: str = "") -> list[str]:
	"""Pick 2-3 SFX categories based on clip content."""
	text = f"{title} {transcript} {style_prompt}".lower()
	chosen = set()

	for category, keywords in SFX_KEYWORD_MAP.items():
		if any(kw in text for kw in keywords):
			chosen.add(category)

	# Default fallback if nothing matched
	if not chosen:
		chosen = {"whoosh", "pop", "transition"}

	# Limit to 3 categories max
	chosen = list(chosen)[:3]

	# Pick one random SFX from each category
	result = []
	for cat in chosen:
		sfx_name = random.choice(SFX_LIBRARY[cat])
		result.append(sfx_name)

	return result


def _find_sfx_file(sfx_name: str) -> str | None:
	"""Locate SFX file in the assets/sfx directory."""
	# Look in project storage/assets/sfx first, then repo-root assets/sfx, then CWD
	roots = [
		settings.STORAGE_ROOT,
		os.path.dirname(settings.STORAGE_ROOT),
		os.getcwd(),
	]
	seen = set()
	for base in roots:
		sfx_dir = os.path.join(base, "assets", "sfx")
		for ext in [".wav", ".mp3", ".ogg"]:
			candidate = os.path.join(sfx_dir, f"{sfx_name}{ext}")
			if candidate in seen:
				continue
			seen.add(candidate)
			if os.path.exists(candidate):
				return candidate
	return None


def mix_sfx_into_video(
	video_path: str,
	output_path: str,
	sfx_list: list[str],
	duck_original: float = 0.3,
	sfx_volume: float = 0.8
) -> str:
	"""
	Mix selected SFX into the video at intelligent timestamps.
	SFX are placed at: start (0.5s), 1/3, 2/3, and before end.
	"""
	os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

	# Resolve actual SFX file paths
	sfx_files = []
	for sfx_name in sfx_list:
		sfx_path = _find_sfx_file(sfx_name)
		if sfx_path:
			sfx_files.append(sfx_path)

	if not sfx_files:
		# No SFX found — log that this is a silent no-op so it's not mistaken for a real effect
		print(f"SFX skipped: no asset files found for {sfx_list} (looked in storage/assets/sfx, repo assets/sfx)")
		import shutil
		shutil.copy2(video_path, output_path)
		return output_path

	# Probe video duration
	probe_cmd = [
		"ffprobe", "-v", "error", "-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1", video_path
	]
	try:
		dur_result = subprocess.run(probe_cmd, capture_output=True, text=True)
		video_dur = float(dur_result.stdout.strip()) if dur_result.stdout.strip() else 0
	except Exception:
		video_dur = 0

	# Calculate SFX timestamps: start, 1/3, 2/3, pre-end
	timestamps = []
	if video_dur > 3:
		timestamps = [0.5, video_dur / 3, 2 * video_dur / 3, max(video_dur - 1.5, 0.5)]
	elif video_dur > 1.5:
		timestamps = [0.5, video_dur / 2, max(video_dur - 1, 0.5)]
	else:
		timestamps = [0.5]

	# Build filter_complex: duck original audio, then mix SFX at timestamps
	filter_parts = []

	# Input indexing: 0 = video, 1...N = SFX files
	sfx_input_count = len(sfx_files)

	# Duck original audio
	if sfx_input_count > 0:
		filter_parts.append(f"[0:a]volume={duck_original}[orig]")

	# Prepare each SFX with adelay
	sfx_labels = []
	for i, ts in enumerate(timestamps):
		if i >= sfx_input_count:
			break
		sfx_idx = i + 1  # ffmpeg input index
		delay_ms = int(ts * 1000)
		filter_parts.append(
			f"[{sfx_idx}:a]adelay={delay_ms}|{delay_ms},volume={sfx_volume}[sfx{i}]"
		)
		sfx_labels.append(f"[sfx{i}]")

	# Mix everything
	if sfx_labels:
		all_inputs = "[orig]" + "".join(sfx_labels)
		filter_parts.append(f"{all_inputs}amix=inputs={1 + len(sfx_labels)}:duration=longest:dropout_transition=0[aout]")
		audio_map = "[aout]"
	else:
		audio_map = "[orig]"

	filter_complex = ";".join(filter_parts)

	# Build FFmpeg command
	cmd = ["ffmpeg", "-y", "-i", video_path]
	cmd.extend(["-i", sfx] for sfx in sfx_files)
	# Flatten the list of lists
	cmd = ["ffmpeg", "-y", "-i", video_path] + [item for sfx in sfx_files for item in ("-i", sfx)]
	cmd.extend([
		"-filter_complex", filter_complex,
		"-map", "0:v",
		"-map", audio_map,
		"-c:v", "copy",
		"-c:a", "aac",
		"-b:a", "192k",
		"-movflags", "+faststart",
		"-pix_fmt", "yuv420p",
		output_path,
	])

	try:
		subprocess.run(cmd, check=True, capture_output=True)
		return output_path
	except subprocess.CalledProcessError as e:
		err = e.stderr
		if isinstance(err, (bytes, bytearray)):
			error_msg = err.decode(errors='ignore')
		else:
			error_msg = str(err) if err else str(e)
		print(f"SFX mix failed: {error_msg}")
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
	"""Convenience function: pick SFX and mix them in one call."""
	sfx_list = pick_sfx_for_clip(title, transcript, style_prompt)
	return mix_sfx_into_video(video_path, output_path, sfx_list)