@echo off
REM Start meeting-listener in console mode (no UI). Usage: start_headless.bat [seconds]
cd /d "%~dp0"
set "PY=C:\Users\132854\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" run.py --no-ui --duration %1
pause
