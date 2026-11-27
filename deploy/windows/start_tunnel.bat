@echo off
rem Keep the cloudflared quick tunnel alive and refresh public-url.txt.
rem Skips when a tunnel process is already running.
setlocal
for %%i in ("%~dp0..\..") do set "ROOT=%%~fi"
cd /d "%ROOT%"
if not exist logs mkdir logs

tasklist /FI "IMAGENAME eq cloudflared.exe" 2>nul | findstr /I "cloudflared.exe" >nul
if not errorlevel 1 (
  echo [%date% %time%] cloudflared already running, skip >> "logs\tunnel.log"
  exit /b 0
)

:loop
".venv\Scripts\python.exe" "deploy\windows\tunnel_watch.py"
timeout /t 10 /nobreak > nul
goto loop
