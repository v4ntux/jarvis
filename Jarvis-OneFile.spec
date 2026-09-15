# Single-file release: pyinstaller --clean --noconfirm Jarvis-OneFile.spec
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

whisper_assets = collect_data_files("faster_whisper")
openwake_assets = collect_data_files("openwakeword")

hidden = [
    "comtypes", "comtypes.stream", "pycaw", "pycaw.pycaw",
    "win32com.client", "win32clipboard", "win32api", "win32crypt",
    "faster_whisper", "ctranslate2", "onnxruntime", "tokenizers",
    "edge_tts", "av", "sounddevice", "httpx",
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
              "pystray", "anthropic", "pydantic", "tkinter.test", "test", "unittest"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="Jarvis-2.0",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="jarvis.ico",
    version="version_info.txt",
)
