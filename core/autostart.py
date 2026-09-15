"""Автозапуск Джарвиса вместе с Windows.

Кладёт ярлык в папку «Автозагрузка» текущего пользователя. Это самый честный
способ: ярлык видно глазами и его можно удалить руками, не лазая в реестр.
Папка открывается по Win+R -> shell:startup
"""
import os
import sys
from pathlib import Path

SHORTCUT_NAME = "Jarvis.lnk"


def startup_dir():
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"


def shortcut_path():
    return startup_dir() / SHORTCUT_NAME


def _target():
    """Persist the running version: frozen executable or current source checkout."""
    if getattr(sys, "frozen", False):           # запущены из собранного exe
        return sys.executable, "", str(Path(sys.executable).parent)

    root = Path(__file__).resolve().parent.parent
    pythonw = root / ".venv/Scripts/pythonw.exe"
    if pythonw.exists():
        return str(pythonw), '"%s"' % (root / "jarvis.py"), str(root)
    current_pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(current_pythonw) if current_pythonw.exists() else sys.executable
    return interpreter, '"%s"' % (root / "jarvis.py"), str(root)


def is_enabled():
    return shortcut_path().exists()


def enable():
    """Создаёт ярлык в автозагрузке. Возвращает путь к ярлыку."""
    import win32com.client

    target, arguments, workdir = _target()
    startup_dir().mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    link = shell.CreateShortCut(str(shortcut_path()))
    link.TargetPath = target
    link.Arguments = arguments
    link.WorkingDirectory = workdir
    link.Description = "Джарвис — голосовой ассистент"
    link.save()
    return shortcut_path()


def disable():
    """Убирает ярлык из автозагрузки."""
    path = shortcut_path()
    if path.exists():
        path.unlink()
        return True
    return False


def status_line():
    if is_enabled():
        target, arguments, _ = _target()
        return "Автозапуск включён: %s\n  запускает: %s %s" % (shortcut_path(), target, arguments)
    return "Автозапуск выключен. Включить:  jarvis.py --autostart on"
