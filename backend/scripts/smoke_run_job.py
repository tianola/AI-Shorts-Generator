"""Simple smoke script: create a DB job and run processing synchronously for a given YouTube URL.
Usage: python smoke_run_job.py <youtube_url>

Note: Intended for local testing only. Ensure dependencies (ffmpeg, faster-whisper, Ollama) are available.
"""
import sys
from database.session import SessionLocal, init_database
from database.models import Job
from worker import process_job


def main(youtube_url: str):
	# Ensure DB tables exist
	init_database()

	db = SessionLocal()
	job = Job(youtube_url=youtube_url, status="created")
	db.add(job)
	db.commit()
	db.refresh(job)
	job_id = job.id
	print(f"Created job {job_id} for {youtube_url}")

	# Create job directory and place a sample source.mp4 to avoid network download issues
	from pathlib import Path
	from config import settings
	job_dir = Path(settings.JOBS_DIR) / str(job_id)
	job_dir.mkdir(parents=True, exist_ok=True)
	source_path = job_dir / "source.mp4"

	if not source_path.exists():
		print("Creating a short sample video with ffmpeg...")
		try:
			# 6-second black video, 1280x720
			cmd = [
				"ffmpeg",
				"-y",
				"-f", "lavfi",
				"-i", "color=c=black:s=1280x720:d=6",
				"-f", "lavfi",
				"-i", "anullsrc=channel_layout=mono:sample_rate=16000",
				"-shortest",
				"-c:v", "libx264",
				"-c:a", "aac",
				"-ar", "16000",
				"-ac", "1",
				str(source_path)
			]
			import subprocess
			subprocess.run(cmd, check=True, capture_output=True)
			print(f"Sample video created at {source_path}")
		except Exception as e:
			print(f"Could not create sample video: {e}")

	try:
		process_job(job_id)
		print("Job processing finished")
	except Exception as e:
		import traceback
		print("Job failed with exception:\n")
		traceback.print_exc()
	finally:
		db.close()


if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python smoke_run_job.py <youtube_url>")
		sys.exit(1)
	main(sys.argv[1])
