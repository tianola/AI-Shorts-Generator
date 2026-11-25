"""Regression tests for the fixes from the November 2026 audit.

* A job whose stage died on a database error must still end up 'failed', not
  stuck at 'processing' behind a PendingRollbackError.
* The OmniRoute vision tier must use the configured vision model and ask
  OmniRoute only, instead of a retired hard-coded model sent to every provider.
* chat_completion treats an empty reply as a miss and honours HF_TOKEN.
* The job card's Resume button is enabled for finished jobs, not for running ones.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.orm import object_session

import worker
from config import settings
from database.models import Clip, Job, JobProgress
from database.session import SessionLocal, init_database
from services import llm_fallback
from services import vlm_scene_attention as vlm


# --------------------------------------------------------------------------
# worker: failure is recorded after a database error
# --------------------------------------------------------------------------

@pytest.fixture
def db():
    init_database()
    session = SessionLocal()
    yield session
    session.rollback()
    session.query(JobProgress).delete()
    session.query(Clip).delete()
    session.query(Job).delete()
    session.commit()
    session.close()


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
def test_job_is_marked_failed_after_a_database_error(db, monkeypatch):
    job = Job(youtube_url="https://youtu.be/x", status="created")
    db.add(job)
    db.commit()
    job_id = job.id

    async def download(job_id, job, job_dir, update_progress):
        session = object_session(job)
        # A duplicate primary key fails the flush and leaves the session needing
        # a rollback, which is what a real stage hits on a constraint error.
        session.add(Job(id=job_id, youtube_url="dup", status="created"))
        session.flush()

    monkeypatch.setattr(worker, "stage_download_source", download)

    with pytest.raises(Exception):
        asyncio.run(worker.process_job_async(job_id))

    probe = SessionLocal()
    try:
        status, error = probe.execute(
            text("SELECT status, error_message FROM jobs WHERE id=:j"), {"j": job_id}
        ).one()
    finally:
        probe.close()
    assert status == "failed"
    assert error


# --------------------------------------------------------------------------
# vision tier
# --------------------------------------------------------------------------

def _frame():
    return np.zeros((32, 32, 3), dtype=np.uint8)


def test_omniroute_vision_needs_a_configured_model(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "k")
    monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM_MODEL", "")
    called = []
    monkeypatch.setattr(llm_fallback, "chat_completion", lambda *a, **k: called.append(1))
    with pytest.raises(ValueError, match="SCENE_ATTENTION_VLM_MODEL"):
        asyncio.run(vlm._omniroute_vision(_frame(), "ctx"))
    assert not called


def test_omniroute_vision_asks_omniroute_with_the_configured_model(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "k")
    monkeypatch.setattr(settings, "SCENE_ATTENTION_VLM_MODEL", "vision/model")
    seen = {}

    def fake(messages, model=None, temperature=0.7, max_tokens=2048, providers=None):
        seen.update(model=model, providers=providers)
        return 'Sure:\n```json\n{"subject": "crane", "objects": [], "action": "lifting", "confidence": 0.8}\n```'

    monkeypatch.setattr(llm_fallback, "chat_completion", fake)
    result = asyncio.run(vlm._omniroute_vision(_frame(), "ctx"))
    assert result["subject"] == "crane"
    assert seen == {"model": "vision/model", "providers": ("omniroute",)}


# --------------------------------------------------------------------------
# chat_completion
# --------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, reply):
        self.calls = 0
        self.reply = reply
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls += 1
        message = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _wire(monkeypatch, clients):
    monkeypatch.setattr(llm_fallback, "_get_client", lambda name: clients.get(name))


def test_empty_reply_falls_through_to_the_next_provider(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "a")
    monkeypatch.setattr(settings, "TOGETHER_API_KEY", "b")
    clients = {"omniroute": _FakeClient(None), "together": _FakeClient("answer")}
    _wire(monkeypatch, clients)
    assert llm_fallback.chat_completion([{"role": "user", "content": "x"}]) == "answer"


def test_providers_limits_the_chain(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "a")
    monkeypatch.setattr(settings, "TOGETHER_API_KEY", "b")
    clients = {"omniroute": _FakeClient(None), "together": _FakeClient("answer")}
    _wire(monkeypatch, clients)
    out = llm_fallback.chat_completion([{"role": "user", "content": "x"}],
                                       model="m", providers=("omniroute",))
    assert out is None
    assert clients["together"].calls == 0


def test_hf_token_alone_enables_the_hf_router(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", None)
    monkeypatch.setattr(settings, "TOGETHER_API_KEY", None)
    monkeypatch.setattr(settings, "HF_KEY", None)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    clients = {"hf_router": _FakeClient("from hf")}
    _wire(monkeypatch, clients)
    assert llm_fallback.chat_completion([{"role": "user", "content": "x"}]) == "from hf"


# --------------------------------------------------------------------------
# job card buttons
# --------------------------------------------------------------------------

def test_resume_button_is_enabled_for_finished_jobs():
    source = (Path(__file__).resolve().parents[1] / "static" / "js" / "jobs.js").read_text(encoding="utf-8")
    assert "resume-btn\" title=\"Resume Job\" ${finished ? '' : 'disabled'}" in source
    # The old template disabled Resume and Restart for exactly the statuses the
    # API accepts them from.
    assert "job.status === 'failed' || job.status === 'completed' ? 'disabled'" not in source


# --------------------------------------------------------------------------
# network exposure
# --------------------------------------------------------------------------

def test_api_listens_on_loopback_by_default():
    from config import Settings
    assert Settings.model_fields["API_HOST"].default == "127.0.0.1"


def test_service_and_compose_do_not_hard_code_every_interface():
    root = Path(__file__).resolve().parents[2]
    service = (root / "backend" / "run_service.py").read_text(encoding="utf-8")
    assert 'host="0.0.0.0"' not in service
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"127.0.0.1:${SHORTS_API_HOST_PORT:-5002}:5001"' in compose


# --------------------------------------------------------------------------
# Claude passes share one cached transcript
# --------------------------------------------------------------------------

def test_context_is_sent_as_a_cached_block_before_the_task():
    from services.claude_client import _user_content
    assert _user_content("task", "") == "task"
    blocks = _user_content("task", "Transcript:\n[0.0s - 1.0s] hi\n")
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[1] == {"type": "text", "text": "task"}


def test_story_and_selection_send_the_same_cached_prefix(tmp_path, monkeypatch):
    import json as _json
    from services import claude_client, clip_selector

    segments = [{"start": float(i * 10), "end": float(i * 10 + 10), "text": f"sentence number {i}."}
                for i in range(12)]
    seg_file = tmp_path / "segments.json"
    seg_file.write_text(_json.dumps(segments), encoding="utf-8")

    calls = []

    async def fake_claude(prompt, system, schema, model=None, max_tokens=16000,
                          effort="medium", context=""):
        calls.append({"system": system, "context": context, "effort": effort,
                      "kind": next(iter(schema["properties"]))})
        if "story_summary" in schema["properties"]:
            return _json.dumps({"story_summary": "s", "hook": "h", "story_type": "reveal",
                                "recommended_layout": "centered_face", "cta": "c",
                                "key_moments": []})
        if "ranked" in schema["properties"]:
            return _json.dumps({"ranked": []})
        return _json.dumps({"clips": []})

    monkeypatch.setattr(claude_client, "claude_available", lambda: True)
    monkeypatch.setattr(claude_client, "call_claude_json", fake_claude)
    asyncio.run(clip_selector.select_clips(str(seg_file), num_clips=1))

    story, candidates = calls[0], calls[1]
    assert story["kind"] == "story_summary" and candidates["kind"] == "clips"
    assert story["context"] and story["context"] == candidates["context"]
    assert story["system"] == candidates["system"]
    assert candidates["effort"] == "high" and story["effort"] == "medium"
