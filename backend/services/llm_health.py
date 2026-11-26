"""What the LLM backends are doing right now, as the app itself saw it.

Every LLM call the router makes is recorded here: when it started, when it ended,
whether it returned usable JSON, and the error if it did not. From that the health
endpoint works out one status per model, so the dashboard can say "ok", "degraded",
"down", "unresponsive" or "idle" and show the last error that caused it.

Recording is in memory and per process. A restart clears the history, which is the
right behaviour for a health signal: the answer is about the running server.

The live probe is separate. It calls the provider's model-list route, which spends no
tokens, and reports whether the server answers at all and how fast.
"""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

# A call still running after this long is reported as "unresponsive". The router's own
# read timeout is 300 s, so a call past 120 s is slow enough to be worth a warning.
UNRESPONSIVE_AFTER_SECONDS = 120.0
# No successful call for this long, after a failure, means the model is "down".
DOWN_AFTER_SECONDS = 600.0
# A model with no call at all in this window is "idle", not an error.
IDLE_AFTER_SECONDS = 1800.0
EVENT_HISTORY = 200

_lock = threading.Lock()
_next_id = 0
_inflight: Dict[int, tuple] = {}
_events: Deque[dict] = deque(maxlen=EVENT_HISTORY)
_targets: Dict[str, dict] = {}


@dataclass
class Call:
	id: int
	target: str
	started: float = field(default_factory=time.time)


def begin(target: str) -> Call:
	"""Mark a call as started. Every begin() must be followed by exactly one end()."""
	global _next_id
	with _lock:
		_next_id += 1
		call = Call(id=_next_id, target=target)
		_inflight[call.id] = (call.target, call.started)
	return call


def end(call: Call, kind: str, detail: str = "") -> None:
	"""Record how a call finished. kind is 'ok', 'no_json', 'timeout' or 'error'."""
	now = time.time()
	latency_ms = int((now - call.started) * 1000)
	with _lock:
		_inflight.pop(call.id, None)
		_events.append({
			"at": now,
			"target": call.target,
			"kind": kind,
			"latency_ms": latency_ms,
			"detail": (detail or "")[:300],
		})
		info = _targets.setdefault(call.target, {"last_ok": None, "last_error": None, "ok": 0, "failed": 0})
		if kind == "ok":
			info["last_ok"] = now
			info["ok"] += 1
		else:
			info["last_error"] = {"at": now, "kind": kind, "detail": (detail or "")[:300]}
			info["failed"] += 1


def classify(exc: BaseException) -> str:
	"""'timeout' for read/connect timeouts, 'error' for everything else."""
	name = type(exc).__name__.lower()
	return "timeout" if "timeout" in name else "error"


def _status_for(target: str, info: Optional[dict], inflight_started: List[float], now: float) -> str:
	if inflight_started and now - min(inflight_started) > UNRESPONSIVE_AFTER_SECONDS:
		return "unresponsive"
	if info is None:
		return "idle"
	last_ok = info.get("last_ok")
	last_error = info.get("last_error")
	last_any = max([t for t in (last_ok, last_error and last_error["at"]) if t] or [0])
	if last_any and now - last_any > IDLE_AFTER_SECONDS:
		return "idle"
	if last_error and (not last_ok or last_error["at"] > last_ok):
		# The most recent outcome is a failure.
		if not last_ok or now - last_ok > DOWN_AFTER_SECONDS:
			return "down"
		return "degraded"
	return "ok"


_RANK = {"unresponsive": 0, "down": 1, "degraded": 2, "idle": 3, "ok": 4}


def snapshot(recent: int = 25) -> dict:
	"""The current state of every model the app has called, and the latest events."""
	now = time.time()
	with _lock:
		inflight_by_target: Dict[str, List[float]] = {}
		for target, started in _inflight.values():
			inflight_by_target.setdefault(target, []).append(started)
		targets = []
		for target in sorted(set(_targets) | set(inflight_by_target)):
			info = _targets.get(target)
			status = _status_for(target, info, inflight_by_target.get(target, []), now)
			targets.append({
				"target": target,
				"status": status,
				"in_flight": len(inflight_by_target.get(target, [])),
				"ok_count": (info or {}).get("ok", 0),
				"failed_count": (info or {}).get("failed", 0),
				"last_ok_at": (info or {}).get("last_ok"),
				"last_error": (info or {}).get("last_error"),
			})
		events = list(_events)[-recent:]
	active = [item["status"] for item in targets if item["status"] != "idle"]
	overall = min(active, key=_RANK.__getitem__) if active else "idle"
	return {"overall": overall, "targets": targets, "recent_events": list(reversed(events))}


def reset() -> None:
	"""Forget everything. Used by tests."""
	global _next_id
	with _lock:
		_next_id = 0
		_inflight.clear()
		_events.clear()
		_targets.clear()
