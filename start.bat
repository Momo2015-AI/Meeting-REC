@echo off
REM Start Meeting-REC with the floating subtitle window.
REM Optional: set the PYTHON var before running to pick a specific interpreter.
cd /d "%~dp0"
if not defined PYTHON set "PYTHON=python"
"%PYTHON%" run.py %*
if errorlevel 1 pause
