@echo off
setlocal
rem Installs a SEPARATE NSSM service for the job worker (python worker.py).
rem The ShortsBackend service hosts the FastAPI :5001 API ONLY; this one hosts
rem the process that claims 'created' jobs and renders them. Run from an
rem elevated prompt (it self-checks with `net session`).
set "LOG=E:\01-AI-PROJECTS\ai-shorts-generator\backend\shorts-worker-service-install.log"
echo === %date% %time% === > "%LOG%"
net session >nul 2>&1 && echo ELEVATED=yes >> "%LOG%" || (echo ELEVATED=no >> "%LOG%" & echo YOU MUST RUN AS ADMIN >> "%LOG%" & type "%LOG%" & exit /b 2)

set "NSSM=C:\Users\thana\AppData\Local\hermes\bin\nssm-2.24\win64\nssm.exe"
set "SERVICE=ShortsWorker"
set "APP_DIR=E:\01-AI-PROJECTS\ai-shorts-generator\backend"
set "PYEXE=%APP_DIR%\.venv\Scripts\python.exe"

if not exist "%NSSM%" (echo MISSING NSSM >> "%LOG%" & exit /b 1)
if not exist "%PYEXE%" (echo MISSING PYTHON >> "%LOG%" & exit /b 1)

echo === Stop/remove old worker service === >> "%LOG%"
sc stop %SERVICE% >nul 2>&1
"%NSSM%" stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1
rem A service marked for deletion cannot be recreated until its process is fully
rem gone; installing too fast fails with "The specified service has been marked
rem for deletion". Wait for the name to clear (up to ~20s) before installing.
set /a WAITED=0
:waitdel
sc query %SERVICE% >nul 2>&1 || goto :ready
set /a WAITED+=1
if %WAITED% geq 20 (echo WARN: old service still present after 20s >> "%LOG%" & goto :ready)
timeout /t 1 /nobreak >nul
goto :waitdel
:ready

echo === Install worker service === >> "%LOG%"
"%NSSM%" install %SERVICE% "%PYEXE%" "worker.py" >> "%LOG%" 2>&1
if errorlevel 1 (echo INSTALL FAILED >> "%LOG%" & goto :done)

"%NSSM%" set %SERVICE% AppDirectory "%APP_DIR%" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppStdout "%APP_DIR%\worker-service.out.log" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppStderr "%APP_DIR%\worker-service.err.log" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRotateFiles 1 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRotateBytes 10485760 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START >> "%LOG%" 2>&1
rem worker.py runs forever; NSSM brings it back on ANY exit (crash or reboot).
"%NSSM%" set %SERVICE% AppExit Default Restart >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRestartDelay 5000 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% DisplayName "AI Shorts Generator Worker" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% Description "Polls the jobs DB, renders clips, drains the publish queue. Runs unattended; no idle-restart." >> "%LOG%" 2>&1
rem cwd is already AppDirectory, but pin PYTHONPATH so `import config` resolves even if NSSM cwd handling changes.
rem LocalSystem services do NOT inherit this user's PATH, so ffmpeg/ffprobe (a
rem winget per-user install) must be added to the service's own PATH explicitly —
rem otherwise the worker logs "ffmpeg is not on PATH" and refuses every job.
set "FFMPEG_DIR="
for /f "delims=" %%I in ('where ffmpeg 2^>nul') do if not defined FFMPEG_DIR for %%J in ("%%I") do set "FFMPEG_DIR=%%~dpJ"
if defined FFMPEG_DIR (echo FFMPEG_DIR=%FFMPEG_DIR% >> "%LOG%") else (echo WARNING: ffmpeg not found on installer PATH >> "%LOG%")
rem FFMPEG_DIR ends with a backslash; the ';' makes it its own PATH entry ahead of
rem the system dirs. Without that separator the two join into one bogus entry and
rem shutil.which still cannot find ffmpeg.
"%NSSM%" set %SERVICE% AppEnvironmentExtra "PYTHONPATH=%APP_DIR%" "PATH=%FFMPEG_DIR%;C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0" >> "%LOG%" 2>&1

echo === Start worker service === >> "%LOG%"
sc start %SERVICE% >> "%LOG%" 2>&1
timeout /t 4 /nobreak >nul
sc query %SERVICE% | findstr /i STATE >> "%LOG%" 2>&1

:done
sc qc %SERVICE% | findstr /i "START_TYPE SERVICE_START_NAME" >> "%LOG%" 2>&1
echo === DONE === >> "%LOG%"
type "%LOG%"
exit /b 0
