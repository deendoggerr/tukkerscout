@echo off
cd /d "%~dp0"
title TukkerScout bijwerken
where git >nul 2>nul
if errorlevel 1 (
  echo Git is nog niet geinstalleerd.
  echo Installeer eerst Git for Windows.
  pause
  exit /b 1
)
echo TukkerScout wordt bijgewerkt vanuit GitHub...
git pull
if errorlevel 1 (
  echo Bijwerken is mislukt.
  pause
  exit /b 1
)
if exist ".venv\Scripts\activate.bat" call ".venv\Scripts\activate.bat"
python -m pip install -r requirements.txt
echo TukkerScout is bijgewerkt.
pause
