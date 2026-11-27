@echo off
rem Keep the API alive on an always-on Windows host.
rem Does nothing when the port is already served, so a second launch from the
rem desktop shortcut cannot create a duplicate collector.
setlocal
for %%i in ("%~dp0..\..") do set "ROOT=%%~fi"
cd /d "%ROOT%"
if not exist logs mkdir logs
if not defined PORT set "PORT=8812"
set "MONITOR_DB=%ROOT%\data\monitor.db"
set "MONITOR_CLOUD=1"

if not defined MONITOR_ACCESS_PASSWORD (
  echo [%date% %time%] MONITOR_ACCESS_PASSWORD not set, abort >> "logs\server.log"
  exit /b 2
)

netstat -ano | findstr /C:":%PORT%" | findstr /C:"LISTENING" >nul
if not errorlevel 1 (
  echo [%date% %time%] port %PORT% already listening, skip >> "logs\server.log"
  exit /b 0
)

:loop
echo [%date% %time%] starting server >> "logs\server.log"
".venv\Scripts\python.exe" "deploy\windows\backup_once.py" >> "logs\server.log" 2>&1
".venv\Scripts\python.exe" "deploy\windows\run_server.py" >> "logs\server.log" 2>&1
echo [%date% %time%] server exited, restart in 10s >> "logs\server.log"
timeout /t 10 /nobreak > nul
goto loop
