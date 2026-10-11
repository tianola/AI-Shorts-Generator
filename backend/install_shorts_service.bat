@echo off
setlocal
set "LOG=E:\01-AI-PROJECTS\ai-shorts-generator\backend\shorts-service-install.log"
echo === %date% %time% === > "%LOG%"
net session >nul 2>&1 && echo ELEVATED=yes >> "%LOG%" || (echo ELEVATED=no >> "%LOG%" & echo YOU MUST RUN AS ADMIN >> "%LOG%" & type "%LOG%" & exit /b 2)

set "NSSM=C:\Users\thana\AppData\Local\hermes\bin\nssm-2.24\win64\nssm.exe"
set "SERVICE=ShortsBackend"
set "APP_DIR=E:\01-AI-PROJECTS\ai-shorts-generator\backend"
set "PYEXE=%APP_DIR%\.venv\Scripts\python.exe"

if not exist "%NSSM%" (echo MISSING NSSM >> "%LOG%" & exit /b 1)
if not exist "%PYEXE%" (echo MISSING PYTHON >> "%LOG%" & exit /b 1)

echo === Stop/remove old service === >> "%LOG%"
sc stop %SERVICE% >nul 2>&1
"%NSSM%" stop %SERVICE% >nul 2>&1
"%NSSM%" remove %SERVICE% confirm >nul 2>&1

echo === Install service === >> "%LOG%"
"%NSSM%" install %SERVICE% "%PYEXE%" "run_service.py" >> "%LOG%" 2>&1
if errorlevel 1 (echo INSTALL FAILED >> "%LOG%" & goto :done)

"%NSSM%" set %SERVICE% AppDirectory "%APP_DIR%" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppStdout "%APP_DIR%\service.out.log" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppStderr "%APP_DIR%\service.err.log" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRotateFiles 1 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRotateBytes 10485760 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% Start SERVICE_AUTO_START >> "%LOG%" 2>&1
rem KEY: any exit (including our idle-triggered exit) restarts the service
"%NSSM%" set %SERVICE% AppExit Default Restart >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% AppRestartDelay 3000 >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% DisplayName "AI Shorts Generator Backend" >> "%LOG%" 2>&1
"%NSSM%" set %SERVICE% Description "FastAPI :5001 (dev mode). Idle-restarts after 10 min no activity to reload code." >> "%LOG%" 2>&1

echo === Start service === >> "%LOG%"
sc start %SERVICE% >> "%LOG%" 2>&1
timeout /t 4 /nobreak >nul
sc query %SERVICE% | findstr /i STATE >> "%LOG%" 2>&1
netstat -ano | findstr ":5001" | findstr /i LISTENING >> "%LOG%" 2>&1

:done
sc qc %SERVICE% | findstr /i "START_TYPE" >> "%LOG%" 2>&1
echo === DONE === >> "%LOG%"
type "%LOG%"
exit /b 0