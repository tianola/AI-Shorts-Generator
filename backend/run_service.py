"""
Development-mode service wrapper for the AI Shorts Generator backend.

Runs the FastAPI app (main:app) and watches for idleness: if no HTTP request
arrives for SHORTS_IDLE_RESTART_SECONDS (default 600 = 10 min), the process
exits cleanly. NSSM (the Windows service manager) sees the exit and restarts
the service after AppRestartDelay ms — which reloads the latest dev code.

Usage:
    python run_service.py
Env:
    SHORTS_IDLE_RESTART_SECONDS   idle limit before self-restart (default 600)
    SHORTS_ACTIVITY_FILE          where to touch the last-activity timestamp
    SHORTS_API_PORT               port override (default 5001)
"""
import os
import sys
import threading
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

# ---------------------------------------------------------------------------
# Idle tracking middleware (pure ASGI, no starlette deprecation warnings)
# ---------------------------------------------------------------------------
class TouchActivityMiddleware:
    def __init__(self, app, activity_file: Path):
        self.app = app
        self.activity_file = activity_file

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                self.activity_file.write_text(f"{time.time()}\n", encoding="utf-8")
            except Exception:
                pass  # never let activity tracking break requests
        await self.app(scope, receive, send)


def main() -> int:
    from main import app  # the real FastAPI app (imports config, routes, etc.)

    idle_limit = int(os.environ.get("SHORTS_IDLE_RESTART_SECONDS", "600"))
    port = int(os.environ.get("SHORTS_API_PORT", "5001"))
    storage = Path(os.environ.get("SHORTS_STORAGE_ROOT", BACKEND_DIR.parent / "storage"))
    storage.mkdir(parents=True, exist_ok=True)
    activity_file = Path(os.environ.get("SHORTS_ACTIVITY_FILE", str(storage / "last_activity.txt")))

    # Seed the activity file so we never restart immediately on boot
    try:
        activity_file.write_text(f"{time.time()}\n", encoding="utf-8")
    except Exception:
        pass

    app.add_middleware(TouchActivityMiddleware, activity_file=activity_file)

    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", access_log=True)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to actually bind
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    print(f"[service] backend listening on :{port} | idle-restart after {idle_limit}s", flush=True)

    try:
        while not server.should_exit:
            time.sleep(20)
            try:
                ts = float(activity_file.read_text(encoding="utf-8").strip())
            except Exception:
                ts = time.time()  # unknown -> treat as active
            idle = time.time() - ts
            print(f"[service] idle={idle:.0f}s / limit {idle_limit}s", flush=True)
            if idle > idle_limit:
                print("[service] IDLE LIMIT REACHED — exiting so NSSM restarts with fresh code", flush=True)
                server.should_exit = True
                break
    except KeyboardInterrupt:
        pass

    server.should_exit = True
    thread.join(timeout=30)
    print("[service] exited cleanly", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())