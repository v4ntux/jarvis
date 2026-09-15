"""Действия над компьютером. Всё, что Джарвис умеет делать руками Windows.

Каждое действие — обычная функция, возвращающая короткую фразу для озвучки.
Никакого обращения к сети: это локальная часть, она работает всегда и бесплатно.
"""
import ctypes
import datetime as dt
import difflib
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
import subprocess
import webbrowser
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import psutil

from core import settings as cfg

user32 = ctypes.WinDLL("user32", use_last_error=True)
HOME = Path.home()

class ActionError(RuntimeError):
    """An operation could not establish its requested result."""


# Use real Windows structures: INPUT is 40 bytes on x64 and 28 on x86.
ULONG_PTR = wintypes.WPARAM


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR))


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = (("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD))


class _INPUTUNION(ctypes.Union):
    _fields_ = (("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("hi", _HARDWAREINPUT))


class _INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("u", _INPUTUNION))


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT
user32.GetForegroundWindow.restype = wintypes.HWND
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL
user32.IsIconic.argtypes = (wintypes.HWND,)
user32.IsZoomed.argtypes = (wintypes.HWND,)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = (ctypes.c_int, ctypes.c_int)
user32.SetCursorPos.restype = wintypes.BOOL
user32.WindowFromPoint.argtypes = (wintypes.POINT,)
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
user32.GetAncestor.restype = wintypes.HWND

_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_EXTENDEDKEY = 0x0001
_EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}

VK = {
    "win": 0x5B, "ctrl": 0x11, "alt": 0x12, "shift": 0x10, "tab": 0x09,
    "enter": 0x0D, "esc": 0x1B, "space": 0x20, "delete": 0x2E,
    "backspace": 0x08, "home": 0x24, "end": 0x23, "pageup": 0x21,
    "pagedown": 0x22, "insert": 0x2D,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "media_play": 0xB3, "media_next": 0xB0, "media_prev": 0xB1, "media_stop": 0xB2,
    **{chr(code).lower(): code for code in range(0x41, 0x5B)},
    **{str(n): 0x30 + n for n in range(10)},
    **{"f%d" % n: 0x6F + n for n in range(1, 13)},
}
_KEY_ALIASES = {"control": "ctrl", "windows": "win", "escape": "esc",
                "return": "enter", "del": "delete", "pgup": "pageup", "pgdn": "pagedown",
                "контрол": "ctrl", "контроль": "ctrl", "ктрл": "ctrl",
                "альт": "alt", "шифт": "shift", "виндовс": "win", "вин": "win",
                "энтер": "enter", "ввод": "enter", "таб": "tab", "табуляция": "tab",
                "пробел": "space", "эскейп": "esc", "эск": "esc", "делит": "delete",
                "бэкспейс": "backspace", "влево": "left", "вправо": "right",
                "вверх": "up", "вниз": "down", "цэ": "c", "с": "c", "си": "c",
                "вэ": "v", "ви": "v", "икс": "x", "зет": "z", "а": "a"}
_target = threading.local()


def foreground_window():
    return int(user32.GetForegroundWindow() or 0)


def window_pid(hwnd):
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return int(owner.value)


def _activate(hwnd):
    if not hwnd or not user32.IsWindow(hwnd):
        raise ActionError("Нужное окно уже закрыто. Выберите другое окно.")
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)
    user32.SetForegroundWindow(hwnd)
    for _ in range(10):
        if foreground_window() == int(hwnd):
            return
        time.sleep(0.03)
    raise ActionError("Windows не разрешила переключиться на окно. Выберите его вручную.")


@contextmanager
def target_window(hwnd):
    """Bind one command to the window selected before the assistant gained focus."""
    previous = dict(vars(_target))
    _target.hwnd = int(hwnd or 0)
    _target.pending = bool(hwnd)
    _target.blocked = ""
    _target.bound = True
    _target.cursor = None
    _target.cursor_window = 0
    try:
        yield
    finally:
        vars(_target).clear()
        vars(_target).update(previous)


def _cursor_window(x, y):
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    return int(user32.GetAncestor(hwnd, 2) or hwnd or 0)


def _cursor_state():
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise ActionError("Не удалось определить положение курсора.")
    return [int(point.x), int(point.y)], _cursor_window(point.x, point.y)


def capture_target():
    """Freeze the selected application and pointer before asking for approval."""
    saved = int(getattr(_target, "hwnd", 0) or 0)
    hwnd = saved if getattr(_target, "pending", False) else foreground_window()
    if not hwnd or window_pid(hwnd) == os.getpid():
        hwnd = saved or hwnd
    try:
        cursor, cursor_window = _cursor_state()
    except ActionError:
        cursor, cursor_window = None, 0
    return {"hwnd": hwnd, "blocked": str(getattr(_target, "blocked", "")),
            "cursor": cursor, "cursor_window": cursor_window}


@contextmanager
def restore_target(snapshot):
    """Approval resumes the original target, even when a GUI button gained focus."""
    if snapshot is None:
        yield
        return
    with target_window(snapshot.get("hwnd", 0)):
        _target.blocked = str(snapshot.get("blocked", ""))
        if not snapshot.get("hwnd"):
            _target.blocked = "Не удалось сохранить выбранное окно. Выберите его и повторите команду."
        _target.cursor = snapshot.get("cursor")
        _target.cursor_window = int(snapshot.get("cursor_window", 0) or 0)
        yield


def _ensure_target():
    if getattr(_target, "blocked", ""):
        raise ActionError(_target.blocked)
    if getattr(_target, "pending", False):
        _activate(_target.hwnd)
        _target.pending = False
    hwnd = foreground_window()
    if not hwnd or window_pid(hwnd) == os.getpid():
        raise ActionError("Сначала выберите окно другой программы, куда выполнить команду.")
    return hwnd


def _selected_target(hwnd):
    _target.hwnd = int(hwnd)
    _target.pending = False
    _target.blocked = ""
    _target.cursor = None
    _target.cursor_window = 0


def _matches_launch(hwnd, expected):
    if not expected:
        return False
    try:
        process_name = psutil.Process(window_pid(hwnd)).name().lower().removesuffix(".exe")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process_name = ""
    if expected == "__browser__":
        return process_name in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "browser")
    expected = str(expected).lower().removesuffix(".exe")
    title = _window_title(hwnd).lower()
    executable = ALIASES.get(expected, expected)
    return expected in title or executable == process_name


def _after_launch(previous, expected=""):
    """Wait before dependent input; don't let a slow launch type into an old app."""
    if not getattr(_target, "bound", False):
        return
    old_windows = {int(previous or 0), int(getattr(_target, "hwnd", 0) or 0)}
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        hwnd = foreground_window()
        if hwnd and window_pid(hwnd) != os.getpid():
            if hwnd not in old_windows or _matches_launch(hwnd, expected):
                _selected_target(hwnd)
                return
        time.sleep(0.05)
    _target.pending = False
    _target.blocked = "Программа запускается, но её окно ещё не выбрано. Выберите окно и повторите ввод."


def _key(vk, up=False):
    flags = (_KEYEVENTF_KEYUP if up else 0) | (_KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_KEYS else 0)
    ki = _KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
    return _INPUT(type=1, u=_INPUTUNION(ki=ki))


def _send(events):
    if not events:
        return
    arr = (_INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        # Release any modifiers accepted before a partial failure, without replaying the action.
        releases = [event for event in events if event.type == 1 and event.u.ki.dwFlags & _KEYEVENTF_KEYUP]
        if releases:
            release_arr = (_INPUT * len(releases))(*releases)
            user32.SendInput(len(releases), release_arr, ctypes.sizeof(_INPUT))
        raise ActionError("Windows не приняла ввод. Проверьте активное окно и его права доступа.")


def press(*keys):
    names = [_KEY_ALIASES.get(str(key).lower(), str(key).lower()) for key in keys]
    if not names or len(names) > 5 or any(key not in VK for key in names):
        raise ActionError("Неизвестная клавиша или слишком длинное сочетание.")
    if len(names) != len(set(names)):
        raise ActionError("Клавиша повторяется в сочетании.")
    codes = [VK[key] for key in names]
    _send([_key(code) for code in codes] + [_key(code, True) for code in reversed(codes)])


def hotkey(keys):
    text = re.sub(r"\bплюс\b", "+", str(keys).strip().lower())
    names = [key for key in re.split(r"[+\s]+", text) if key]
    # Validate before moving focus.
    if not names or len(names) > 5 or any(_KEY_ALIASES.get(key.lower(), key.lower()) not in VK for key in names):
        raise ActionError("Не понял сочетание клавиш. Например: ctrl+c или alt+tab.")
    _ensure_target()
    press(*names)
    return "Нажал %s." % "+".join(names)


def type_text(text):
    """Send UTF-16 code units, including surrogate pairs, to the selected window."""
    if not isinstance(text, str) or not text or len(text) > 20000:
        raise ActionError("Нужен текст от 1 до 20000 символов.")
    hwnd = _ensure_target()
    events = []
    encoded = text.replace("\r\n", "\n").encode("utf-16-le")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        if unit in (9, 10, 13):
            vk = VK["tab"] if unit == 9 else VK["enter"]
            events.extend((_key(vk), _key(vk, True)))
            continue
        for up in (False, True):
            ki = _KEYBDINPUT(wVk=0, wScan=unit,
                             dwFlags=_KEYEVENTF_UNICODE | (_KEYEVENTF_KEYUP if up else 0),
                             time=0, dwExtraInfo=0)
            events.append(_INPUT(type=1, u=_INPUTUNION(ki=ki)))
    for offset in range(0, len(events), 200):
        if foreground_window() != hwnd:
            raise ActionError("Ввод остановлен: активное окно изменилось.")
        _send(events[offset:offset + 200])
    return "Напечатал."


def scroll(direction="down", amount=3):
    if direction not in ("up", "down"):
        raise ActionError("Направление прокрутки: up или down.")
    amount = int(amount)
    if not 1 <= amount <= 20:
        raise ActionError("Прокрутка должна быть от 1 до 20 шагов.")
    hwnd = _ensure_target()
    _, cursor_owner = _cursor_state()
    if cursor_owner != hwnd:
        raise ActionError("Наведите курсор на выбранное окно, чтобы прокрутить его.")
    delta = 120 * amount * (1 if direction == "up" else -1)
    event = _INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(mouseData=delta & 0xFFFFFFFF, dwFlags=0x0800)))
    _send([event])
    return "Прокрутил %s." % ("вверх" if direction == "up" else "вниз")


def click_mouse(button="left", count=1):
    if button not in ("left", "right") or int(count) not in (1, 2):
        raise ActionError("Разрешён один или два щелчка левой или правой кнопкой.")
    point = getattr(_target, "cursor", None)
    owner = getattr(_target, "cursor_window", 0)
    if point is None:
        point, owner = _cursor_state()
    hwnd = _ensure_target()
    if owner != hwnd or _cursor_window(*point) != hwnd:
        raise ActionError("Наведите курсор на нужное место в выбранном окне и повторите голосовую команду.")
    if not user32.SetCursorPos(*point):
        raise ActionError("Windows не разрешила восстановить положение курсора.")
    flags = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    events = [_INPUT(type=0, u=_INPUTUNION(mi=_MOUSEINPUT(dwFlags=flag)))
              for _ in range(int(count)) for flag in flags]
    _send(events)
    return "Щёлкнул в текущем положении курсора."


# --- звук -------------------------------------------------------------------
@contextmanager
def _volume():
    import pythoncom
    from pycaw.pycaw import AudioUtilities

    initialized = False
    try:
        pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        initialized = True
    except pythoncom.com_error as exc:
        if exc.hresult != -2147417850:  # RPC_E_CHANGED_MODE: current thread already has COM.
            raise
    endpoint = None
    try:
        endpoint = AudioUtilities.GetSpeakers().EndpointVolume
        yield endpoint
    finally:
        endpoint = None
        if initialized:
            pythoncom.CoUninitialize()


def get_volume():
    with _volume() as volume:
        return "Громкость %d процентов, звук %s." % (
            round(volume.GetMasterVolumeLevelScalar() * 100),
            "выключен" if volume.GetMute() else "включён")


def set_volume(percent):
    percent = max(0, min(100, int(percent)))
    with _volume() as volume:
        volume.SetMute(0, None)
        volume.SetMasterVolumeLevelScalar(percent / 100.0, None)
        actual = round(volume.GetMasterVolumeLevelScalar() * 100)
        if abs(actual - percent) > 1:
            raise ActionError("Не удалось установить громкость: устройство не подтвердило изменение.")
    return "Громкость %d процентов." % actual


def change_volume(delta):
    with _volume() as volume:
        current = round(volume.GetMasterVolumeLevelScalar() * 100)
    return set_volume(current + int(delta))


def set_mute(muted):
    with _volume() as volume:
        volume.SetMute(1 if muted else 0, None)
        if bool(volume.GetMute()) != bool(muted):
            raise ActionError("Устройство не подтвердило изменение звука.")
    return "Звук выключен." if muted else "Звук включён."


def media(action):
    key = {"play_pause": "media_play", "next": "media_next",
           "previous": "media_prev", "stop": "media_stop"}.get(action)
    if not key:
        raise ActionError("Не понял, что сделать с плеером.")
    press(key)
    return {"play_pause": "Готово.", "next": "Следующий трек.",
            "previous": "Предыдущий трек.", "stop": "Остановил."}[action]


def _powershell(script):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def get_brightness():
    out = _powershell("$ErrorActionPreference='Stop'; "
                      "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness")
    values = (out.stdout or "").strip().splitlines()
    return int(values[0]) if out.returncode == 0 and values and values[0].isdigit() else None


def brightness_status():
    value = get_brightness()
    if value is None:
        raise ActionError("Этот экран не поддерживает управление яркостью через Windows.")
    return "Яркость %d процентов." % value


def set_brightness(percent):
    percent = max(0, min(100, int(percent)))
    out = _powershell(
        "$ErrorActionPreference='Stop'; $monitors = @(Get-CimInstance -Namespace root/WMI "
        "-ClassName WmiMonitorBrightnessMethods); if (!$monitors.Count) { exit 2 }; "
        "foreach ($monitor in $monitors) { $result = Invoke-CimMethod -InputObject $monitor "
        "-MethodName WmiSetBrightness -Arguments @{Timeout=[uint32]1; Brightness=[byte]%d}; "
        "if ($result.ReturnValue -ne 0) { exit 3 } }" % percent)
    if out.returncode != 0:
        raise ActionError("Этот экран не поддерживает управление яркостью через Windows.")
    actual = get_brightness()
    if actual is not None and abs(actual - percent) > 1:
        raise ActionError("Экран не подтвердил изменение яркости.")
    return "Яркость %d процентов." % percent


def change_brightness(delta):
    current = get_brightness()
    if current is None:
        raise ActionError("Этот экран не поддерживает управление яркостью через Windows.")
    return set_brightness(current + int(delta))


# --- время и состояние ------------------------------------------------------
DAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря")


def get_time():
    now = dt.datetime.now()
    return "Сейчас %d:%02d." % (now.hour, now.minute)


def get_date():
    now = dt.datetime.now()
    return "Сегодня %d %s, %s." % (now.day, MONTHS[now.month - 1], DAYS[now.weekday()])


def system_status():
    mem = psutil.virtual_memory()
    parts = ["Процессор загружен на %d процентов" % round(psutil.cpu_percent(interval=0.3)),
             "память на %d" % round(mem.percent)]
    battery = psutil.sensors_battery()
    if battery:
        parts.append("батарея %d процентов" % round(battery.percent))
    return ", ".join(parts) + "."


def disk_status():
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        out.append("%s свободно %d гигабайт" % (part.device.strip(os.sep + ":"),
                                                round(usage.free / 2 ** 30)))
    return ", ".join(out) + "." if out else "Не смог прочитать диски."


# --- окна и программы -------------------------------------------------------
ALIASES = {
    "блокнот": "notepad", "калькулятор": "calc", "проводник": "explorer",
    "диспетчер задач": "taskmgr", "диспетчер": "taskmgr", "панель управления": "control",
    "настройки": "ms-settings:", "параметры": "ms-settings:", "командная строка": "cmd",
    "терминал": "wt", "браузер": "chrome", "хром": "chrome", "гугл хром": "chrome",
    "эдж": "msedge", "ворд": "winword", "эксель": "excel", "пейнт": "mspaint",
    "рисование": "mspaint", "телеграм": "telegram", "дискорд": "discord",
    "стим": "steam", "спотифай": "spotify", "музыка": "spotify", "почта": "outlook",
    "курсор": "cursor", "брейв": "brave", "код": "code", "вс код": "code",
    "edge": "msedge", "эдж браузер": "msedge", "chrome": "chrome",
    "vs code": "code", "visual studio code": "code", "телеграмм": "telegram",
}

PROTECTED = {
    "system", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "sihost.exe",
    "ctfmon.exe", "registry", "memory compression", "audiodg.exe", "conhost.exe",
    "explorer.exe", "jarvis.exe", "python.exe", "pythonw.exe",
}

_START_MENUS = [
    Path(os.environ.get("ProgramData", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
]

_shortcuts_cache = None


def shortcuts():
    """Ярлыки меню Пуск: {имя в нижнем регистре: путь}. Считается один раз."""
    global _shortcuts_cache
    if _shortcuts_cache is None:
        found = {}
        for root in _START_MENUS:
            if root.exists():
                for path in root.rglob("*"):
                    if path.suffix.lower() in (".lnk", ".url"):
                        found.setdefault(path.stem.lower(), path)
        _shortcuts_cache = found
    return _shortcuts_cache


def app_names():
    """Названия программ — используются как подсказка распознавателю речи."""
    return sorted(shortcuts().keys())


_APP_DISPLAY_NAMES = {
    "msedge": ("microsoft edge",), "winword": ("word",), "excel": ("excel",),
    "code": ("visual studio code",), "notepad": ("блокнот", "notepad"),
    "calc": ("калькулятор", "calculator"), "mspaint": ("paint",),
    "telegram": ("telegram desktop", "telegram"),
}


def _registered_app(target):
    """Read executable registrations, without evaluating command strings."""
    import winreg

    executable = target if target.lower().endswith(".exe") else target + ".exe"
    key_name = "Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\" + executable
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                path = Path(os.path.expandvars(str(value).strip().strip('"')))
                if path.is_file():
                    return path
            except OSError:
                continue
    return None


def resolve_app(name):
    """Resolve an installed executable or shortcut, with no launch side effect."""
    raw = str(name or "").strip().lower()
    if not raw:
        return None
    target = ALIASES.get(raw, raw)
    if target == "ms-settings:":
        return target
    index = shortcuts()
    candidates = (target, raw) + _APP_DISPLAY_NAMES.get(target, ())
    for candidate in candidates:
        if candidate in index:
            return index[candidate]
    located = shutil.which(target)
    if located:
        return Path(located)
    registered = _registered_app(target)
    if registered:
        return registered
    matches = [(key, path) for key, path in index.items()
               if any(candidate in key for candidate in candidates if len(candidate) >= 3)]
    if matches:
        return min(matches, key=lambda item: len(item[0]))[1]
    close = difflib.get_close_matches(target, list(index), n=2, cutoff=0.85)
    if len(close) == 1:
        return index[close[0]]
    return None


def open_app(name):
    raw = str(name or "").strip().lower()
    if not raw:
        raise ActionError("Укажите название программы.")
    previous = foreground_window()
    if raw == "браузер":
        if not webbrowser.open("about:blank"):
            raise ActionError("Не удалось открыть браузер по умолчанию.")
        _after_launch(previous, "__browser__")
        return "Открываю браузер по умолчанию."
    target = resolve_app(raw)
    if target is None:
        raise ActionError("Не нашёл программу «%s». Проверьте, установлена ли она." % name)
    os.startfile(str(target))
    label = target.stem if isinstance(target, Path) else str(name)
    _after_launch(previous, label)
    return "Запускаю %s." % label


def close_app(name):
    raw = str(name or "").strip().lower()
    query = ALIASES.get(raw, raw).removesuffix(".exe")
    if not query or query + ".exe" in PROTECTED or query in PROTECTED:
        raise ActionError("Это системная программа, не закрываю её.")
    targets = []
    for proc in psutil.process_iter(["name"]):
        try:
            process_name = (proc.info["name"] or "").lower()
            if process_name not in PROTECTED and process_name.removesuffix(".exe") == query:
                targets.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not targets:
        raise ActionError("Программа «%s» не запущена." % name)
    sent = 0
    for proc in targets:
        for hwnd in _windows_of_pid(proc.pid):
            if user32.PostMessageW(hwnd, 0x0010, 0, 0):
                sent += 1
    if not sent:
        raise ActionError("У программы нет доступного окна для обычного закрытия.")
    _, alive = psutil.wait_procs(targets, timeout=1.0)
    if alive:
        return "Отправил запрос на закрытие. Если есть несохранённые данные, подтвердите в окне программы."
    return "Закрыл."


def _window_title(hwnd):
    length = user32.GetWindowTextLengthW(hwnd)
    if not length:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value.strip()


def _enum_windows():
    result = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.strip():
                result.append((hwnd, buf.value))
        return True

    user32.EnumWindows(proto(callback), 0)
    return result


def _windows_of_pid(pid):
    out = []
    for hwnd, _title in _enum_windows():
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid:
            out.append(hwnd)
    return out


def list_windows(limit=5):
    titles = [title for _hwnd, title in _enum_windows()][:limit]
    return "Открыто: " + "; ".join(titles) + "." if titles else "Открытых окон нет."


def focus_window(title):
    query = str(title or "").lower().strip()
    if not query:
        raise ActionError("Укажите название окна.")
    matches = [(hwnd, name) for hwnd, name in _enum_windows() if query in name.lower()]
    if not matches:
        raise ActionError("Окно «%s» не найдено." % title)
    hwnd, name = min(matches, key=lambda item: (item[1].lower() != query, len(item[1])))
    _activate(hwnd)
    _selected_target(hwnd)
    return "Переключился на %s." % name


def minimize_window():
    hwnd = _ensure_target()
    user32.ShowWindow(hwnd, 6)
    if not user32.IsIconic(hwnd):
        raise ActionError("Не удалось свернуть окно.")
    return "Свернул окно."


def maximize_window():
    hwnd = _ensure_target()
    user32.ShowWindow(hwnd, 3)
    if not user32.IsZoomed(hwnd):
        raise ActionError("Не удалось развернуть окно.")
    return "Развернул окно."


def show_desktop():
    press("win", "d")
    return "Переключил показ рабочего стола."


def lock_screen():
    if not user32.LockWorkStation():
        raise ActionError("Windows не смогла заблокировать экран.")
    return "Блокирую экран."


def sleep_pc():
    if not ctypes.WinDLL("powrprof").SetSuspendState(0, 0, 0):
        raise ActionError("Windows не смогла перевести компьютер в спящий режим.")
    return "Компьютер возвращается из спящего режима."


def power(action, delay=30):
    if action not in ("cancel", "shutdown", "restart"):
        raise ActionError("Неизвестное действие питания.")
    delay = max(10, min(3600, int(delay)))
    args = ["shutdown", "/a"] if action == "cancel" else [
        "shutdown", "/s" if action == "shutdown" else "/r", "/t", str(delay)]
    result = subprocess.run(args, capture_output=True, timeout=5,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise ActionError("Windows не приняла команду питания." if action != "cancel" else
                          "Нет запланированного выключения или Windows не разрешила отмену.")
    if action == "cancel":
        return "Выключение отменено."
    word = "Выключаю" if action == "shutdown" else "Перезагружаю"
    return "%s через %d секунд. Скажите «отмени выключение», если передумали." % (word, delay)


# --- интернет ---------------------------------------------------------------
def open_url(url):
    raw = str(url or "").strip()
    target = raw if "://" in raw else "https://" + raw
    parsed = urlsplit(target)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or any(char.isspace() for char in target)):
        raise ActionError("Нужен адрес сайта http или https без пробелов и пароля.")
    previous = foreground_window()
    if not webbrowser.open(target):
        raise ActionError("Не удалось передать адрес браузеру.")
    _after_launch(previous, "__browser__")
    return "Открываю сайт %s." % parsed.hostname


def web_search(query, engine="google"):
    engines = {
        "google": "https://www.google.com/search?q=",
        "yandex": "https://yandex.ru/search/?text=",
        "youtube": "https://www.youtube.com/results?search_query=",
    }
    if engine not in engines or not str(query or "").strip():
        raise ActionError("Укажите запрос и поисковик google, yandex или youtube.")
    previous = foreground_window()
    if not webbrowser.open(engines[engine] + quote_plus(query)):
        raise ActionError("Не удалось открыть поиск в браузере.")
    _after_launch(previous, "__browser__")
    return "Открываю результаты поиска."


# --- файлы ------------------------------------------------------------------
USER_DIRS = [HOME / "Desktop", HOME / "OneDrive/Desktop", HOME / "Documents",
             HOME / "Downloads", HOME / "Pictures", HOME / "Videos", HOME / "Music"]
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "AppData", "$RECYCLE.BIN"}


_FOLDERS = {
    "рабочий стол": ("Desktop", "Desktop"), "desktop": ("Desktop", "Desktop"),
    "загрузки": ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
    "downloads": ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
    "документы": ("Documents", "Personal"), "documents": ("Documents", "Personal"),
    "изображения": ("Pictures", "My Pictures"), "картинки": ("Pictures", "My Pictures"),
    "pictures": ("Pictures", "My Pictures"), "видео": ("Videos", "My Video"),
    "videos": ("Videos", "My Video"), "музыка": ("Music", "My Music"),
    "music": ("Music", "My Music"), "домашняя папка": ("", ""),
}


def open_folder(name):
    query = str(name or "").strip().lower()
    if query not in _FOLDERS:
        raise ActionError("Известные папки: загрузки, документы, рабочий стол, изображения, видео и музыка.")
    folder, registry_name = _FOLDERS[query]
    path = HOME / folder
    if registry_name:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
                value, _ = winreg.QueryValueEx(key, registry_name)
            path = Path(os.path.expandvars(value))
        except OSError:
            pass
    return open_path(path)


def find_files(query, limit=5):
    q = str(query or "").lower().strip()
    if not q:
        return []
    direct = Path(os.path.expandvars(os.path.expanduser(str(query).strip())))
    try:
        if direct.is_file():
            return [direct]
    except OSError:
        pass
    limit = max(1, min(50, int(limit)))
    hits, visited = [], set()
    deadline = time.monotonic() + 3.0
    roots = list(USER_DIRS)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
            for value_name in ("Desktop", "Personal", "My Pictures", "My Music", "My Video",
                               "{374DE290-123F-4565-9164-39C4925E467B}"):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                    roots.append(Path(os.path.expandvars(value)))
                except OSError:
                    continue
    except OSError:
        pass
    for root in roots:
        if time.monotonic() >= deadline:
            break
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() >= deadline:
                break
            resolved = os.path.normcase(os.path.abspath(dirpath))
            if resolved in visited:
                dirnames[:] = []
                continue
            visited.add(resolved)
            dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS and not name.startswith(".")]
            for name in filenames:
                if q in name.lower():
                    path = Path(dirpath) / name
                    try:
                        hits.append((path.stat().st_mtime, path))
                    except OSError:
                        continue
                    if len(hits) >= limit * 4:
                        break
            if len(hits) >= limit * 4:
                break
        if len(hits) >= limit * 4:
            break
    hits.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in hits[:limit]]


def open_path(path):
    p = Path(os.path.expandvars(os.path.expanduser(str(path))))
    if not p.exists():
        raise ActionError("Файл или папка не найдены.")
    previous = foreground_window()
    os.startfile(str(p))
    _after_launch(previous, p.stem)
    return "Открываю %s." % p.name


def screenshot():
    from PIL import ImageGrab

    cfg.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = cfg.SCREENSHOT_DIR / ("screen_%s.png" % dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f"))
    ImageGrab.grab(all_screens=True).save(path)
    return "Снимок сохранён: %s." % path


def clipboard_get():
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        if not win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
            return ""
        return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT) or ""
    except TypeError:
        return ""
    finally:
        win32clipboard.CloseClipboard()


def clipboard_set(text):
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()
    return "Скопировал."
