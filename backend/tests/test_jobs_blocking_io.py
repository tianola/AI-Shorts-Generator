"""Multi-second file work does not belong on the event loop.

`restart_job` calls `shutil.rmtree` on a job directory that can hold several GB
of renders, and the two upload endpoints `shutil.copyfileobj` a whole source
video into `storage/jobs/<id>/source.mp4`. All of it runs inside `async def`
handlers, so while it runs the one event loop is doing nothing else: the Studio
UI stops updating, `/ws/progress/{job_id}` goes quiet, and every other request
queues behind a copy that on this machine takes tens of seconds.

The fix is one `run_in_threadpool` per site - same responses, same cleanup
order, same failure paths. Each test below records which thread actually did
the work: the loop thread is the one that called the coroutine, so a different
ident is the proof.
"""
import asyncio
import io
import os
import shutil
import threading

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import pytest
from starlette.datastructures import UploadFile

from api import jobs as jobs_api
from config import settings
from database.base import Base
from database.models import Job
from database.session import SessionLocal, engine

Base.metadata.create_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(Job).delete()
    session.commit()
    session.close()


class _ThreadSpy:
    """Wraps a blocking function and remembers who called it."""

    def __init__(self, real):
        self._real = real
        self.thread_idents = []

    def __call__(self, *args, **kwargs):
        self.thread_idents.append(threading.get_ident())
        return self._real(*args, **kwargs)


@pytest.fixture
def spy_on_blocking_io(monkeypatch):
    spies = {}
    for name in ("rmtree", "copyfileobj"):
        real = getattr(shutil, name)
        spy = _ThreadSpy(real)
        monkeypatch.setattr(shutil, name, spy)
        spies[name] = spy
    return spies


def _new_job(db, url="https://youtu.be/x", status="failed"):
    job = Job(youtube_url=url, status=status)
    db.add(job)
    db.commit()
    return job.id


def _upload(payload: bytes, filename="source.mp4") -> UploadFile:
    return UploadFile(file=io.BytesIO(payload), size=len(payload), filename=filename)


#: The endpoints are called directly (so the test can see which thread ran the
#: copy), which means every `Form(...)` default has to be supplied by hand.
_FORM_DEFAULTS = dict(style_prompt=None, whisper_model="tiny",
                      caption_style="mrbeast", layout_mode="auto",
                      target_clips=1, auto_upload=False, llm_translate=False,
                      upload_privacy="unlisted")


def test_restart_deletes_the_tree_off_the_event_loop(db, monkeypatch, spy_on_blocking_io):
    job_id = _new_job(db)
    job_dir = os.path.join(settings.JOBS_DIR, str(job_id))
    os.makedirs(job_dir, exist_ok=True)
    with open(os.path.join(job_dir, "source.mp4"), "wb") as fh:
        fh.write(b"rendered bytes")

    loop_thread = []

    async def call():
        loop_thread.append(threading.get_ident())
        return await jobs_api.restart_job(job_id, db=db)

    result = asyncio.run(call())

    assert result["status"] == "created"
    assert spy_on_blocking_io["rmtree"].thread_idents, "rmtree never ran"
    assert spy_on_blocking_io["rmtree"].thread_idents[0] != loop_thread[0], (
        "the tree was deleted on the event loop thread")
    assert not os.path.exists(os.path.join(job_dir, "source.mp4"))


def test_upload_writes_the_source_off_the_event_loop(db, spy_on_blocking_io):
    payload = b"x" * 4096

    loop_thread = []

    async def call():
        loop_thread.append(threading.get_ident())
        return await jobs_api.create_job_from_upload(file=_upload(payload),
                                                     db=db, **_FORM_DEFAULTS)

    response = asyncio.run(call())

    assert spy_on_blocking_io["copyfileobj"].thread_idents
    assert spy_on_blocking_io["copyfileobj"].thread_idents[0] != loop_thread[0], (
        "a multi-GB copy ran on the event loop thread")

    job_id = response.job_id
    dest = os.path.join(settings.JOBS_DIR, str(job_id), "source.mp4")
    assert open(dest, "rb").read() == payload


def test_diagnostic_upload_writes_the_source_off_the_event_loop(db, spy_on_blocking_io):
    payload = b"y" * 2048
    loop_thread = []

    async def call():
        loop_thread.append(threading.get_ident())
        return await jobs_api.zero_view_diagnostic_endpoint(
            youtube_url=None, file=_upload(payload), db=db, **_FORM_DEFAULTS)

    response = asyncio.run(call())

    assert "job_id" in response
    copied = spy_on_blocking_io["copyfileobj"].thread_idents
    assert copied, "the diagnostic upload never copied anything"
    assert copied[0] != loop_thread[0], "the diagnostic copy ran on the event loop"


def test_a_failed_upload_still_reports_500(db, monkeypatch):
    """The threadpool move must keep the error path, not swallow it."""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfileobj", boom)

    async def call():
        return await jobs_api.create_job_from_upload(file=_upload(b"z"),
                                                     db=db, **_FORM_DEFAULTS)

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(call())

    assert excinfo.value.status_code == 500
    probe = SessionLocal()
    try:
        job = probe.query(Job).order_by(Job.id.desc()).first()
        assert job.status == "failed"
        assert "disk full" in job.error_message
    finally:
        probe.close()
