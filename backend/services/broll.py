"""Lightweight B-roll enhancement for short-form clips."""
import os
import subprocess


def add_broll_overlay(video_path: str, output_path: str) -> str:
	"""Add cinematic style/color polish to short-form clips without truncating duration."""
	os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)

	# Subtle cinematic color grade and vignette across the full duration of the video
	cmd = [
		"ffmpeg",
		"-y",
		"-fps_mode", "cfr",
		"-i", video_path,
		"-vf", "eq=contrast=1.04:saturation=1.08,vignette=PI/4",
		"-c:v", "libx264",
		"-preset", "veryfast",
		"-crf", "20",
		"-c:a", "copy",
		"-movflags", "+faststart",
		"-pix_fmt", "yuv420p",
		output_path,
	]

	try:
		subprocess.run(cmd, check=True, capture_output=True)
		return output_path
	except Exception:
		# Fallback to direct copy if filter fails
		import shutil
		shutil.copy2(video_path, output_path)
		return output_path

