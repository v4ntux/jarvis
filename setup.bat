@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  py -3.14 -m venv .venv
  if errorlevel 1 (
    echo Установите Python 3.14 для Windows с python.org и повторите setup.bat.
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Не удалось установить зависимости. Проверьте сообщения выше и интернет.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m core.prepare_voice
if errorlevel 1 (
  echo Не удалось загрузить голосовые модели. Чат доступен через run.bat.
  echo Для повторной загрузки используйте prepare-voice.bat.
  pause
  exit /b 1
)
echo Готово. Запустите run.bat.
pause
