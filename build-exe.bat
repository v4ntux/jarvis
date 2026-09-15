@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment not found: .venv
  exit /b 1
)
echo Собираю Jarvis.exe...
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 (
  echo. & echo Тесты не прошли. Сборка остановлена.
  exit /b 1
)
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean Jarvis.spec
if errorlevel 1 (
  echo Build failed. The previous executable was not updated.
  exit /b 1
)
if exist "dist\Jarvis\Jarvis.exe" (
  if exist ".env.example" copy /y ".env.example" "dist\Jarvis\.env.example" >nul
  if exist "README.md" copy /y "README.md" "dist\Jarvis\README.md" >nul
  echo. & echo Готово: dist\Jarvis\Jarvis.exe
) else (
  echo. & echo Сборка не удалась, смотри сообщения выше.
  exit /b 1
)
pause
