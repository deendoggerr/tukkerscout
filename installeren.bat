@echo off
cd /d "%~dp0"
title TukkerScout 3.0 installeren
echo TukkerScout 3.0 wordt geinstalleerd...
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv
)
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Installatie gereed.
pause
