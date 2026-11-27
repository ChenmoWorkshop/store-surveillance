@echo off
rem One-shot launcher: API first, tunnel once the port is listening.
setlocal
for %%i in ("%~dp0..\..") do set "ROOT=%%~fi"
cd /d "%ROOT%"
if not exist logs mkdir logs
start "" /b cmd /c "deploy\windows\start_server.bat"
timeout /t 8 /nobreak > nul
start "" /b cmd /c "deploy\windows\start_tunnel.bat"
