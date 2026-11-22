"""The LLM health indicator: what the app saw, and the status it reports.

No test calls a real model or the real server. The probe is replaced with a fake.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import llm_health as api_llm_health
from services import llm_health


@pytest.fixture(autouse=True)
def fresh_history():
	llm_health.reset()
	yield
	llm_health.reset()


def _status(target="omniroute:free/shorts"):
	return next(t for t in llm_health.snapshot()["targets"] if t["target"] == target)


def test_a_model_never_called_is_idle_not_an_error():
	assert llm_health.snapshot()["overall"] == "idle"


def test_a_successful_call_is_ok():
	llm_health.end(llm_health.begin("omniroute:free/shorts"), "ok")
	assert _status()["status"] == "ok"
	assert llm_health.snapshot()["overall"] == "ok"


def test_a_failure_with_no_earlier_success_is_down_and_keeps_the_error():
	llm_health.end(llm_health.begin("omniroute:free/shorts"), "timeout", "ReadTimeout: slow")
	status = _status()
	assert status["status"] == "down"
	assert status["last_error"]["kind"] == "timeout"
	assert "ReadTimeout" in status["last_error"]["detail"]


def test_a_failure_after_a_success_is_degraded_not_down():
	target = "omniroute:free/shorts"
	llm_health.end(llm_health.begin(target), "ok")
	llm_health.end(llm_health.begin(target), "error", "HTTP 503")
	assert _status()["status"] == "degraded"


def test_a_call_that_never_returns_is_unresponsive(monkeypatch):
	monkeypatch.setattr(llm_health, "UNRESPONSIVE_AFTER_SECONDS", 0.0)
	call = llm_health.begin("omniroute:free/shorts")
	assert _status()["status"] == "unresponsive"
	assert _status()["in_flight"] == 1
	llm_health.end(call, "ok")
	assert _status()["in_flight"] == 0


def test_no_json_reply_is_recorded_as_its_own_kind():
	llm_health.end(llm_health.begin("omniroute:free/shorts"), "no_json", "'I cannot help'")
	assert llm_health.snapshot()["recent_events"][0]["kind"] == "no_json"


def test_the_endpoint_reports_state_and_a_failed_probe_marks_the_app_down(monkeypatch):
	llm_health.end(llm_health.begin("omniroute:free/shorts"), "ok")

	async def unreachable(*_args, **_kwargs):
		return {"provider": "omniroute", "reachable": False, "detail": "ConnectError: refused"}

	monkeypatch.setattr(api_llm_health, "_probe_omniroute", unreachable)
	app = FastAPI()
	app.include_router(api_llm_health.router, prefix="/api")
	client = TestClient(app)

	plain = client.get("/api/llm/health").json()
	assert plain["overall"] == "ok" and plain["probe"] is None

	probed = client.get("/api/llm/health?probe=1").json()
	assert probed["probe"]["reachable"] is False
	assert probed["overall"] == "down"
