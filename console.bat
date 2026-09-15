@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist ".venv\Scripts\python.exe" (
  echo Сначала запустите setup.bat.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" jarvis.py --console
pause
