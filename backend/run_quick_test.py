import os
import json
import asyncio
from config import settings
from services.youtube import download_youtube
from services.audio import extract_audio
from services.whisper import transcribe_audio_file
from services.clip_selector import select_best_clips
from services.story_analysis import analyze_story_segments


def ensure_job_dir(job_id: int):
	path = os.path.join(settings.STORAGE_ROOT.strip('/'), 'jobs', str(job_id))
	os.makedirs(path, exist_ok=True)
	return path


async def run_test(youtube_url: str, job_id: int = 999999):
	print(f"Starting quick test job for: {youtube_url}")
	job_dir = os.path.join('storage', 'jobs', str(job_id))
	os.makedirs(job_dir, exist_ok=True)

	print("Downloading video...")
	video_path = download_youtube(youtube_url, job_id)
	print("Video saved to:", video_path)

	print("Extracting audio...")
	audio_path = extract_audio(video_path)
	print("Audio saved to:", audio_path)

	print("Transcribing (Groq/local)...")
	segments_file, words_file = transcribe_audio_file(audio_path, job_dir)
	print("Transcription saved:", segments_file, words_file)

	# Load segments for analysis
	with open(segments_file, 'r', encoding='utf-8') as f:
		segments = json.load(f)

	print("Analyzing story segments...")
	story = await analyze_story_segments(segments)
	print("Story analysis result:")
	print(json.dumps(story, indent=2, ensure_ascii=False))

	print(f"Selecting top {settings.NUM_CLIPS} clips...")
	clips = await select_best_clips(segments_file, settings.NUM_CLIPS)
	print("Selected clips:")
	print(json.dumps(clips, indent=2, ensure_ascii=False))

	out_json = os.path.join(job_dir, 'quick_test_result.json')
	with open(out_json, 'w', encoding='utf-8') as f:
		json.dump({'story': story, 'clips': clips}, f, ensure_ascii=False, indent=2)

	print("Quick test complete. Results saved to:", out_json)


if __name__ == '__main__':
	url = 'https://youtu.be/5pV3D14y1FQ'
	asyncio.run(run_test(url, job_id=999999))
