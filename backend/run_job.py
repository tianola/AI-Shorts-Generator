"""Run an end-to-end job for the downloaded source.mp4 (job id 999999)."""
import os
import sys

# Force Windows-friendly storage root relative to the repo root.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
os.environ.setdefault("STORAGE_ROOT", os.path.join(REPO_ROOT, "storage"))

from config import settings  # noqa: E402
from database.session import SessionLocal, init_database  # noqa: E402
from database.models import Job  # noqa: E402
from worker import process_job_async  # noqa: E402
import asyncio  # noqa: E402

print(f"STORAGE_ROOT = {settings.STORAGE_ROOT}")
print(f"JOBS_DIR     = {settings.JOBS_DIR}")
print(f"Jobs dir exists: {os.path.isdir(settings.JOBS_DIR)}")

init_database()

db = SessionLocal()
job = db.query(Job).filter(Job.id == 999999).first()
if job is None:
	job = Job(youtube_url="https://youtu.be/5pV3D14y1FQ", status="created")
	db.add(job)
	db.commit()
	db.refresh(job)
print(f"Job id={job.id} status={job.status} url={job.youtube_url}")

source = os.path.join(settings.JOBS_DIR, "999999", "source.mp4")
print(f"source.mp4 exists: {os.path.exists(source)} ({source})")
if not os.path.exists(source):
	sys.exit("source.mp4 not found - cannot run pipeline")

asyncio.run(process_job_async(job.id))
print("Pipeline finished.")
db.close()