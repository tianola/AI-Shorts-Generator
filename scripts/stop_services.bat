@echo off
echo Stopping AI Shorts Generator Services...
powershell.exe -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"name like '%%python%%'\" | Where-Object { $_.CommandLine -match 'main.py' -or $_.CommandLine -match 'worker.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Services stopped!
exit /b 0
