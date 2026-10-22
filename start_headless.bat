@echo off
REM Start Meeting-REC in console mode (no UI). Usage: start_headless.bat [seconds]
REM Optional: set the PYTHON var before running to pick a specific interpreter.
cd /d "%~dp0"
if not defined PYTHON set "PYTHON=python"
"%PYTHON%" run.py --no-ui --duration %1 %2 %3
pause
