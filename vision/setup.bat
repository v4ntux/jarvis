@echo off
rem Local vision for Jarvis: OmniParser V2 (YOLO + Florence-2) and EasyOCR on the GPU.
rem Everything goes to this drive: ~3.2 GB download, ~6 GB on disk. Run once from the repo root.
chcp 65001 >nul
cd /d "%~dp0.."
set TMP=%CD%\.vision-tmp
set TEMP=%CD%\.vision-tmp
set PIP_NO_CACHE_DIR=1
if not exist .vision-tmp mkdir .vision-tmp
if not exist .vision-venv py -3.14 -m venv .vision-venv || exit /b 1
set PY=.vision-venv\Scripts\python.exe
%PY% -m pip install --upgrade pip || exit /b 1
rem CUDA builds first: the plain PyPI torchvision is CPU-only and breaks YOLO on the GPU
%PY% -m pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu130 || exit /b 1
%PY% -m pip install -r vision\requirements.txt || exit /b 1
set M=vision\models
set HF=https://huggingface.co
for %%F in (icon_detect/model.pt icon_detect/model.yaml icon_caption/config.json icon_caption/generation_config.json icon_caption/model.safetensors) do (
  curl -sS -L --create-dirs -C - --retry 5 -o "%M%/%%F" "%HF%/microsoft/OmniParser-v2.0/resolve/main/%%F" || exit /b 1
)
for %%F in (config.json preprocessor_config.json processor_config.json tokenizer_config.json tokenizer.json special_tokens_map.json vocab.json merges.txt added_tokens.json) do (
  curl -sS -L --create-dirs -o "%M%/florence_native/%%F" "%HF%/florence-community/Florence-2-base-ft/resolve/main/%%F"
)
set HF_HUB_OFFLINE=1
%PY% vision\convert_caption.py || exit /b 1
echo Готово. Проверка: %PY% vision\parse_server.py --check screenshot.png
