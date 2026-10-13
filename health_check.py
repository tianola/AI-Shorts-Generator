import subprocess
import sys
import json
import requests
import time
import os

# The API listens on settings.API_PORT (5001); a Docker stack publishes it on
# SHORTS_API_HOST_PORT (5002 by default), so let the caller point this script
# at whichever one is live.
API_BASE = os.environ.get("SHORTS_API_BASE_URL", "http://localhost:5001").rstrip("/")

def check_backend():
    try:
        resp = requests.get(f'{API_BASE}/api/system/presets', timeout=5)
        return resp.status_code == 200, resp.text if resp.status_code == 200 else f"Status {resp.status_code}"
    except Exception as e:
        return False, str(e)

WORKER_QUERY = (
    "Get-CimInstance Win32_Process -Filter \"Name LIKE '%python%'\" | "
    "ForEach-Object { if ($_.CommandLine) { '{0}|{1}' -f $_.ProcessId, $_.CommandLine } }"
)

def check_worker():
    # wmic was removed from this Windows build (10.0.26300), so the old CSV query
    # returned nothing and this check failed even with a live worker. CIM through
    # PowerShell is the supported path.
    try:
        proc = subprocess.run(
            ['powershell', '-NoProfile', '-Command', WORKER_QUERY],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as e:
        return False, f"Could not query processes: {e}"
    for line in proc.stdout.splitlines():
        pid, _, cmdline = line.partition('|')
        if 'worker.py' in cmdline.lower():
            return True, f"PID {pid.strip()}: {cmdline.strip()[:160]}"
    return False, "No worker.py process found"

# POST /api/jobs takes scalar parameters, which FastAPI reads from the query
# string - a JSON body is rejected with 422 before the handler runs.
CLAIM_TIMEOUT_SECONDS = 60

def submit_test_job(live=False):
    """Prove the job API is mounted, validating, and reachable by the worker.

    Default mode never enqueues work: an empty youtube_url must be refused by
    the endpoint's own check, which exercises routing + validation without
    downloading a source video and rendering ~500 MB into storage/jobs/.
    Pass --live to submit a real job, using SHORTS_HEALTH_URL as the source.
    """
    try:
        if not live:
            resp = requests.post(f'{API_BASE}/api/jobs', params={'youtube_url': ''}, timeout=10)
            if resp.status_code == 400:
                return True, "POST /api/jobs mounted and validating (run with --live to enqueue a real job)"
            return False, f"Expected 400 for an empty youtube_url, got {resp.status_code}: {resp.text[:200]}"

        source_url = os.environ.get('SHORTS_HEALTH_URL', '').strip()
        if not source_url:
            return False, "--live needs SHORTS_HEALTH_URL set to a real YouTube URL to process"

        resp = requests.post(
            f'{API_BASE}/api/jobs',
            params={'youtube_url': source_url, 'target_clips': 1, 'auto_upload': False},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return False, f"Job submission failed: {resp.status_code} {resp.text[:200]}"
        # The response model names the field job_id, not id.
        job_id = resp.json().get('job_id')
        if not job_id:
            return False, f"No job_id in response: {resp.text[:200]}"

        # A real render runs for minutes; the useful signal is that the worker
        # claims the row at all, which proves worker + DB + queue wiring.
        for _ in range(CLAIM_TIMEOUT_SECONDS):
            time.sleep(1)
            status_resp = requests.get(f'{API_BASE}/api/jobs/{job_id}', timeout=5)
            if status_resp.status_code != 200:
                return False, f"Failed to fetch job {job_id} status: {status_resp.status_code}"
            status = status_resp.json().get('status')
            if status == 'processing':
                return True, f"Worker claimed job {job_id}; it will run to completion on its own"
            if status in ('completed', 'failed'):
                return True, f"Job {job_id} already reached {status}"
        return False, f"Job {job_id} stayed 'created' for {CLAIM_TIMEOUT_SECONDS}s - the worker is not picking up work"
    except Exception as e:
        return False, str(e)

LOG_TAIL_BYTES = 2 * 1024 * 1024
# utils/logger writes the rotating sink to <storage>/logs/app.log; the old
# backend/logs/ path never existed here, so this check always failed.
LOG_CANDIDATES = (
    os.path.join('storage', 'logs', 'app.log'),
    os.path.join('backend', 'logs', 'app.log'),
)

def check_logs():
    log_path = next((p for p in LOG_CANDIDATES if os.path.exists(p)), None)
    if log_path is None:
        return False, f"No app.log found at any of: {', '.join(LOG_CANDIDATES)}"
    try:
        size = os.path.getsize(log_path)
        with open(log_path, 'rb') as f:
            # Only the tail is a health signal. Reading the whole file costs a
            # multi-second stall once the log grows, and an ERROR from last
            # month says nothing about whether the stack works now.
            if size > LOG_TAIL_BYTES:
                f.seek(size - LOG_TAIL_BYTES)
            content = f.read().decode('utf-8', errors='ignore')
        import re
        # Non-capturing group: with a capturing one findall returns only "ERROR",
        # so the reported "first" match was the keyword instead of the line.
        errors = re.findall(r'(?i)(?:ERROR|Exception|Traceback).*', content)
        if errors:
            return False, f"Found {len(errors)} error patterns in the last " \
                          f"{min(size, LOG_TAIL_BYTES) // 1024} KB (first: {errors[0][:100]})"
        return True, "No error patterns found"
    except Exception as e:
        return False, f"Could not read log: {e}"

if __name__ == '__main__':
    # Change to the project directory
    os.chdir(r'E:\01-AI-PROJECTS\ai-shorts-generator')

    # --live downloads and renders a real source; off by default on purpose.
    LIVE_TEST_JOB = '--live' in sys.argv

    results = {}
    
    # 1. Backend
    ok, msg = check_backend()
    results['backend_api'] = (ok, msg)
    print(f"Backend API: {'PASS' if ok else 'FAIL'} - {msg}")
    
    # 2. Worker
    ok, msg = check_worker()
    results['worker'] = (ok, msg)
    print(f"Worker: {'PASS' if ok else 'FAIL'} - {msg}")
    
    # 3. Test job (only if backend is ok)
    if results['backend_api'][0]:
        ok, msg = submit_test_job(live=LIVE_TEST_JOB)
        results['test_job'] = (ok, msg)
        print(f"{'Live' if LIVE_TEST_JOB else 'Contract'} test job: {'PASS' if ok else 'FAIL'} - {msg}")
    else:
        results['test_job'] = (False, "Backend not responsive, skipping test job")
        print("Test job: SKIPPED - Backend not responsive")
    
    # 4. Logs
    ok, msg = check_logs()
    results['logs'] = (ok, msg)
    print(f"Logs: {'PASS' if ok else 'FAIL'} - {msg}")
    
    # Overall
    all_ok = all(v[0] for v in results.values())
    print(f"\nOverall health check: {'PASS' if all_ok else 'FAIL'}")
    
    # Save results as JSON for the kanban task
    with open('health_check_results.json', 'w', encoding='utf-8') as f:
        json.dump({k: {'ok': ok, 'msg': msg} for k, (ok, msg) in results.items()}, f,
                  indent=2, ensure_ascii=False)
    print("\nResults saved to health_check_results.json")
    
    # Exit with code 0 if all passed, 1 otherwise
    sys.exit(0 if all_ok else 1)