param(
	[switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $repoRoot 'backend'
$venvPython = Join-Path $backendDir '.venv\Scripts\python.exe'
$storageLogs = Join-Path $repoRoot 'storage\logs'

if (-not (Test-Path $storageLogs)) {
	New-Item -ItemType Directory -Path $storageLogs -Force | Out-Null
}

if (-not (Test-Path $venvPython)) {
	Write-Host 'Creating virtual environment...' -ForegroundColor Cyan
	py -3.11 -m venv (Join-Path $backendDir '.venv')
}

if (-not $SkipInstall) {
	Write-Host 'Installing Python dependencies...' -ForegroundColor Cyan
	& $venvPython -m pip install --upgrade pip
	& $venvPython -m pip install -r (Join-Path $backendDir 'requirements.txt')
}

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
	Write-Warning 'ffmpeg is not on PATH. Install FFmpeg before running the pipeline.'
}

$env:PYTHONPATH = $backendDir
$env:API_PORT = '5000'
$env:API_HOST = '0.0.0.0'

$apiLog = Join-Path $storageLogs 'api.log'
$workerLog = Join-Path $storageLogs 'worker.log'
$apiErr = Join-Path $storageLogs 'api.err.log'
$workerErr = Join-Path $storageLogs 'worker.err.log'

$apiProcess = Start-Process -FilePath $venvPython -ArgumentList 'main.py' -WorkingDirectory $backendDir -RedirectStandardOutput $apiLog -RedirectStandardError $apiErr -PassThru
$workerProcess = Start-Process -FilePath $venvPython -ArgumentList 'worker.py' -WorkingDirectory $backendDir -RedirectStandardOutput $workerLog -RedirectStandardError $workerErr -PassThru

Write-Host ''
Write-Host 'AI Shorts Generator started.' -ForegroundColor Green
Write-Host 'API: http://localhost:5000/docs' -ForegroundColor Yellow
Write-Host 'Worker PID: ' $workerProcess.Id -ForegroundColor Yellow
Write-Host 'API PID: ' $apiProcess.Id -ForegroundColor Yellow
Write-Host 'Logs:' -ForegroundColor Yellow
Write-Host "  API stdout: $apiLog" 
Write-Host "  API stderr: $apiErr" 
Write-Host "  Worker stdout: $workerLog" 
Write-Host "  Worker stderr: $workerErr" 
Write-Host ''
Write-Host 'Use Ctrl+C in the script window if you want to stop manually.' -ForegroundColor DarkGray
Write-Host 'Tip: create a job with the smoke runner or by calling the API endpoint.' -ForegroundColor DarkGray
