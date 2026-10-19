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
"%NSSM%" set %SERVICE% AppEnvironmentExtra "PYTHONPATH=%APP_DIR%" >> "%LOG%" 2>&1

echo === Start worker service === >> "%LOG%"
sc start %SERVICE% >> "%LOG%" 2>&1
timeout /t 4 /nobreak >nul
sc query %SERVICE% | findstr /i STATE >> "%LOG%" 2>&1

:done
sc qc %SERVICE% | findstr /i "START_TYPE SERVICE_START_NAME" >> "%LOG%" 2>&1
echo === DONE === >> "%LOG%"
type "%LOG%"
exit /b 0
