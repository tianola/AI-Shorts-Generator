@echo off
setlocal
cd /d "E:\01-AI-PROJECTS\ai-shorts-generator"
if not exist "storage\logs" mkdir "storage\logs"

set "PYW=C:\Users\thana\AppData\Local\Programs\Python\Python311\pythonw.exe"
if not exist "%PYW%" set "PYW=pythonw.exe"

:: Stop existing instances first to prevent duplicate processes
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name like '%%python%%'\" | Where-Object { $_.CommandLine -match 'main.py' -or $_.CommandLine -match 'worker.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1

:: Start API Server silently (0 windows, 0 cmd tabs)
wscript.exe "E:\01-AI-PROJECTS\ai-shorts-generator\scripts\run_silent.vbs" "%PYW%" "E:\01-AI-PROJECTS\ai-shorts-generator\backend" "main.py"

:: Start Worker silently (0 windows, 0 cmd tabs)
wscript.exe "E:\01-AI-PROJECTS\ai-shorts-generator\scripts\run_silent.vbs" "%PYW%" "E:\01-AI-PROJECTS\ai-shorts-generator\backend" "worker.py"

exit /b 0
