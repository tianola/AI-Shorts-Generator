"""The router retries a dead OmniRoute attempt once before giving up on it.

`auto/*` is a pooled route: each call resolves to a different upstream, so one
timeout says nothing about the next attempt. Before this, a single
`httpx.ReadTimeout` dropped the whole call to Ollama — which is not running on
this box — and clip selection silently degraded to evenly-spaced grid clips
(measured on job 110). Retrying the transport failure once keeps the analysis.

A 200 whose body carries no JSON is deliberately NOT retried: the same model
answering in prose will answer in prose again, so a second attempt only spends
the latency. Retrying belongs to the transport, not to the content.

Everything here runs with the model chain switched off, so a post count means a
retry. What happens when a leg runs out is test_selection_model_chain.py.
"""
import httpx
import pytest

from config import settings
from services import llm_router


class _Timeout(httpx.ReadTimeout):
    pass


class _FakeClient:
    """Stand-in for httpx.AsyncClient. `outcomes` is consumed per POST, in order."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.posts = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):
        self.posts.append({"url": url, "payload": json})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _Response:
    def __init__(self, payload, status=200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise httpx.HTTPStatusError("refused", request=None, response=None)

    def json(self):
        return self._payload


def _omniroute_reply(content):
    return _Response({"choices": [{"message": {"content": content}}]})


def _install(monkeypatch, outcomes):
    """Route every httpx.AsyncClient — OmniRoute and Ollama alike — to one fake."""
    client = _FakeClient(outcomes)
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "AI_PROVIDER", "omniroute")
    monkeypatch.setattr(settings, "OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", "some/pinned-model")
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", "")
    monkeypatch.setattr(settings, "OLLAMA_URL", "http://localhost:11434")
    return client


def _omniroute_posts(client):
    return [p for p in client.posts if "20128" in p["url"]]


@pytest.mark.asyncio
async def test_a_timeout_retries_the_omniroute_call_once_before_falling_back(monkeypatch):
    client = _install(monkeypatch, [
        _Timeout("read timeout"),
        _omniroute_reply('{"clips": [{"start": 12.0}]}'),
    ])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == '{"clips": [{"start": 12.0}]}', "the retry's reply should be used"
    assert len(_omniroute_posts(client)) == 2, "one retry, then the result"


@pytest.mark.asyncio
async def test_a_second_timeout_stops_retrying_and_reaches_the_ollama_fallback(monkeypatch):
    client = _install(monkeypatch, [
        _Timeout("read timeout"),
        _Timeout("read timeout"),
        _Timeout("connection refused"),
    ])

    with pytest.raises(RuntimeError, match="All LLM backends failed"):
        await llm_router.call_llm_json(prompt="p", system="s")

    assert len(_omniroute_posts(client)) == 2, "exactly one retry, not a retry loop"


@pytest.mark.asyncio
async def test_a_prose_reply_is_passed_through_once_without_a_retry(monkeypatch):
    """Guard: the retry belongs to the transport, not to the content.

    A model that answers in prose answers in prose again, so a second attempt
    would only spend the latency. Parsing stays the caller's job.
    """
    client = _install(monkeypatch, [
        _omniroute_reply("Sorry, I cannot help with that."),
    ])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == "Sorry, I cannot help with that."
    assert len(_omniroute_posts(client)) == 1, "a refusal is an answer, not a transport failure"
    assert [p for p in client.posts if "11434" in p["url"]] == [], "never reached the fallback"

