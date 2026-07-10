@echo off
cd /d "%~dp0"
title TukkerScout 3.0
if not exist ".venv\Scripts\python.exe" (
  echo Start eerst installeren.bat
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "%~dp0run.py"
pause
