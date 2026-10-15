"""The TikTok Content Posting API client, against a fake transport.

These tests exist because the publish path used to return a video id it had
made up after a sleep. Everything asserted here is about proving an id can only
come back from TikTok's own response — or the call raises.
"""
import json
import time

import pytest

from config import settings
from services import tiktok_content_api as api


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeTransport:
    """Records every call; answers queued responses per URL fragment."""

    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def _answer(self, url, method, kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        # Deepest path fragment wins: the status URL also contains 'post/publish',
        # and both fragments are the same length.
        matches = [f for f in self.answers if f in url]
        if not matches:
            return FakeResponse({"error": {"code": "unexpected_url", "message": url}}, 404)
        payload = self.answers[max(matches, key=lambda f: (len(f), url.rfind(f)))]
        if isinstance(payload, tuple):
            return FakeResponse(payload[0], payload[1])
        return FakeResponse(payload)

    def post(self, url, **kwargs):
        return self._answer(url, "POST", kwargs)

    def put(self, url, **kwargs):
        return self._answer(url, "PUT", kwargs)

    def get(self, url, **kwargs):
        return self._answer(url, "GET", kwargs)

    def urls(self, method=None):
        return [c["url"] for c in self.calls if method is None or c["method"] == method]


@pytest.fixture
def secrets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_KEY", "test_key")
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_SECRET", "test_secret")
    # Most of these tests exercise DRAFT, which only a Direct-Post-approved app
    # can use; the upload-only reality is covered by its own tests below.
    monkeypatch.setattr(settings, "TIKTOK_POST_MODE", "DRAFT")
    monkeypatch.setattr(settings, "TIKTOK_SCOPES", "user.info.basic,video.publish")
    return tmp_path / "secrets"


@pytest.fixture
def connected(secrets_dir, monkeypatch):
    """A stored OAuth token, so access_token() never hits the network."""
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "tiktok_oauth.json").write_text(json.dumps({
        "access_token": "ACCESS", "refresh_token": "REFRESH", "open_id": "OID",
        "expires_at": time.time() + 3000, "username": "clipperevrything",
    }), encoding="utf-8")
    return secrets_dir


def _video(tmp_path, size=45, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    return str(path)


# --------------------------------------------------------------------------
# chunk planning
# --------------------------------------------------------------------------

def test_small_file_is_a_single_chunk(tmp_path):
    chunk, total = api.plan_chunks(3 * 1024 * 1024)
    assert (chunk, total) == (3 * 1024 * 1024, 1)


def test_chunk_count_rounds_down_and_the_last_chunk_holds_the_remainder(monkeypatch):
    # TikTok counts 45/20 as two chunks (20 + 25), not ceil()'s three. Sending
    # three gets the whole init rejected.
    monkeypatch.setattr(api, "CHUNK_MIN", 10)
    monkeypatch.setattr(api, "CHUNK_MAX", 20)
    assert api.plan_chunks(45) == (20, 2)
    assert api.plan_chunks(40) == (20, 2)
    assert api.plan_chunks(39) == (20, 1)


def test_chunk_count_stays_under_the_api_maximum(monkeypatch):
    monkeypatch.setattr(api, "CHUNK_MIN", 1)
    monkeypatch.setattr(api, "CHUNK_MAX", 10)
    monkeypatch.setattr(api, "MAX_CHUNKS", 5)
    chunk, total = api.plan_chunks(50)
    assert total <= 5
    assert chunk * total <= 50


def test_oversized_and_empty_files_are_refused_locally():
    with pytest.raises(api.TikTokAPIError):
        api.plan_chunks(api.MAX_VIDEO_BYTES + 1)
    with pytest.raises(api.TikTokAPIError):
        api.plan_chunks(0)


# --------------------------------------------------------------------------
# init payload
# --------------------------------------------------------------------------

def test_privacy_level_only_goes_with_a_real_post():
    direct = api.init_payload("caption", "PUBLIC_TO_EVERYONE", "DIRECT_PUBLISH", 100, 100, 1)
    assert direct["post_info"]["privacy_level"] == "PUBLIC_TO_EVERYONE"
    draft = api.init_payload("caption", "PUBLIC_TO_EVERYONE", "DRAFT", 100, 100, 1)
    assert "privacy_level" not in draft["post_info"]
    assert draft["source_info"] == {"source": "FILE_UPLOAD", "video_size": 100,
                                    "chunk_size": 100, "total_chunk_count": 1}


def test_caption_is_cut_to_the_api_limit():
    assert len(api.init_payload("a" * 5000, "SELF_ONLY", "DRAFT", 10, 10, 1)["post_info"]["title"]) == 2200


# --------------------------------------------------------------------------
# the publish flow
# --------------------------------------------------------------------------

def test_publish_sends_every_byte_once_then_waits_for_completion(connected, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "CHUNK_MIN", 10)
    monkeypatch.setattr(api, "CHUNK_MAX", 20)
    transport = FakeTransport({
        "post/publish": {"data": {"publish_id": "PUB1", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
        "status/fetch": {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["123"]}},
    })
    monkeypatch.setattr(api, "requests", transport)

    result = api.upload_video(_video(tmp_path, 45), "hi", "PUBLIC_TO_EVERYONE",
                              post_mode="DIRECT_PUBLISH", poll_interval=0, poll_timeout=10)

    assert [c["headers"]["Content-Range"] for c in transport.calls if c["method"] == "PUT"] == \
        ["bytes 0-19/45", "bytes 20-44/45"]
    assert b"".join(c["data"] for c in transport.calls if c["method"] == "PUT") == b"x" * 45
    assert result["video_id"] == "123"
    assert result["status"] == "PUBLISH_COMPLETE"
    assert result["tiktok_url"] == "https://www.tiktok.com/@clipperevrything/video/123"


def test_draft_has_no_url_because_nothing_is_public_yet(connected, tmp_path, monkeypatch):
    transport = FakeTransport({
        "post/publish": {"data": {"publish_id": "PUB2", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
        "status/fetch": {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["555"]}},
    })
    monkeypatch.setattr(api, "requests", transport)

    result = api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", poll_interval=0, poll_timeout=10)

    assert result["post_mode"] == "DRAFT"
    assert result["tiktok_url"] is None
    assert result["privacy_level"] is None
    assert result["video_id"] == "555"


def test_inbox_mode_uses_the_upload_init_endpoint(connected, tmp_path, monkeypatch):
    transport = FakeTransport({
        "video/init": {"data": {"publish_id": "PUB3", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
        "status/fetch": {"data": {"status": "SEND_TO_USER_INBOX"}},
    })
    monkeypatch.setattr(api, "requests", transport)

    result = api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY",
                              post_mode="UPLOAD", poll_interval=0, poll_timeout=10)
    assert any("post/publish/video/init" in u for u in transport.urls("POST"))
    assert result["status"] == "SEND_TO_USER_INBOX"


def test_processing_is_polled_until_it_stops(connected, tmp_path, monkeypatch):
    polls = [{"status": "PROCESSING_UPLOAD"}, {"status": "PROCESSING_UPLOAD"},
             {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["77"]}]
    calls = {"n": 0}

    class PollingTransport(FakeTransport):
        def post(self, url, **kwargs):
            if "status/fetch" in url:
                payload = polls[min(calls["n"], len(polls) - 1)]
                calls["n"] += 1
                self.calls.append({"method": "POST", "url": url, **kwargs})
                return FakeResponse({"data": payload})
            return super().post(url, **kwargs)

    monkeypatch.setattr(api, "requests", PollingTransport({
        "post/publish": {"data": {"publish_id": "PUB4", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
    }))
    result = api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", poll_interval=0, poll_timeout=10)
    assert calls["n"] == 3
    assert result["video_id"] == "77"


def test_a_refused_publish_raises_instead_of_inventing_an_id(connected, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "requests", FakeTransport({
        "post/publish": {"error": {"code": "error_input_incorrect_video_format",
                                   "message": "bad", "log_id": "L1"}},
    }))
    with pytest.raises(api.TikTokAPIError) as exc:
        api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", poll_interval=0, poll_timeout=10)
    assert exc.value.code == "error_input_incorrect_video_format"
    assert exc.value.log_id == "L1"


def test_a_failed_status_is_reported_as_failed(connected, tmp_path, monkeypatch):
    monkeypatch.setattr(api, "requests", FakeTransport({
        "post/publish": {"data": {"publish_id": "PUB5", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
        "status/fetch": {"data": {"status": "FAILED", "fail_reason": {"file_bad_format": True}}},
    }))
    result = api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", poll_interval=0, poll_timeout=10)
    assert result["status"] == "FAILED"
    assert result["video_id"] is None
    assert result["tiktok_url"] is None


def test_a_chunk_rejected_midway_stops_the_publish(connected, tmp_path, monkeypatch):
    class RejectingChunks(FakeTransport):
        def put(self, url, **kwargs):
            self.calls.append({"method": "PUT", "url": url, **kwargs})
            return FakeResponse(None, 416)

    monkeypatch.setattr(api, "CHUNK_MIN", 10)
    monkeypatch.setattr(api, "CHUNK_MAX", 20)
    transport = RejectingChunks({
        "post/publish": {"data": {"publish_id": "PUB6", "upload_url": "https://upload.test/chunk"}},
    })
    monkeypatch.setattr(api, "requests", transport)
    with pytest.raises(api.TikTokAPIError) as exc:
        api.upload_video(_video(tmp_path, 45), "hi", "SELF_ONLY", poll_interval=0, poll_timeout=10)
    assert "chunk 1/2" in str(exc.value)
    assert not [u for u in transport.urls("POST") if "status/fetch" in u], \
        "a half-sent file must never be reported as publishing"


def test_dry_run_plans_without_any_network_call(connected, tmp_path, monkeypatch):
    class Exploding:
        def __getattr__(self, _name):
            raise AssertionError("dry run must not touch the network")

    monkeypatch.setattr(api, "requests", Exploding())
    plan = api.upload_video(_video(tmp_path, 10), "caption", "SELF_ONLY", dry_run=True)
    assert plan["dry_run"] is True
    assert plan["init_payload"]["post_info"]["post_mode"] == "DRAFT"
    assert plan["bytes"] == 10


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def test_expired_access_token_is_refreshed_from_the_store(connected, monkeypatch):
    stored = json.loads((connected / "tiktok_oauth.json").read_text(encoding="utf-8"))
    stored["expires_at"] = time.time() - 1
    (connected / "tiktok_oauth.json").write_text(json.dumps(stored), encoding="utf-8")
    monkeypatch.setattr(api, "_request_token",
                        lambda form: {"access_token": "FRESH", "expires_in": 3600,
                                      "refresh_token": None, "open_id": "OID"})
    assert api.access_token() == "FRESH"
    saved = json.loads((connected / "tiktok_oauth.json").read_text(encoding="utf-8"))
    assert saved["refresh_token"] == "REFRESH", "a refresh response without a new refresh token must keep the old one"
    assert saved["username"] == "clipperevrything"


def test_missing_credentials_raise_rather_than_defaulting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_KEY", "")
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_SECRET", "")
    monkeypatch.setattr(settings, "STORAGE_ROOT", str(tmp_path / "empty"))
    with pytest.raises(api.TikTokAPIError) as exc:
        api.get_client_credentials()
    assert "client_key" in str(exc.value)


def test_oauth_error_from_the_token_endpoint_is_surfaced(monkeypatch):
    monkeypatch.setattr(api, "get_client_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(api, "requests", FakeTransport({
        "oauth/token": ({"error": "invalid_grant", "error_description": "code expired"}, 400),
    }))
    with pytest.raises(api.TikTokAPIError) as exc:
        api.exchange_code("CODE")
    assert "code expired" in str(exc.value)


# --------------------------------------------------------------------------
# PKCE — TikTok's consent screen refuses an authorize request without a challenge
# --------------------------------------------------------------------------

def test_pkce_challenge_is_tiktoks_hex_not_the_rfc_base64url():
    # RFC 7636 says base64url(-digest, unpadded); TikTok's Login Kit docs say
    # "you must use hex encoding of SHA256". A spec-correct challenge is refused
    # by the consent screen with code_challenge named as the problem, exactly like
    # a missing one — verified live 2026-10-15. Vector: RFC 7636 Appendix B verifier.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert api.pkce_challenge(verifier) == \
        "13d31e961a1ad8ec2f16b10c4c982e0876a878ad6df144566ee1894acb70f9c3"


def test_generated_verifier_is_within_the_pkce_range():
    verifier = api.new_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert all(char in "0123456789abcdef" for char in verifier)


def test_authorize_url_carries_the_challenge_only_when_one_is_given(secrets_dir):
    assert "code_challenge" not in api.authorization_url(state="S")
    with_challenge = api.authorization_url(state="S", code_challenge="CHAL")
    assert "code_challenge=CHAL" in with_challenge
    assert "code_challenge_method=S256" in with_challenge


def test_the_consent_screen_asks_for_the_scopes_the_app_holds(secrets_dir, monkeypatch):
    # An app with only "Upload to TikTok" has no video.publish in its scope list,
    # and asking for a scope it lacks makes the consent screen refuse everything.
    monkeypatch.setattr(settings, "TIKTOK_SCOPES", "user.info.basic,video.upload")
    url = api.authorization_url(state="S", code_challenge="CHAL")
    assert "video.upload" in url
    assert "video.publish" not in url


def test_draft_and_direct_publish_are_refused_locally_without_video_publish(secrets_dir, monkeypatch, tmp_path):
    # Both go through the direct-post init endpoint. Failing here, before any
    # network call, beats a TikTok rejection that names some other parameter.
    monkeypatch.setattr(settings, "TIKTOK_SCOPES", "user.info.basic,video.upload")
    for mode in ("DRAFT", "DIRECT_PUBLISH"):
        with pytest.raises(api.TikTokAPIError) as exc:
            api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", post_mode=mode, dry_run=True)
        assert "video.publish" in str(exc.value)


def test_upload_mode_needs_no_direct_post_rights(secrets_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_SCOPES", "user.info.basic,video.upload")
    plan = api.upload_video(_video(tmp_path, 10), "hi", "SELF_ONLY", post_mode="UPLOAD", dry_run=True)
    assert plan["init_url"].endswith("/v2/post/publish/video/init/")


def test_the_token_exchange_sends_the_verifier_that_paired_with_the_challenge(secrets_dir, monkeypatch):
    transport = FakeTransport({
        "oauth/token": {"access_token": "A", "refresh_token": "R", "expires_in": 3600, "open_id": "O"},
        "user/info": {"data": {"user_name": "clipperevrything"}},
    })
    monkeypatch.setattr(api, "requests", transport)
    api.exchange_code("CODE", code_verifier="VERIFIER")
    token_call = next(c for c in transport.calls if c["url"].endswith("oauth/token/"))
    assert token_call["data"]["code_verifier"] == "VERIFIER"


# --------------------------------------------------------------------------
# the service layer the worker calls
# --------------------------------------------------------------------------

def test_a_stored_session_id_is_not_reported_as_authenticated(secrets_dir, monkeypatch):
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "tiktok_token.json").write_text(
        json.dumps({"username": "@ClippinEvrything", "session_id": "BBO8"}), encoding="utf-8")
    from services.tiktok_upload import get_tiktok_auth_status
    status = get_tiktok_auth_status()
    assert status["authenticated"] is False
    assert status["has_token"] is False
    assert status["auth_type"] == "session_id"
    assert "cannot post" in status["note"]


def test_upload_clip_to_tiktok_refuses_without_oauth(tmp_path, secrets_dir, monkeypatch):
    monkeypatch.setattr(settings, "WATERMARK_HANDLE", "@clips")
    from services.tiktok_upload import upload_clip_to_tiktok
    with pytest.raises(ValueError) as exc:
        upload_clip_to_tiktok(video_path=_video(tmp_path, 10), title="t", description="d")
    assert "not connected through OAuth" in str(exc.value)


def test_a_successful_publish_paces_the_next_one(connected, tmp_path, monkeypatch):
    import utils.upload_pacing as pacing
    monkeypatch.setattr(settings, "UPLOAD_MIN_GAP_SECONDS", 900, raising=False)
    pacing.reset("tiktok")
    monkeypatch.setattr(api, "requests", FakeTransport({
        "post/publish": {"data": {"publish_id": "PUB7", "upload_url": "https://upload.test/chunk"}},
        "upload.test": {"data": {}},
        "status/fetch": {"data": {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": ["9"]}},
    }))
    from services.tiktok_upload import upload_clip_to_tiktok
    monkeypatch.setattr(settings, "WATERMARK_HANDLE", "@clips")
    res = upload_clip_to_tiktok(video_path=_video(tmp_path, 10), title="Title",
                                description="d", hashtags=["#x"], privacy_level="PUBLIC_TO_EVERYONE")
    assert res["video_id"] == "9"
    assert pacing.seconds_to_wait("tiktok") > 0, "publishes must space themselves"


def test_visibility_translates_to_tiktok_privacy_values():
    from services.tiktok_upload import tiktok_privacy_for
    assert tiktok_privacy_for("public") == "PUBLIC_TO_EVERYONE"
    assert tiktok_privacy_for("PRIVATE") == "SELF_ONLY"
    assert tiktok_privacy_for("unlisted") in api.PRIVACY_LEVELS
    with pytest.raises(ValueError):
        tiktok_privacy_for("PUBLIC_TO_SELF")  # the value the old code invented


# --------------------------------------------------------------------------
# the endpoints a browser actually hits
# --------------------------------------------------------------------------

def _api_client():
    from fastapi.testclient import TestClient
    from main import app
    return TestClient(app)


def test_the_auth_url_endpoint_offers_a_pkce_challenge(secrets_dir, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_REDIRECT_URI",
                        "http://localhost:5001/api/tiktok/oauth2callback")
    response = _api_client().post("/api/tiktok/auth-url")
    assert response.status_code == 200
    auth_url = response.json()["auth_url"]
    assert "code_challenge=" in auth_url, "TikTok rejects an authorize request without PKCE"
    assert "code_challenge_method=S256" in auth_url
    offered = auth_url.split("code_challenge=")[1].split("&")[0]
    assert len(offered) == 64 and all(char in "0123456789abcdef" for char in offered), \
        "TikTok wants the hex digest; the RFC's base64url form is refused"
    assert api.load_token() is None, "offering a consent link must not store anything"


def test_the_callback_refuses_a_state_the_server_does_not_know(secrets_dir):
    # A restart between opening the link and landing here loses the verifier, so
    # the exchange could only fail with an opaque invalid_grant from TikTok.
    response = _api_client().get("/api/tiktok/oauth2callback?code=CODE&state=never-issued")
    assert response.status_code == 400
    assert "no longer known to the server" in response.text
