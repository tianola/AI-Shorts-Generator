# Sets up a LOCAL Ollama vision model for scene attention (no external API).
# Run from an ELEVATED PowerShell (winget installs Ollama per-machine).
#   powershell -ExecutionPolicy Bypass -File setup_local_vlm.ps1
# After this, scene attention uses it when LOCAL_VLM_ENABLED=true in backend/.env.
$ErrorActionPreference = "Stop"
$Model = if ($env:LOCAL_VLM_MODEL) { $env:LOCAL_VLM_MODEL } else { "qwen2.5vl:3b" }

Write-Host "== 1/3 install Ollama (needs admin) =="
if (Get-Command ollama -ErrorAction SilentlyContinue) {
    Write-Host "ollama already on PATH, skipping install"
} else {
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements
    # the installer drops ollama.exe under the per-user app dir; refresh PATH
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + `
               [System.Environment]::GetEnvironmentVariable("Path","User")
}

Write-Host "== 2/3 make sure the server is up (default http://localhost:11434) =="
try {
    Invoke-RestMethod -Uri "http://localhost:11434/api/tags" -TimeoutSec 3 | Out-Null
    Write-Host "ollama server already responding"
} catch {
    Write-Host "starting ollama serve in the background"
    Start-Process -FilePath "ollama" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 6
}

Write-Host "== 3/3 pull the vision model: $Model =="
& ollama pull $Model

Write-Host "== smoke test: ask the model to describe a 1x1 image =="
$png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
$body = @{ model = $Model; prompt = "Reply with JSON {""subject"":""""}. What is this?"; images = @($png); stream = $false } | ConvertTo-Json
$resp = Invoke-RestMethod -Uri "http://localhost:11434/api/generate" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 120
Write-Host "model answered:" $resp.response

Write-Host "DONE. To use it in the pipeline set in backend/.env:"
Write-Host "  LOCAL_VLM_ENABLED=true"
Write-Host "  LOCAL_VLM_MODEL=$Model"
