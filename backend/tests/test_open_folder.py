"""
Regression tests for the 'Open Folder' endpoint (t_a2bf3c12).

The endpoint must:
- open Windows Explorer at the real job directory on native Windows,
- refuse loudly (503) when running inside a Docker container (where the
  container's view of the path cannot open Windows Explorer),
- fall back gracefully on Linux/macOS (xdg-open / open), and
- return 404 when the job directory does not exist.
"""
import asyncio
from types import SimpleNamespace


from api import jobs
from api.jobs import open_job_folder


class FakeResult:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _call_route(job_id: int):
    try:
        return await open_job_folder(job_id), None
    except Exception as exc:  # HTTPException propagates from await
        return None, exc


def _run(job_id: int):
    return asyncio.run(_call_route(job_id))


def _prepare(tmp_path, monkeypatch, job_id=42):
    """Point jobs.settings.JOBS_DIR at tmp_path and create the job dir."""
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir(exist_ok=True)
    job_dir = jobs_dir / str(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jobs, "settings", SimpleNamespace(JOBS_DIR=str(jobs_dir)))
    return job_dir


def test_open_folder_windows_uses_explorer(tmp_path, monkeypatch):
    job_dir = _prepare(tmp_path, monkeypatch)
    calls = {}

    monkeypatch.setattr(jobs, "IS_WINDOWS", True)

    def fake_run(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return FakeResult()

    monkeypatch.setattr(jobs.subprocess, "run", fake_run)

    result, exc = _run(42)
    assert exc is None
    assert result["status"] == "ok"
    assert result["opened_with"] == "explorer.exe"
    assert result["path"] == str(job_dir.resolve())

    assert calls["args"][0] == "explorer.exe"
    # Windows path must point AT the job dir and use native separators
    assert calls["args"][1] == str(job_dir.resolve())


def test_open_folder_missing_dir_returns_404(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)
    # Do NOT create the job dir -> 404
    result, exc = _run(9999)
    assert result is None and exc is not None


def test_open_folder_docker_container_returns_503(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)

    real_exists = jobs.os.path.exists

    def fake_exists(p):
        if p == "/.dockerenv":
            return True
        return real_exists(p)

    monkeypatch.setattr(jobs.os.path, "exists", fake_exists)

    result, exc = _run(42)
    assert result is None and exc is not None
    # HTTP 503 Service Unavailable from FastAPI's HTTPException
    assert exc.status_code == 503
    assert "Docker" in str(exc.detail)


def test_open_folder_linux_xdg_open(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)

    run_calls = []
    popen_calls = []

    def fake_run(args, **kwargs):
        run_calls.append(args)
        # `which xdg-open` succeeds -> xdg-open available
        return FakeResult(returncode=0)

    monkeypatch.setattr(jobs, "IS_WINDOWS", False)
    monkeypatch.setattr(jobs, "IS_MACOS", False)
    monkeypatch.setattr(jobs.subprocess, "run", fake_run)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda args: popen_calls.append(args) or FakeResult())

    result, exc = _run(42)
    assert exc is None
    assert result["opened_with"] == "xdg-open"
    assert run_calls[0] == ["which", "xdg-open"]
    assert popen_calls[0] == ["xdg-open", str((tmp_path / "jobs" / "42").resolve())]


def test_open_folder_linux_no_xdg_open_returns_path(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)

    monkeypatch.setattr(jobs, "IS_WINDOWS", False)
    monkeypatch.setattr(jobs, "IS_MACOS", False)
    real_exists = jobs.os.path.exists

    def fake_exists(p):
        # jobs.os.path IS the global os.path module, so the replacement must
        # delegate to the captured real function; calling Path.exists() here
        # re-entered this lambda and blew the stack.
        if p == "/.dockerenv":
            return False
        return real_exists(p)

    monkeypatch.setattr(jobs.os.path, "exists", fake_exists)
    monkeypatch.setattr(jobs.subprocess, "run", lambda args, **k: FakeResult(returncode=1))

    result, exc = _run(42)
    assert exc is None
    assert result["status"] == "ok"
    assert result["opened_with"] is None
    assert result["path"] == str((tmp_path / "jobs" / "42").resolve())


def test_open_folder_macos_uses_open(tmp_path, monkeypatch):
    _prepare(tmp_path, monkeypatch)

    popen_calls = []
    monkeypatch.setattr(jobs, "IS_WINDOWS", False)
    monkeypatch.setattr(jobs, "IS_MACOS", True)
    monkeypatch.setattr(jobs.subprocess, "Popen", lambda args: popen_calls.append(args))

    result, exc = _run(42)
    assert exc is None
    assert result["opened_with"] == "open"
    assert popen_calls[0] == ["open", str((tmp_path / "jobs" / "42").resolve())]