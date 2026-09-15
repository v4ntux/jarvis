@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "%~dp0jarvis.py"
  exit /b 0
)
if exist "dist\Jarvis\Jarvis.exe" (
  start "" "dist\Jarvis\Jarvis.exe"
  exit /b 0
)
echo Не найден Python или собранный Jarvis.
echo Запустите setup.bat, затем run.bat.
pause
exit /b 1
