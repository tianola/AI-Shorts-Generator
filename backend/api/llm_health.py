"""GET /api/llm/health: whether the LLM backends are answering, and why not.

The recorded state comes from services/llm_health.py (every real call). With
?probe=1 the server also asks the provider's model-list route. That spends no tokens,
so the dashboard can check reachability every minute without costing anything.
"""

import time
import httpx
from fastapi import APIRouter, Query
from loguru import logger

from config import settings
from services import llm_health
from services.llm_router import omniroute_chain

router = APIRouter()

PROBE_TIMEOUT_SECONDS = 10.0


async def _probe_omniroute() -> dict:
	base_url = settings.OMNIROUTE_BASE_URL.rstrip("/")
	api_key = settings.OMNIROUTE_API_KEY or ""
	if not api_key:
		return {"provider": "omniroute", "reachable": False, "detail": "OMNIROUTE_API_KEY is not set"}
	started = time.time()
	try:
		async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
			response = await client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
		latency_ms = int((time.time() - started) * 1000)
		if response.status_code != 200:
			return {
				"provider": "omniroute", "reachable": False, "latency_ms": latency_ms,
				"detail": f"HTTP {response.status_code}: {response.text[:200]}",
			}
		models = [m.get("id") for m in response.json().get("data", []) if isinstance(m, dict)]
		wanted = omniroute_chain()
		missing = [m for m in wanted if m not in models] if models else []
		return {
			"provider": "omniroute", "reachable": True, "latency_ms": latency_ms,
			"models_listed": len(models), "chain": wanted, "missing_from_server": missing,
			"detail": "" if not missing else f"not on the server: {', '.join(missing)}",
		}
	except Exception as exc:
		logger.warning(f"LLM health probe (OmniRoute) failed: {type(exc).__name__}: {exc}")
		return {
			"provider": "omniroute", "reachable": False,
			"latency_ms": int((time.time() - started) * 1000),
			"detail": f"{type(exc).__name__}: {exc}" or type(exc).__name__,
		}


async def _probe_ollama() -> dict:
	url = f"{settings.OLLAMA_URL.rstrip('/')}/api/tags"
	started = time.time()
	try:
		async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_SECONDS) as client:
			response = await client.get(url)
		latency_ms = int((time.time() - started) * 1000)
		if response.status_code != 200:
			return {"provider": "ollama", "reachable": False, "latency_ms": latency_ms,
					"detail": f"HTTP {response.status_code}"}
		names = [m.get("name") for m in response.json().get("models", []) if isinstance(m, dict)]
		return {
			"provider": "ollama", "reachable": True, "latency_ms": latency_ms,
			"models_listed": len(names), "detail": "",
		}
	except Exception as exc:
		return {"provider": "ollama", "reachable": False,
				"latency_ms": int((time.time() - started) * 1000),
				"detail": f"{type(exc).__name__}: {exc}" or type(exc).__name__}


@router.get("/llm/health")
async def llm_health_status(probe: bool = Query(False), events: int = Query(25, ge=0, le=200)):
	provider = settings.AI_PROVIDER.lower()
	state = llm_health.snapshot(recent=events)
	payload = {
		"provider": provider,
		"overall": state["overall"],
		"targets": state["targets"],
		"recent_events": state["recent_events"],
		"thresholds": {
			"unresponsive_after_seconds": llm_health.UNRESPONSIVE_AFTER_SECONDS,
			"down_after_seconds": llm_health.DOWN_AFTER_SECONDS,
		},
		"probe": None,
	}
	if probe:
		payload["probe"] = await (_probe_omniroute() if provider == "omniroute" else _probe_ollama())
		reachable = payload["probe"].get("reachable")
		if reachable is False and payload["overall"] in ("ok", "idle", "degraded"):
			payload["overall"] = "down"
	return payload
