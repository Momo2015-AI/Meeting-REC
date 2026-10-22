@echo off
REM Start meeting-listener with the floating subtitle window.
cd /d "%~dp0"
set "PY=C:\Users\132854\AppData\Local\Programs\Python\Python314\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" run.py
if errorlevel 1 pause
