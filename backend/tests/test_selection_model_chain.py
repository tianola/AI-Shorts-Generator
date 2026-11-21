"""OmniRoute clip selection is a chain of free models, not one point of failure.

Every free model can be unavailable: quota spent, upstream down, or a route that
answers with a thinking block instead of a string. Job 110 shipped its grid
because such a reply had nowhere to go. The router's next step was Ollama, which
is not running on this box, so `select_clips` caught the raise and returned
evenly spaced windows - clips that looked AI-selected and were not.

The chain gives that reply somewhere to go. `OMNIROUTE_MODEL` is the head, the
best free model measured on the real selection path, and
`OMNIROUTE_MODEL_FALLBACKS` holds the next ones in order. A leg is skipped when
its transport dies twice (the retry that shipped with it) or when it answers
with no JSON object at all; whatever a later leg produces is used, and the log
names the leg that produced it.

Nothing here changes the 120 s per-request timeout - raising it was declined -
so the most a chain can cost is one more dead model's latency.
"""

import httpx
import pytest
from config import Settings, settings
from loguru import logger
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


def _reply(content):
    """A 200 whose message.content is exactly `content` - string, list, or nothing."""
    return _Response({"choices": [{"message": {"content": content}}]})


def _thinking_block():
    """The job 110 shape: a 200 whose content is a list of reasoning dicts."""
    return _reply([{"type": "thinking", "thinking": "considering the windows..."}])


def _install(monkeypatch, outcomes, legs):
    """Route every httpx.AsyncClient — OmniRoute and Ollama alike — to one fake."""
    client = _FakeClient(outcomes)
    monkeypatch.setattr(llm_router.httpx, "AsyncClient", client)
    monkeypatch.setattr(settings, "AI_PROVIDER", "omniroute")
    monkeypatch.setattr(settings, "OMNIROUTE_BASE_URL", "http://localhost:20128/v1")
    monkeypatch.setattr(settings, "OMNIROUTE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OLLAMA_URL", "http://localhost:11434")
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", legs[0])
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", ",".join(legs[1:]))
    return client


def _models_asked(client):
    return [p["payload"]["model"] for p in client.posts if "20128" in p["url"]]


def _reached_ollama(client):
    return [p for p in client.posts if "11434" in p["url"]]


HEAD = "head/first-model"
SECOND = "head/second-model"
THIRD = "head/third-model"


# ------------------------------------------------------------------ chain ----
def test_the_chain_is_the_head_then_the_fallbacks_in_order(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", HEAD)
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", f"{SECOND}, {THIRD}")

    assert llm_router.omniroute_chain() == [HEAD, SECOND, THIRD]


def test_an_explicit_model_heads_the_chain(monkeypatch):
    """`clip_selector` passes the model itself, so the chain must start there.

    Building the chain from settings.OMNIROUTE_MODEL alone would leave every
    production call single-legged, because the callers name the model explicitly.
    """
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", f"{SECOND},{THIRD}")

    assert llm_router.omniroute_chain("named/other-model") == [
        "named/other-model", SECOND, THIRD,
    ]


def test_a_fallback_that_duplicates_the_head_is_tried_once(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", HEAD)
    repeated = f"{HEAD},{SECOND},{HEAD}"
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", repeated)

    assert llm_router.omniroute_chain() == [HEAD, SECOND]


def test_blank_fallback_entries_do_not_become_legs(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", HEAD)
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", " , ")

    assert llm_router.omniroute_chain() == [HEAD]


def test_no_fallbacks_configured_means_the_head_only(monkeypatch):
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL", HEAD)
    monkeypatch.setattr(settings, "OMNIROUTE_MODEL_FALLBACKS", "")

    assert llm_router.omniroute_chain() == [HEAD]


# --------------------------------------------------------- shipped defaults ----
# Measured on job 110's own source, on the 91 653-char production prompt, several
# rounds per model (2026-11-12). Every free id that answers at all fills the same
# 10 clips and the same 17 schema keys with legal enums, so what separates them is
# latency and whether the clip lengths obey the selector's own 20-30 s rule:
#
#   model                               latency      schema    20-30s rule
#   agy/gemini-3-flash                7.6-9.9s     170/170     6 to 10 of 10
#   agy/gemini-3.1-flash-lite       10.1-17.2s     170/170     10 of 10
#   antigravity/gemini-3.1-flash-lite 13.1-17.6s   170/170     10 of 10
#
# The head keeps its place on latency; its round-to-round length spread belongs to
# the model's sampling, not to which leg ran. Not legs, and why:
#
#   nvidia/poolside/laguna-xs-2.1   45.4-116.2s - one round of the 120 s timeout -
#                                 with only 2 of 10 clips inside the length rule.
#   agy/gemini-3.6-flash-low        HTTP 400 on every attempt, both rounds.
#   antigravity/gemini-3.1-flash-image  HTTP 400 on every attempt, both rounds.
#   free-ai/* and openrouter/*:free the gateway itself raised a DNS error
#                                 ([Errno 11001]) in 0.6 s; unreachable here.
#   free/coding                     answered in 10.2 s but is a combo, and a combo
#                                 re-resolves upstream per call - the thing the pin
#                                 removed from the head, so it does not come back
#                                 through the fallbacks either.
# The free-only priority chains on the local OmniRoute (2026-11-16): free/shorts,
# built for clip selection from a 38-model benchmark on job 111's prompts, then
# the general free/best (5 model-picked clips in 70 s on job 111).
MEASURED_CHAIN = [
    "free/shorts",
    "free/best",
]
# auto/* resolves to an arbitrary upstream per call: auto/best-free timed out on
# the job 111 prompt, and an earlier auto/* resolution cost job 110 its selection.
COMBO_PREFIXES = ("auto/", "best/")


def _defaults(monkeypatch):
    """The shipped defaults with no env file and no inherited variable."""
    monkeypatch.delenv("OMNIROUTE_MODEL", raising=False)
    monkeypatch.delenv("OMNIROUTE_MODEL_FALLBACKS", raising=False)
    fresh = Settings(_env_file=None)
    monkeypatch.setattr(llm_router, "settings", fresh)
    return fresh


def test_the_default_chain_has_no_auto_routed_combo(monkeypatch):
    _defaults(monkeypatch)
    for leg in llm_router.omniroute_chain():
        assert not leg.startswith(COMBO_PREFIXES), (
            f"{leg!r} is a gateway combo, not a model: every call would resolve to "
            f"a different upstream, which is how job 110 lost its selection."
        )


def test_the_text_block_of_a_thinking_reply_is_the_answer():
    content = [
        {"type": "thinking", "thinking": "considering the windows..."},
        {"type": "text", "text": '{"clips": []}'},
    ]
    assert llm_router._json_object_in(content) == '{"clips": []}'
    assert llm_router._json_object_in([{"type": "thinking", "thinking": "{not this}"}]) == ""


@pytest.mark.asyncio
async def test_the_omniroute_selection_call_leaves_room_for_reasoning(monkeypatch):
    """nemotron-3-ultra returned no JSON at 3500 tokens and ten windows at 12000."""
    from services import clip_selector

    seen = {}

    async def fake_llm(prompt, system, model=None, temperature=0.4, max_tokens=2000):
        seen["max_tokens"] = max_tokens
        return '{"clips": []}'

    monkeypatch.setattr(clip_selector, "call_llm_json", fake_llm)
    _, path = await clip_selector._selection_model_json("p", {}, 3500)
    assert path == "ai"
    assert seen["max_tokens"] >= 12000


def test_the_default_chain_is_the_measured_one(monkeypatch):
    _defaults(monkeypatch)
    assert llm_router.omniroute_chain() == MEASURED_CHAIN


# ------------------------------------------------------------- the router ----
@pytest.mark.asyncio
async def test_a_head_that_answers_in_prose_hops_to_the_next_model(monkeypatch):
    """A refusal is not an answer for a JSON caller, so it must not end the call.

    `_extract_json_text` hands prose back unchanged, which used to make prose look
    like success: the router returned it and the caller's json.loads raised.
    """
    client = _install(monkeypatch, [
        _reply("Sorry, I cannot help with that."),
        _reply('{"clips": [{"start": 12.0}]}'),
    ], [HEAD, SECOND])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == '{"clips": [{"start": 12.0}]}', "the fallback's JSON is used"
    assert _models_asked(client) == [HEAD, SECOND]
    assert not _reached_ollama(client), "a live chain must not fall through to Ollama"


@pytest.mark.asyncio
async def test_a_head_that_answers_with_a_thinking_block_hops_the_chain(monkeypatch):
    """The exact job 110 failure: a 200 whose content is not a string at all."""
    client = _install(monkeypatch, [
        _thinking_block(),
        _reply('{"clips": [{"start": 99.0}]}'),
    ], [HEAD, SECOND])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == '{"clips": [{"start": 99.0}]}'
    assert _models_asked(client) == [HEAD, SECOND]
    assert not _reached_ollama(client)


@pytest.mark.asyncio
async def test_a_head_that_times_out_twice_hops_to_the_next_model(monkeypatch):
    """The per-model retry still applies: the head gets its retry, then the chain moves on."""
    client = _install(monkeypatch, [
        _Timeout("read timeout"),
        _Timeout("read timeout"),
        _reply('{"clips": [{"start": 5.0}]}'),
    ], [HEAD, SECOND])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == '{"clips": [{"start": 5.0}]}'
    assert _models_asked(client) == [HEAD, HEAD, SECOND]
    assert not _reached_ollama(client)


@pytest.mark.asyncio
async def test_a_prose_head_does_not_burn_its_retry_on_the_way_out(monkeypatch):
    """Content failures hop immediately; only transport failures are retried.

    Re-asking the model that just refused spends 120 s to hear the same word.
    """
    client = _install(monkeypatch, [
        _reply("I cannot do that."),
        _reply('{"clips": []}'),
    ], [HEAD, SECOND])

    await llm_router.call_llm_json(prompt="p", system="s")

    assert _models_asked(client) == [HEAD, SECOND], "one call per leg for a content hop"


@pytest.mark.asyncio
async def test_a_chain_that_only_answers_in_prose_returns_the_first_reply(monkeypatch):
    """The caller's contract is unchanged: prose comes back, no RuntimeError.

    Which matters because the caller turns a raise into a silent grid; a prose
    reply at least reaches its own error message with the model's words in it.
    """
    client = _install(monkeypatch, [
        _reply("Sorry, I cannot help with that."),
        _reply("No."),
    ], [HEAD, SECOND])

    result = await llm_router.call_llm_json(prompt="p", system="s")

    assert result == "Sorry, I cannot help with that.", "the first reply is kept"
    assert not _reached_ollama(client)


@pytest.mark.asyncio
async def test_every_leg_dying_at_the_transport_reaches_ollama(monkeypatch):
    """With no reply from anyone, the old path is intact — including its raise."""
    client = _install(monkeypatch, [
        _Timeout("read timeout"), _Timeout("read timeout"),
        _Timeout("read timeout"), _Timeout("read timeout"),
        _Timeout("connection refused"),
    ], [HEAD, SECOND])

    with pytest.raises(RuntimeError, match="All LLM backends failed"):
        await llm_router.call_llm_json(prompt="p", system="s")

    assert _models_asked(client) == [HEAD, HEAD, SECOND, SECOND]
    assert _reached_ollama(client), "nothing answered, so the fallback still runs"


@pytest.mark.asyncio
async def test_the_log_names_the_leg_that_answered(monkeypatch):
    """Which model produced the clips is invisible in the clips themselves.

    A job whose head was down looks exactly like a job whose head answered, so the
    log line is the only evidence that the chain moved — the thing to read when
    selection quality changes for no apparent reason.
    """
    lines = []
    sink = logger.add(lines.append, level="INFO")
    try:
        _install(monkeypatch, [
            _Timeout("read timeout"), _Timeout("read timeout"),
            _reply('{"clips": [{"start": 12.0}]}'),
        ], [HEAD, SECOND])
        await llm_router.call_llm_json(prompt="p", system="s")
    finally:
        logger.remove(sink)

    assert any(HEAD in line for line in lines), "the dead head is named in the log"
    assert any(SECOND in line for line in lines), "the leg that answered is named too"
