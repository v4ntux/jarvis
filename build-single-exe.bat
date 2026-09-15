@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Python environment not found: .venv
    exit /b 1
)

echo Running tests...
".venv\Scripts\python.exe" -m unittest discover -s tests -q
if errorlevel 1 exit /b 1

echo Building standalone Jarvis-2.0.exe...
".venv\Scripts\python.exe" -m PyInstaller --clean --noconfirm Jarvis-OneFile.spec
if errorlevel 1 exit /b 1

if not exist "dist\Jarvis-2.0.exe" exit /b 1
echo Ready: dist\Jarvis-2.0.exe
endlocal
