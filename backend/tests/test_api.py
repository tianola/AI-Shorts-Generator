import pytest
import os
os.environ["TEST_DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient
import database.session as db_session
from database.base import Base
from database.session import SessionLocal
from database.models import Job

import pytest
import os
os.environ["TEST_DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient
from database.session import get_database, SessionLocal
from database.base import Base
from database.models import Job

# Create tables in the engine defined in database.session
from database.session import engine
Base.metadata.create_all(bind=engine)

from main import app

client = TestClient(app)

def test_create_job_with_style_prompt():
	response = client.post("/api/jobs?youtube_url=https://youtube.com/test&style_prompt=cyberpunk")
	assert response.status_code == 200
	data = response.json()
	job_id = data["job_id"]

	db = SessionLocal()
	job = db.query(Job).filter(Job.id == job_id).first()
	assert job.style_prompt == "cyberpunk"
	db.close()

def test_create_job_with_style_prompt():
	response = client.post("/api/jobs?youtube_url=https://youtube.com/test&style_prompt=cyberpunk")
	assert response.status_code == 200
	data = response.json()
	job_id = data["job_id"]

	db = SessionLocal()
	job = db.query(Job).filter(Job.id == job_id).first()
	assert job.style_prompt == "cyberpunk"
	db.close()
