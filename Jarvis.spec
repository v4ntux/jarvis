# Сборка Джарвиса: pyinstaller Jarvis.spec
#
# Получается папка dist/Jarvis. Пользовательские данные и DPAPI-секреты живут в
# LOCALAPPDATA/Jarvis и никогда не копируются в дистрибутив.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

# Модель тишины/речи: без неё распознавание падает при первом вызове.
whisper_assets = collect_data_files("faster_whisper")
openwake_assets = collect_data_files("openwakeword")

hidden = [
    "comtypes", "comtypes.stream", "pycaw", "pycaw.pycaw",
    "win32com.client", "win32clipboard", "win32api",
    "faster_whisper", "ctranslate2", "onnxruntime", "tokenizers",
    "edge_tts", "av", "sounddevice", "httpx", "win32crypt",
    "openwakeword", "openwakeword.model", "openwakeword.utils", "openwakeword.vad",
    "core.actions", "core.router", "core.skills", "core.memory",
    "core.ears", "core.voice", "core.session", "core.ai", "core.autostart",
    "core.credentials", "core.scheduler", "ui.island", "ui.theme",
]

a = Analysis(
    ["jarvis.py"],
    pathex=[os.path.abspath(".")],
    binaries=collect_dynamic_libs("sounddevice") + collect_dynamic_libs("ctranslate2"),
    datas=whisper_assets + openwake_assets + [("jarvis.ico", ".")],
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "pytest", "IPython", "notebook",
              "pystray", "anthropic",
              "pydantic", "tkinter.test", "test", "unittest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Jarvis",
          debug=False, strip=False, upx=False, console=False, icon="jarvis.ico",
          version="version_info.txt")

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Jarvis")
