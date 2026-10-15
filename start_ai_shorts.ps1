param(
	[switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = Join-Path $repoRoot 'backend'
$storageLogs = Join-Path $repoRoot 'storage\logs'

if (-not (Test-Path $storageLogs)) {
	New-Item -ItemType Directory -Path $storageLogs -Force | Out-Null
}

# A venv directory can exist and still be unrunnable - backend\.venv has a
# pyvenv.cfg its own launcher refuses to resolve - so probe by executing the
# interpreter instead of testing the path. Start-Process on a dead venv returns
# a PID and dies instantly, which is how this script used to report success
# while nothing was listening.
function Test-VenvPython([string]$Candidate) {
	if (-not (Test-Path $Candidate)) { return $false }
	& $Candidate -c 'import sys' | Out-Null
	return ($LASTEXITCODE -eq 0)
}

$candidates = @(
	(Join-Path $repoRoot '.venv\Scripts\python.exe'),
	(Join-Path $backendDir '.venv\Scripts\python.exe')
)
$venvPython = $null
foreach ($candidate in $candidates) {
	if (Test-Path $candidate) {
		if (Test-VenvPython $candidate) { $venvPython = $candidate; break }
		Write-Warning "Unusable virtual environment at $candidate - trying the next one."
	}
}

if (-not $venvPython) {
	Write-Host 'Creating virtual environment...' -ForegroundColor Cyan
	$venvDir = Join-Path $repoRoot '.venv'
	py -3.11 -m venv $venvDir
	$venvPython = Join-Path $venvDir 'Scripts\python.exe'
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
$env:API_PORT = '5001'
$env:API_HOST = '0.0.0.0'

$apiLog = Join-Path $storageLogs 'api.log'
$workerLog = Join-Path $storageLogs 'worker.log'
$apiErr = Join-Path $storageLogs 'api.err.log'
$workerErr = Join-Path $storageLogs 'worker.err.log'

# A second API on the same port dies on bind while the first keeps answering the
# health probe, so the run below would look like a success and hide the failure.
try {
	$already = Invoke-WebRequest -Uri 'http://localhost:5001/api/health' -UseBasicParsing -TimeoutSec 3
	if ($already.StatusCode -eq 200) {
		Write-Host 'Already running: something answers 200 on http://localhost:5001/api/health.' -ForegroundColor Yellow
		Write-Host 'Leave it alone, or stop it before starting a new one.' -ForegroundColor Yellow
		exit 0
	}
} catch { }

$apiProcess = Start-Process -FilePath $venvPython -ArgumentList 'main.py' -WorkingDirectory $backendDir -RedirectStandardOutput $apiLog -RedirectStandardError $apiErr -PassThru
$workerProcess = Start-Process -FilePath $venvPython -ArgumentList 'worker.py' -WorkingDirectory $backendDir -RedirectStandardOutput $workerLog -RedirectStandardError $workerErr -PassThru

# uvicorn imports the whole app (ffmpeg/ffprobe probe included) before it binds
# the port, so an early probe is expected to fail - keep asking.
$healthy = $false
foreach ($attempt in 1..10) {
	Start-Sleep -Seconds 3
	if ($apiProcess.HasExited -or $workerProcess.HasExited) { break }
	try {
		$response = Invoke-WebRequest -Uri 'http://localhost:5001/api/health' -UseBasicParsing -TimeoutSec 5
		if ($response.StatusCode -eq 200) { $healthy = $true; break }
	} catch { }
}

if (-not $healthy) {
	Write-Host ''
	Write-Host 'AI Shorts Generator did NOT come up.' -ForegroundColor Red
	if ($apiProcess.HasExited) { Write-Host "  API exited with code $($apiProcess.ExitCode)." -ForegroundColor Red }
	if ($workerProcess.HasExited) { Write-Host "  Worker exited with code $($workerProcess.ExitCode)." -ForegroundColor Red }
	Write-Host "  Check $apiErr and $workerErr" -ForegroundColor Red
	exit 1
}

Write-Host ''
Write-Host 'AI Shorts Generator started.' -ForegroundColor Green
Write-Host 'API: http://localhost:5001' -ForegroundColor Yellow
Write-Host 'Worker PID: ' $workerProcess.Id -ForegroundColor Yellow
Write-Host 'API PID: ' $apiProcess.Id -ForegroundColor Yellow
Write-Host 'Logs:' -ForegroundColor Yellow
Write-Host "  API stdout: $apiLog" 
Write-Host "  API stderr: $apiErr" 
Write-Host "  Worker stdout: $workerLog" 
Write-Host "  Worker stderr: $workerErr" 
Write-Host ''
Write-Host 'Both processes are detached: closing this window does not stop them.' -ForegroundColor DarkGray
Write-Host "Stop them with: Stop-Process -Id $($apiProcess.Id),$($workerProcess.Id) -Force" -ForegroundColor DarkGray
Write-Host 'Tip: create a job with the smoke runner or by calling the API endpoint.' -ForegroundColor DarkGray
