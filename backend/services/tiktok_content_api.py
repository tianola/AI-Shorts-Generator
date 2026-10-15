"""TikTok Content Posting API v2 client.

This replaces the publish path that used to invent a video id after a
``time.sleep(0.6)``. Everything here either talks to TikTok or raises, so a
``clip.tiktok_video_id`` written through this module names a real post.

Endpoints (Content Posting API v2):

  authorize  GET  https://www.tiktok.com/v2/auth/authorize/
  token      POST https://open.tiktokapis.com/v2/oauth/token/
  init       POST https://open.tiktokapis.com/v2/post/publish/            (DIRECT_PUBLISH / DRAFT)
             POST https://open.tiktokapis.com/v2/post/publish/video/init/ (UPLOAD, lands in the user's inbox)
  chunk      PUT  {upload_url from init}   Content-Range: bytes {first}-{last}/{total}
  status     POST https://open.tiktokapis.com/v2/post/publish/status/fetch/
  user info  GET  https://open.tiktokapis.com/v2/user/info/

Credentials are a TikTok for Developers *app* (client key + client secret) with
the Content Posting API product, the scopes that product actually granted the
app (``video.upload`` by default, ``video.publish`` only after Direct Post is
approved — see ``TIKTOK_SCOPES``), plus an OAuth authorisation from the account
that should own the posts. A browser ``session_id`` cannot do any of this, which
is why the legacy ``storage/secrets/tiktok_token.json`` is no longer treated as
publishable.

The consent screen **requires PKCE**: without ``code_challenge`` it refuses with
"code_challenge" as the named problem and returns no code at all (hit live on
2026-10-15 with a real app), so ``/auth-url`` generates a verifier, sends its
S256 challenge, and the callback hands the verifier to the token exchange.
TikTok's flavour of S256 is the *hex* digest of SHA-256 rather than the base64url
of RFC 7636 — a spec-correct challenge is rejected the same way a missing one is.
"""
import hashlib
import json
import math
import os
import secrets
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from utils.logger import logger

AUTHORIZE_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_ROOT = "https://open.tiktokapis.com"
# The token endpoint is on the API host, not ``identity.tiktok.com`` — that name
# is not published in DNS at all, so an exchange pointed there fails before any
# request is sent.
TOKEN_URL = f"{API_ROOT}/v2/oauth/token/"
USER_INFO_URL = f"{API_ROOT}/v2/user/info/"
DIRECT_POST_INIT_URL = f"{API_ROOT}/v2/post/publish/"
INBOX_INIT_URL = f"{API_ROOT}/v2/post/publish/video/init/"
STATUS_URL = f"{API_ROOT}/v2/post/publish/status/fetch/"

# ``video.publish`` is only granted to apps approved for the Content Posting
# API's *Direct Post* feature; an app that has only "Upload to TikTok" sees
# ``video.upload`` in its scope list. Asking for a scope the app does not have
# makes the consent screen refuse the request outright — hit live on 2026-10-15,
# where the app's own scope list had no ``video.publish`` at all.
BASIC_SCOPE = "user.info.basic"
UPLOAD_SCOPE = "video.upload"
PUBLISH_SCOPE = "video.publish"


def requested_scopes() -> str:
    """The scopes to put on the consent screen, as configured for this app."""
    from config import settings
    return (settings.TIKTOK_SCOPES or f"{BASIC_SCOPE},{UPLOAD_SCOPE}").strip()


def has_direct_post_rights() -> bool:
    return PUBLISH_SCOPE in [s.strip() for s in requested_scopes().split(",")]


# Content Posting API privacy enum. TikTok has no 'unlisted': the closest thing
# to a link-only post is follower-only, and 'private' means a true self-only post.
PRIVACY_LEVELS = ("PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY")

# UPLOAD parks the clip in the account's inbox and needs only ``video.upload``.
# DRAFT and DIRECT_PUBLISH both go through the direct-post init endpoint and
# therefore need the app to hold ``video.publish``.
POST_MODES = ("DIRECT_PUBLISH", "DRAFT", "UPLOAD")

MAX_VIDEO_BYTES = 4 * 1024 * 1024 * 1024  # the API's 4 GB ceiling
CHUNK_MIN = 5 * 1024 * 1024
CHUNK_MAX = 64 * 1024 * 1024
MAX_CHUNKS = 1000

TERMINAL_STATUSES = ("PUBLISH_COMPLETE", "FAILED", "SEND_TO_USER_INBOX")


class TikTokAPIError(RuntimeError):
    """A refusal from TikTok, or a response we could not trust."""

    def __init__(self, message: str, *, code: Optional[str] = None, log_id: Optional[str] = None,
                 status_code: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.log_id = log_id
        self.status_code = status_code

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.code:
            parts.append(f"code={self.code}")
        if self.log_id:
            parts.append(f"log_id={self.log_id}")
        if self.status_code:
            parts.append(f"HTTP={self.status_code}")
        return " | ".join(parts)


# --------------------------------------------------------------------------
# Credentials and token store
# --------------------------------------------------------------------------

def _secrets_dir() -> Path:
    from config import settings
    path = Path(settings.STORAGE_ROOT) / "secrets"
    path.mkdir(parents=True, exist_ok=True)
    return path


def client_credentials_path() -> Path:
    return _secrets_dir() / "tiktok_client.json"


def token_path() -> Path:
    return _secrets_dir() / "tiktok_oauth.json"


def get_client_credentials() -> Tuple[str, str]:
    """Client key/secret from the environment, else from tiktok_client.json."""
    from config import settings
    key = (settings.TIKTOK_CLIENT_KEY or "").strip()
    secret = (settings.TIKTOK_CLIENT_SECRET or "").strip()
    if key and secret:
        return key, secret

    path = client_credentials_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TikTokAPIError(f"Could not read {path}: {exc}")
        key = key or (data.get("client_key") or "").strip()
        secret = secret or (data.get("client_secret") or "").strip()
    if not key or not secret:
        raise TikTokAPIError(
            "TikTok app credentials are not configured. Create an app at TikTok for Developers, "
            "enable the Content Posting API, and put its client_key/client_secret in "
            "TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET (or storage/secrets/tiktok_client.json)."
        )
    return key, secret


def credentials_configured() -> bool:
    try:
        get_client_credentials()
        return True
    except TikTokAPIError:
        return False


def save_client_credentials(client_key: str, client_secret: str) -> None:
    payload = {"client_key": (client_key or "").strip(), "client_secret": (client_secret or "").strip()}
    if not payload["client_key"] or not payload["client_secret"]:
        raise TikTokAPIError("Both client_key and client_secret are required.")
    client_credentials_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_token() -> Optional[Dict[str, Any]]:
    path = token_path()
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(f"Ignoring unreadable TikTok OAuth token file: {exc}")
        return None


def save_token(token: Dict[str, Any]) -> Dict[str, Any]:
    token_path().write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def _token_from_response(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise an OAuth token response into our on-disk shape."""
    access = payload.get("access_token")
    if not access:
        raise TikTokAPIError(
            payload.get("error_description") or "TikTok returned no access_token.",
            code=payload.get("error"),
            log_id=payload.get("log_id"),
        )
    return {
        "access_token": access,
        "refresh_token": payload.get("refresh_token"),
        "open_id": payload.get("open_id"),
        "scope": payload.get("scope"),
        "expires_at": time.time() + float(payload.get("expires_in") or 3600) - 60,
        "refresh_expires_at": (time.time() + float(payload["refresh_expires_in"]) if payload.get("refresh_expires_in") else None),
        "obtained_at": time.time(),
        "username": None,
    }


def redirect_uri() -> str:
    from config import settings
    return settings.TIKTOK_REDIRECT_URI


def authorization_url(state: Optional[str] = None, target_redirect_uri: Optional[str] = None,
                      code_challenge: Optional[str] = None) -> str:
    """The URL to send the user to once. TikTok redirects back with ?code=.

    ``code_challenge`` is not optional in practice — TikTok's consent screen
    rejects a request without it — but it stays a parameter so a caller that
    already holds a verifier pair is the one that decides.
    """
    from urllib.parse import urlencode
    key, _secret = get_client_credentials()
    params = {
        "client_key": key,
        "scope": requested_scopes(),
        "response_type": "code",
        "redirect_uri": target_redirect_uri or redirect_uri(),
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def new_state() -> str:
    return secrets.token_urlsafe(16)


def new_code_verifier() -> str:
    """64 hex characters — inside RFC 7636's 43-128 range, and a charset TikTok
    cannot object to."""
    return secrets.token_hex(32)


def pkce_challenge(code_verifier: str) -> str:
    """TikTok's S256 is ``hexdigest`` of the SHA-256, not RFC 7636's base64url.

    A spec-correct base64url challenge is *named* by the consent screen as a bad
    ``code_challenge`` even though it is present, so this deviates from the RFC on
    purpose. Changing it back re-breaks the connection.
    """
    return hashlib.sha256(code_verifier.encode("ascii")).hexdigest()


def _request_token(form: Dict[str, Any]) -> Dict[str, Any]:
    key, secret = get_client_credentials()
    body = {"client_key": key, "client_secret": secret, **form}
    try:
        resp = requests.post(TOKEN_URL, data=body, timeout=(10, 30))
    except requests.RequestException as exc:
        raise TikTokAPIError(f"Could not reach TikTok's token endpoint: {exc}")
    try:
        payload = resp.json()
    except ValueError:
        raise TikTokAPIError(f"TikTok token endpoint returned non-JSON (HTTP {resp.status_code}).",
                             status_code=resp.status_code)
    if resp.status_code >= 400 or payload.get("error"):
        raise TikTokAPIError(
            payload.get("error_description") or f"TikTok OAuth failed (HTTP {resp.status_code}).",
            code=payload.get("error"), log_id=payload.get("log_id"), status_code=resp.status_code,
        )
    return payload


def exchange_code(code: str, target_redirect_uri: Optional[str] = None,
                  code_verifier: Optional[str] = None) -> Dict[str, Any]:
    """Turn an authorization code into a stored token.

    The verifier must be the one whose challenge went up to the consent screen,
    otherwise TikTok answers `invalid_grant`.
    """
    form: Dict[str, Any] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": target_redirect_uri or redirect_uri(),
    }
    if code_verifier:
        form["code_verifier"] = code_verifier
    payload = _request_token(form)
    token = save_token(_token_from_response(payload))
    try:
        token["username"] = fetch_username(token["access_token"])
        save_token(token)
    except TikTokAPIError as exc:
        # Posting works without the profile call; do not lose a good token over it.
        logger.warning(f"TikTok connected but the profile lookup failed: {exc}")
    return token


def refresh_stored_token() -> Dict[str, Any]:
    stored = load_token()
    if not stored or not stored.get("refresh_token"):
        raise TikTokAPIError("No TikTok refresh token — reconnect the account through /api/tiktok/auth-url.")
    payload = _request_token({"grant_type": "refresh_token", "refresh_token": stored["refresh_token"]})
    token = _token_from_response(payload)
    token["username"] = stored.get("username")
    # TikTok may omit the refresh token on refresh; keep the long-lived one.
    if not token.get("refresh_token"):
        token["refresh_token"] = stored.get("refresh_token")
    return save_token(token)


def access_token(force_refresh: bool = False) -> str:
    stored = load_token()
    if not stored or not stored.get("access_token"):
        raise TikTokAPIError(
            "TikTok is not connected through OAuth. Authorise the account once at "
            "/api/tiktok/auth-url — a browser session_id cannot publish."
        )
    if force_refresh or time.time() >= float(stored.get("expires_at") or 0):
        stored = refresh_stored_token()
    return stored["access_token"]


def fetch_username(token: str) -> Optional[str]:
    payload = _get(USER_INFO_URL, token, params={"fields": "user_name,avatar_url"})
    data = payload.get("data") or {}
    return data.get("user_name")


def connection_status() -> Dict[str, Any]:
    """What the settings page needs to know, without ever exposing a token."""
    from config import settings
    stored = load_token()
    return {
        "client_configured": credentials_configured(),
        "connected": bool(stored and stored.get("access_token")),
        "username": (stored or {}).get("username"),
        "open_id": (stored or {}).get("open_id"),
        "scope": (stored or {}).get("scope"),
        "expires_at": (stored or {}).get("expires_at"),
        "token_path": str(token_path()),
        "post_mode": settings.TIKTOK_POST_MODE,
        "redirect_uri": settings.TIKTOK_REDIRECT_URI,
    }


def disconnect() -> None:
    for path in (token_path(),):
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------------
# HTTP helpers
# --------------------------------------------------------------------------

def _check(payload: Dict[str, Any], url: str, status_code: int) -> Dict[str, Any]:
    """TikTok answers 200 even when it refused; the error object is the truth."""
    error = payload.get("error") or {}
    if error.get("code") and error.get("code") != "ok":
        raise TikTokAPIError(f"TikTok refused {url}: {error.get('message') or error.get('code')}",
                             code=error.get("code"), log_id=error.get("log_id"), status_code=status_code)
    if status_code >= 400:
        raise TikTokAPIError(f"TikTok returned HTTP {status_code} for {url}.", status_code=status_code)
    return payload


def _post_json(url: str, token: str, payload: Dict[str, Any], timeout: Tuple[float, float] = (10, 60)) -> Dict[str, Any]:
    try:
        resp = requests.post(url, json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=UTF-8"})
    except requests.RequestException as exc:
        raise TikTokAPIError(f"Could not reach {url}: {exc}")
    try:
        body = resp.json()
    except ValueError:
        raise TikTokAPIError(f"{url} returned non-JSON (HTTP {resp.status_code}).", status_code=resp.status_code)
    return _check(body, url, resp.status_code)


def _get(url: str, token: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    try:
        resp = requests.get(url, params=params, timeout=(10, 30),
                            headers={"Authorization": f"Bearer {token}"})
    except requests.RequestException as exc:
        raise TikTokAPIError(f"Could not reach {url}: {exc}")
    try:
        body = resp.json()
    except ValueError:
        raise TikTokAPIError(f"{url} returned non-JSON (HTTP {resp.status_code}).", status_code=resp.status_code)
    return _check(body, url, resp.status_code)


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------

def plan_chunks(video_size: int, wanted_chunk: Optional[int] = None) -> Tuple[int, int]:
    """(chunk_size, total_chunk_count) for a file of ``video_size`` bytes.

    TikTok counts chunks by rounding *down*: the last chunk absorbs the
    remainder and may therefore be up to twice as large as chunk_size (hence
    its 128 MB final-chunk allowance). Rounding up gets the init rejected.
    """
    if video_size <= 0:
        raise TikTokAPIError("Refusing to publish an empty video file.")
    if video_size > MAX_VIDEO_BYTES:
        raise TikTokAPIError(f"Video is {video_size / 1024 / 1024:.0f} MB; TikTok's Content Posting API caps at 4 GB.")
    if video_size <= CHUNK_MAX:
        return video_size, 1
    chunk = wanted_chunk or CHUNK_MAX
    chunk = max(CHUNK_MIN, min(CHUNK_MAX, chunk))
    total = max(1, video_size // chunk)
    if total > MAX_CHUNKS:
        chunk = max(CHUNK_MIN, math.ceil(video_size / MAX_CHUNKS))
        total = max(1, video_size // chunk)
    return chunk, total


def init_payload(caption: str, privacy_level: str, post_mode: str, video_size: int,
                 chunk_size: int, total_chunks: int) -> Dict[str, Any]:
    """The body sent to the init endpoint — split out so a dry run can show it."""
    post_info: Dict[str, Any] = {"title": caption[:2200], "post_mode": post_mode}
    # privacy_level is only meaningful for a post that actually goes out; a
    # draft/inbox item is invisible by definition and TikTok rejects the pair.
    if post_mode == "DIRECT_PUBLISH":
        post_info["privacy_level"] = privacy_level
    return {
        "post_info": post_info,
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunks,
        },
    }


def upload_video(
    video_path: str,
    caption: str,
    privacy_level: str = "SELF_ONLY",
    post_mode: Optional[str] = None,
    *,
    progress: Optional[Callable[[int, int], None]] = None,
    poll_interval: float = 5.0,
    poll_timeout: float = 600.0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Publish (or draft) one file. Raises ``TikTokAPIError`` on any refusal.

    ``dry_run`` computes and returns everything that *would* be sent without a
    single network call — the way to check a caption or chunk plan before
    spending a real post.
    """
    from config import settings

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video file not found at {video_path}")
    mode = (post_mode or settings.TIKTOK_POST_MODE or "UPLOAD").upper()
    if mode not in POST_MODES:
        raise TikTokAPIError(f"Unknown post_mode '{mode}'. Expected one of {', '.join(POST_MODES)}.")
    if mode != "UPLOAD" and not has_direct_post_rights():
        raise TikTokAPIError(
            f"post_mode {mode} goes through the direct-post endpoint, which needs the "
            f"'{PUBLISH_SCOPE}' scope the app has not been granted. Use "
            f"TIKTOK_POST_MODE=UPLOAD, or apply for Direct Post and set "
            f"TIKTOK_SCOPES=user.info.basic,{PUBLISH_SCOPE}.")
    if privacy_level not in PRIVACY_LEVELS:
        raise TikTokAPIError(f"Unknown privacy_level '{privacy_level}'. Expected one of {', '.join(PRIVACY_LEVELS)}.")

    size = os.path.getsize(video_path)
    chunk_size, total_chunks = plan_chunks(size)
    init_url = INBOX_INIT_URL if mode == "UPLOAD" else DIRECT_POST_INIT_URL
    payload = init_payload(caption, privacy_level, mode, size, chunk_size, total_chunks)

    if dry_run:
        return {"dry_run": True, "init_url": init_url, "init_payload": payload,
                "video_path": video_path, "bytes": size, "chunk_size": chunk_size,
                "total_chunk_count": total_chunks}

    token = access_token()
    started = time.time()
    logger.info(f"TikTok init: {os.path.basename(video_path)} {size / 1024 / 1024:.1f} MB "
                f"in {total_chunks} chunk(s), mode={mode}")
    init = _post_json(init_url, token, payload)
    data = init.get("data") or {}
    publish_id = data.get("publish_id")
    upload_url = data.get("upload_url")
    if not publish_id or not upload_url:
        raise TikTokAPIError(f"TikTok init returned no publish_id/upload_url: {data}")

    sent = _upload_chunks(video_path, upload_url, token, chunk_size, total_chunks, size, progress)
    logger.info(f"TikTok accepted {sent} bytes for publish_id {publish_id}; polling status")
    status_payload = poll_publish_status(publish_id, token, interval=poll_interval,
                                         timeout=poll_timeout, started=started)

    video_id = _extract_video_id(status_payload)
    username = (load_token() or {}).get("username")
    result = {
        "dry_run": False,
        "publish_id": publish_id,
        "video_id": video_id,
        "status": status_payload.get("status"),
        "fail_reason": status_payload.get("fail_reason"),
        "post_mode": mode,
        "privacy_level": privacy_level if mode == "DIRECT_PUBLISH" else None,
        "username": username,
        "bytes": sent,
        "chunks": total_chunks,
    }
    # A draft or an inbox item has no public link yet. Naming a URL for one, as
    # the old stub did, is how a fake publish looks real in the database.
    result["tiktok_url"] = (f"https://www.tiktok.com/@{username}/video/{video_id}"
                            if video_id and username and mode == "DIRECT_PUBLISH" else None)
    if video_id:
        remember_published(video_id)
    return result


def _upload_chunks(video_path: str, upload_url: str, token: str, chunk_size: int,
                   total_chunks: int, size: int,
                   progress: Optional[Callable[[int, int], None]] = None) -> int:
    """PUT the file in ranges; the last chunk carries the remainder."""
    with open(video_path, "rb") as fh:
        sent = 0
        for part in range(total_chunks):
            first = part * chunk_size
            # Only the final part may exceed chunk_size — it holds the remainder.
            last = size - 1 if part == total_chunks - 1 else min(first + chunk_size, size) - 1
            fh.seek(first)
            blob = fh.read(last - first + 1)
            if not blob:
                break
            try:
                resp = requests.put(
                    upload_url, data=blob, timeout=(10, 300),
                    headers={"Content-Range": f"bytes {first}-{last}/{size}",
                             "Content-Type": "video/mp4",
                             "Content-Length": str(len(blob)),
                             "video_size": str(size)},
                )
            except requests.RequestException as exc:
                raise TikTokAPIError(f"TikTok chunk {part + 1}/{total_chunks} failed to send: {exc}")
            if resp.status_code >= 400:
                raise TikTokAPIError(f"TikTok rejected chunk {part + 1}/{total_chunks} (HTTP {resp.status_code}): "
                                     f"{resp.text[:200]}")
            sent = last + 1
            if progress:
                progress(sent, size)
        return sent


def poll_publish_status(publish_id: str, token: str, interval: float = 5.0,
                        timeout: float = 600.0, started: Optional[float] = None) -> Dict[str, Any]:
    """Poll the status endpoint until TikTok reports a terminal state."""
    origin = started if started is not None else time.time()
    last: Dict[str, Any] = {}
    while True:
        payload = _post_json(STATUS_URL, token, {"publish_id": publish_id})
        last = payload.get("data") or {}
        status = last.get("status")
        if status in TERMINAL_STATUSES:
            return last
        if time.time() - origin > timeout:
            raise TikTokAPIError(f"TikTok still reports '{status}' after {timeout:.0f} s "
                                 f"(publish_id {publish_id}).")
        logger.info(f"TikTok publish {publish_id}: status={status}, waiting {interval:.0f} s")
        time.sleep(interval)


def _extract_video_id(status_data: Dict[str, Any]) -> Optional[str]:
    """The post id, from TikTok's own (misspelled) field name."""
    raw = status_data.get("publicaly_available_post_id") or status_data.get("publicly_available_post_id")
    if isinstance(raw, list):
        return str(raw[0]) if raw else None
    return str(raw) if raw else None


def stored_ids() -> List[str]:
    """Video ids this machine has actually published, for the reconcile script."""
    path = _secrets_dir() / "tiktok_published.json"
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return []


def remember_published(video_id: str) -> None:
    """Append a published id to a local ledger (the DB row is the primary record;
    this exists so a reconcile can tell 'published by us' from 'fabricated')."""
    if not video_id:
        return
    path = _secrets_dir() / "tiktok_published.json"
    ids = stored_ids()
    if video_id not in ids:
        ids.append(str(video_id))
        path.write_text(json.dumps(ids, indent=2), encoding="utf-8")
