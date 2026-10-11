import subprocess
import sys
import json
import requests
import time
import os

def run_cmd(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, stderr=subprocess.DEVNULL).strip()
    except subprocess.CalledProcessError as e:
        return ""
    except Exception:
        return ""

def check_backend():
    try:
        resp = requests.get('http://localhost:8000/api/system/presets', timeout=5)
        return resp.status_code == 200, resp.text if resp.status_code == 200 else f"Status {resp.status_code}"
    except Exception as e:
        return False, str(e)

def check_worker():
    # Get list of python processes with command lines
    output = run_cmd('wmic process where "name like \"%%python%%\"" get ProcessId,CommandLine /format:csv')
    for line in output.split('\\n'):
        if not line.strip() or 'ProcessId' in line:
            continue
        parts = line.split(',')
        if len(parts) >= 3:
            pid = parts[1].strip()
            cmdline = parts[2].strip()
            if 'worker.py' in cmdline.lower():
                return True, f"PID {pid}: {cmdline}"
    return False, "No worker.py process found"

def submit_test_job():
    try:
        presets = requests.get('http://localhost:8000/api/system/presets', timeout=5).json()
        preset_id = None
        if isinstance(presets, list) and len(presets) > 0:
            preset_id = presets[0].get('id') or presets[0].get('name') or str(presets[0])
        elif isinstance(presets, dict) and len(presets) > 0:
            preset_id = list(presets.keys())[0]
        if not preset_id:
            return False, "Could not determine preset ID"
        job_data = {"preset_id": preset_id, "metadata": {"test": True}}
        job_resp = requests.post('http://localhost:8000/api/jobs', json=job_data, timeout=10)
        if job_resp.status_code not in (200, 201):
            return False, f"Job submission failed: {job_resp.status_code} {job_resp.text}"
        job = job_resp.json()
        job_id = job.get('id') or job.get('job_id')
        if not job_id:
            return False, "No job ID in response"
        # Poll for completion (max 10 seconds)
        for _ in range(10):
            time.sleep(1)
            status_resp = requests.get(f'http://localhost:8000/api/jobs/{job_id}', timeout=5)
            if status_resp.status_code != 200:
                return False, f"Failed to fetch job status: {status_resp.status_code}"
            job_status = status_resp.json()
            status = job_status.get('status')
            if status in ('completed', 'finished', 'done', 'success'):
                return True, f"Job completed successfully: {job_status}"
            elif status in ('failed', 'error'):
                return False, f"Job failed: {job_status}"
        return False, "Job did not complete within timeout"
    except Exception as e:
        return False, str(e)

def check_logs():
    log_path = os.path.join(os.getcwd(), 'backend', 'logs', 'app.log')
    if not os.path.exists(log_path):
        return False, "Log file not found at backend/logs/app.log"
    try:
        with open(log_path, 'r', errors='ignore') as f:
            content = f.read()
        # Look for ERROR, Exception, Traceback (case insensitive)
        import re
        errors = re.findall(r'(?i)(ERROR|Exception|Traceback).*', content)
        if errors:
            return False, f"Found {len(errors)} error patterns (first: {errors[0][:100]})"
        else:
            return True, "No error patterns found"
    except Exception as e:
        return False, f"Could not read log: {e}"

if __name__ == '__main__':
    # Change to the project directory
    os.chdir(r'E:\01-AI-PROJECTS\ai-shorts-generator')
    
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
        ok, msg = submit_test_job()
        results['test_job'] = (ok, msg)
        print(f"Test job: {'PASS' if ok else 'FAIL'} - {msg}")
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
    with open('health_check_results.json', 'w') as f:
        json.dump({k: {'ok': ok, 'msg': msg} for k, (ok, msg) in results.items()}, f, indent=2)
    print("\nResults saved to health_check_results.json")
    
    # Exit with code 0 if all passed, 1 otherwise
    sys.exit(0 if all_ok else 1)