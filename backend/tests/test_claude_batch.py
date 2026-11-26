"""CLAUDE_USE_BATCH sends the Claude passes through the Message Batches API.

No network: the SDK client is replaced with a fake that records what it is sent.
Pins that:
1. With the flag off, calls stream as before (with the refusal fallback).
2. With it on, one batch request carries the same model, system, content and
   output_config, and no streaming-only fields (betas, fallbacks).
3. A batch that outlasts the timeout is cancelled and the call is made directly.
4. A refused batch request raises, so the selector falls back as it does today.
"""
import os
from types import SimpleNamespace

os.environ.setdefault("TEST_DATABASE_URL", "sqlite:///:memory:")

import anthropic
import pytest

from config import settings
from services import claude_client


def _message(text='{"ok": true}', stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason, model="claude-opus-5-5",
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0),
    )


class _Stream:
    def __init__(self, record, kwargs):
        record.append(("stream", kwargs))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return _message('{"via": "stream"}')


class _Results:
    def __init__(self, entries):
        self._entries = list(entries)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._entries:
            raise StopAsyncIteration
        return self._entries.pop(0)


class _Batches:
    def __init__(self, record, finish_after, result_type):
        self.record = record
        self.polls = 0
        self.finish_after = finish_after
        self.result_type = result_type
        self.custom_id = None

    async def create(self, *, requests):
        self.record.append(("batch", requests))
        self.custom_id = requests[0]["custom_id"]
        return SimpleNamespace(id="batch_1", processing_status="in_progress")

    async def retrieve(self, batch_id):
        self.polls += 1
        done = self.finish_after is not None and self.polls >= self.finish_after
        return SimpleNamespace(id=batch_id, processing_status="ended" if done else "in_progress")

    async def cancel(self, batch_id):
        self.record.append(("cancel", batch_id))

    async def results(self, batch_id):
        if self.result_type == "succeeded":
            result = SimpleNamespace(type="succeeded", message=_message('{"via": "batch"}'))
        else:
            result = SimpleNamespace(type=self.result_type, error=None)
        return _Results([SimpleNamespace(custom_id="someone-else", result=None),
                         SimpleNamespace(custom_id=self.custom_id, result=result)])


@pytest.fixture
def fake_client(monkeypatch):
    record = []
    state = {"finish_after": 2, "result_type": "succeeded"}

    class FakeClient:
        def __init__(self, api_key=None):
            self.messages = SimpleNamespace(
                batches=_Batches(record, state["finish_after"], state["result_type"]))
            self.beta = SimpleNamespace(messages=SimpleNamespace(
                stream=lambda **kw: _Stream(record, kw)))

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeClient)
    monkeypatch.setattr(claude_client, "_BATCH_POLL_SECONDS", 0)
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test-key", raising=False)
    return SimpleNamespace(record=record, state=state)


SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}


@pytest.mark.asyncio
async def test_without_the_flag_the_call_streams(fake_client, monkeypatch):
    monkeypatch.setattr(settings, "CLAUDE_USE_BATCH", False, raising=False)
    text = await claude_client.call_claude_json("task", "sys", SCHEMA, context="transcript")
    assert text == '{"via": "stream"}'
    kind, kwargs = fake_client.record[0]
    assert kind == "stream" and kwargs["fallbacks"] == "default"


@pytest.mark.asyncio
async def test_with_the_flag_one_batch_request_carries_the_same_call(fake_client, monkeypatch):
    monkeypatch.setattr(settings, "CLAUDE_USE_BATCH", True, raising=False)
    text = await claude_client.call_claude_json("task", "sys", SCHEMA, effort="high",
                                                context="transcript")
    assert text == '{"via": "batch"}'
    kinds = [kind for kind, _ in fake_client.record]
    assert kinds == ["batch"]
    (request,) = fake_client.record[0][1]
    params = request["params"]
    assert params["system"] == "sys" and params["model"] == settings.CLAUDE_SELECTION_MODEL
    assert params["output_config"] == {"effort": "high",
                                       "format": {"type": "json_schema", "schema": SCHEMA}}
    assert params["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "fallbacks" not in params and "betas" not in params and "stream" not in params


@pytest.mark.asyncio
async def test_a_batch_that_runs_too_long_is_cancelled_and_the_call_made_directly(fake_client, monkeypatch):
    monkeypatch.setattr(settings, "CLAUDE_USE_BATCH", True, raising=False)
    monkeypatch.setattr(settings, "CLAUDE_BATCH_TIMEOUT_MINUTES", 0, raising=False)
    fake_client.state["finish_after"] = None
    text = await claude_client.call_claude_json("task", "sys", SCHEMA)
    assert text == '{"via": "stream"}'
    assert [kind for kind, _ in fake_client.record] == ["batch", "cancel", "stream"]


@pytest.mark.asyncio
async def test_a_failed_batch_request_raises(fake_client, monkeypatch):
    monkeypatch.setattr(settings, "CLAUDE_USE_BATCH", True, raising=False)
    fake_client.state["result_type"] = "errored"
    with pytest.raises(RuntimeError, match="errored"):
        await claude_client.call_claude_json("task", "sys", SCHEMA)
