"""Watch list for Live Studio: save channel links, and clip each one while it is live.

A background thread checks every enabled entry every POLL_SECONDS with yt-dlp (no download). When
an entry is live and no session is running for it, a LiveSession starts for that entry. Each entry
has its own folder for the rolling buffer and its clips, so several streams run at once without
sharing files. Clips stay on disk after the stream ends and show up again the next time.
"""

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

from loguru import logger

from config import settings
from services import live_studio

POLL_SECONDS = 120
_lock = threading.RLock()
_sessions: Dict[str, live_studio.LiveSession] = {}     # entry id -> the session recording it now
_checks: Dict[str, dict] = {}                          # entry id -> {"checked": ts, "live": bool, "error": str}
_thread: Optional[threading.Thread] = None


def _root() -> Path:
    return Path(settings.STORAGE_ROOT) / "live_studio"


def _store() -> Path:
    return _root() / "watchlist.json"


def _watch_dir(entry_id: str) -> Path:
    return _root() / "watch" / entry_id


def load() -> List[dict]:
    try:
        return json.loads(_store().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(entries: List[dict]) -> None:
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalise(url: str) -> tuple:
    """(kind, url) for a link a person pastes: a channel page becomes its live page."""
    raw = (url or "").strip()
    if raw and not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    host = urlparse(raw).netloc.lower().removeprefix("www.").removeprefix("m.")
    if host.endswith("youtube.com") or host == "youtu.be":
        if "/watch" not in raw and not raw.rstrip("/").endswith("/live"):
            raw = raw.rstrip("/") + "/live"
        return "youtube", raw
    if host.endswith("twitch.tv"):
        return "twitch", raw.rstrip("/")
    if host.endswith("tiktok.com"):
        return "tiktok", raw if raw.rstrip("/").endswith("/live") else raw.rstrip("/") + "/live"
    return "", raw


def add(url: str, name: str = "") -> dict:
    kind, clean = normalise(url)
    error = live_studio.validate_source(kind, clean) if kind else "Paste a YouTube, Twitch or TikTok link"
    if error:
        raise ValueError(error)
    with _lock:
        entries = load()
        if any(e["url"] == clean for e in entries):
            raise ValueError("That link is already on the watch list")
        entry = {"id": uuid.uuid4().hex[:8], "name": name.strip(), "url": clean, "kind": kind,
                 "enabled": True, "auto_clip": True, "added": time.time()}
        entries.append(entry)
        _save(entries)
    return entry


def update(entry_id: str, enabled: Optional[bool] = None, name: Optional[str] = None) -> Optional[dict]:
    with _lock:
        entries = load()
        for entry in entries:
            if entry["id"] == entry_id:
                if enabled is not None:
                    entry["enabled"] = bool(enabled)
                if name is not None:
                    entry["name"] = name.strip()
                _save(entries)
                if enabled is False:
                    _stop(entry_id)
                return entry
    return None


def _stop(entry_id: str) -> None:
    session = _sessions.pop(entry_id, None)
    if session:
        session.stop()


def remove(entry_id: str) -> bool:
    with _lock:
        entries = load()
        kept = [e for e in entries if e["id"] != entry_id]
        if len(kept) == len(entries):
            return False
        _stop(entry_id)
        _save(kept)
        _checks.pop(entry_id, None)
    return True


def clips_for(entry_id: str) -> List[dict]:
    out = []
    for meta in (_watch_dir(entry_id) / "clips").glob("*/meta.json"):
        try:
            item = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        item["watch_id"] = entry_id
        out.append(item)
    return sorted(out, key=lambda c: c.get("created_at", 0), reverse=True)


def delete_clip(entry_id: str, clip_id: str) -> bool:
    if not clip_id.isalnum():
        return False
    folder = _watch_dir(entry_id) / "clips" / clip_id
    if folder.is_dir():
        shutil.rmtree(folder, ignore_errors=True)
        return True
    return False


def _state(entry: dict) -> dict:
    session = _sessions.get(entry["id"])
    check = _checks.get(entry["id"], {})
    if not entry.get("enabled"):
        state, detail = "paused", "Paused"
    elif session and session.status in ("connecting", "live"):
        state, detail = "live", session.detail
    elif check.get("error"):
        state, detail = "error", check["error"]
    elif "checked" not in check:
        state, detail = "checking", "Waiting for the first check"
    else:
        state, detail = "offline", "Not live"
    return {**entry, "state": state, "detail": detail, "checked": check.get("checked"),
            "clips": clips_for(entry["id"])}


def view() -> List[dict]:
    with _lock:
        return [_state(e) for e in load()]


def _check(url: str) -> dict:
    """{'live': bool, 'title': str, 'channel': str} from yt-dlp, without downloading."""
    import yt_dlp

    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False) or {}
    live = info.get("is_live") is True or info.get("live_status") == "is_live"
    return {"live": live, "title": info.get("title") or "", "channel": info.get("channel") or info.get("uploader") or ""}


def _tick() -> None:
    for entry in load():
        entry_id = entry["id"]
        with _lock:
            session = _sessions.get(entry_id)
            if session and session.status in ("connecting", "live"):
                continue
            if session:                                   # the stream ended; forget the old session
                _sessions.pop(entry_id, None)
        if not entry.get("enabled"):
            continue
        try:
            info = _check(entry["url"])
        except Exception as exc:
            message = str(exc)[:200]
            # yt-dlp reports "not live right now" as an error; that is an ordinary offline state.
            if re.search(r"not currently live|will begin|is offline|no video formats|not live", message, re.I):
                with _lock:
                    _checks[entry_id] = {"checked": time.time(), "live": False, "error": ""}
                continue
            with _lock:
                _checks[entry_id] = {"checked": time.time(), "live": False, "error": message}
            logger.warning(f"Live watch: check failed for {entry['url']} ({type(exc).__name__}: {exc})")
            continue
        with _lock:
            _checks[entry_id] = {"checked": time.time(), "live": info["live"], "error": ""}
            if info["channel"] and not entry.get("name"):
                saved = load()
                for item in saved:
                    if item["id"] == entry_id:
                        item["name"] = info["channel"]
                _save(saved)
        if not info["live"]:
            continue
        logger.info(f"Live watch: {entry['url']} is live, starting a session")
        session = live_studio.LiveSession(_watch_dir(entry_id), entry["kind"], entry["url"],
                                          auto_clip=entry.get("auto_clip", True))
        session.start()
        with _lock:
            _sessions[entry_id] = session


def _loop() -> None:
    while True:
        try:
            _tick()
        except Exception as exc:
            logger.error(f"Live watch loop error ({type(exc).__name__}: {exc})")
        time.sleep(POLL_SECONDS)


def start() -> None:
    """Start the watcher thread once. Idempotent."""
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, daemon=True, name="live-watch")
            _thread.start()
