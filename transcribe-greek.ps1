# ============================================================
# Whisper transcription - Greek (auto-detect multilingual)
# Audio: C:\Users\thana\Videos\Timeline_22_audio.wav
# Model: medium (stored on E:\01-AI-PROJECTS\whisper-models)
# NOTE: NO --language flag -> Whisper auto-detects the language
#       (previous run forced --language th and produced garbage)
# ============================================================

$ErrorActionPreference = "Stop"

$audio   = "C:\Users\thana\Videos\Timeline_22_audio.wav"
$outDir  = "C:\Users\thana\Videos"
$modelDir = "E:\01-AI-PROJECTS\whisper-models"

if (-not (Test-Path $audio)) {
    Write-Error "Audio file not found: $audio"
    exit 1
}

Write-Host "Transcribing: $audio"
Write-Host "Model: medium | Language: AUTO-DETECT | Output: $outDir"
Write-Host ""

whisper $audio `
    --model medium `
    --output_format all `
    --output_dir $outDir `
    --model_dir $modelDir `
    --verbose True

Write-Host ""
Write-Host "Done. Check the detected language in the output above (should be 'el' / Greek)."