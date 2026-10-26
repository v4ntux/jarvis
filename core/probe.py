"""What Jarvis can ask the laptop that is a fact, not a guess: which windows exist and what they are, what the
taskbar offers, whether sound is really playing, a scaled screenshot, and the mouse and keyboard.

Every function returns what really happened, so the server never has to believe «открыл» on faith.
Windows only. Called from hands.py's single worker thread.
"""
import base64
import ctypes
import io
import json
import os
import re
import threading
import time
from ctypes import wintypes

try:
    ctypes.windll.user32.SetProcessDPIAware()  # pixels are real pixels: a click lands where the screenshot shows
except (AttributeError, OSError):
    pass

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi

TERMINALS = {"windowsterminal.exe", "wt.exe", "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe", "bash.exe",
             "mintty.exe", "wsl.exe", "alacritty.exe", "wezterm-gui.exe", "openconsole.exe"}
BROWSERS = {"chrome.exe", "brave.exe", "msedge.exe", "firefox.exe", "opera.exe", "browser.exe", "vivaldi.exe",
            "yandex.exe", "iexplore.exe", "arc.exe"}
SKIP_TITLES = {"program manager", "windows input experience", "microsoft text input application", "settings"}
SCREEN_LONG_EDGE = 1280  # screenshots the model sees: general desktop work is recommended at 1280x720


def kind_of(process: str) -> str:
    name = (process or "").lower()
    return "terminal" if name in TERMINALS else "browser" if name in BROWSERS else "app"


# ---- windows -----------------------------------------------------------------------------------------------------

def _cloaked(hwnd) -> bool:
    flag = ctypes.c_int(0)
    try:
        dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(flag), ctypes.sizeof(flag))
    except OSError:
        return False
    return bool(flag.value)


def _process(pid: int) -> tuple[str, str]:
    import psutil
    try:
        p = psutil.Process(pid)
        return p.name(), p.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return "", ""


def windows() -> list[dict]:
    """Visible top-level windows with a title, newest-focused last: title, process, kind, hwnd."""
    import win32gui
    import win32process

    found = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or _cloaked(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd).strip()
        if not title or title.lower() in SKIP_TITLES:
            return True
        if user32.GetWindowLongW(hwnd, -20) & 0x80:  # WS_EX_TOOLWINDOW: floating helpers, not apps
            return True
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        name, exe = _process(pid)
        found.append({"title": title[:200], "process": name, "exe": exe, "kind": kind_of(name), "hwnd": int(hwnd),
                      "pid": pid})
        return True

    win32gui.EnumWindows(visit, None)
    found.reverse()  # EnumWindows goes front to back: the front window ends up last
    return found


def windows_info() -> str:
    return json.dumps(windows(), ensure_ascii=False)


def close_window(title: str) -> str:
    """Closes windows whose title contains `title` (case does not matter), the polite way (WM_CLOSE)."""
    import win32gui

    wanted = (title or "").strip().lower()
    if not wanted:
        raise ValueError("Не сказано, какое окно закрыть.")
    hits = [w for w in windows() if wanted in w["title"].lower()]
    if not hits:
        return "Ошибка: окна «%s» нет." % title
    for w in hits:
        win32gui.PostMessage(w["hwnd"], 0x0010, 0, 0)
    time.sleep(0.6)
    still = [w for w in windows() if wanted in w["title"].lower()]
    closed = [w["title"] for w in hits if w["hwnd"] not in {s["hwnd"] for s in still}]
    if closed and not still:
        return "Закрыл: %s." % ", ".join(closed)
    if still:
        return "Ошибка: окно «%s» не закрылось (оно может спрашивать, сохранять ли файл)." % still[0]["title"]
    return "Ошибка: ничего не закрылось."


def focus_window(title: str) -> str:
    import win32com.client
    import win32gui

    wanted = (title or "").strip().lower()
    for w in reversed(windows()):
        if wanted in w["title"].lower():
            if win32gui.IsIconic(w["hwnd"]):
                win32gui.ShowWindow(w["hwnd"], 9)
            win32com.client.Dispatch("WScript.Shell").SendKeys("%")  # lets SetForegroundWindow through
            try:
                win32gui.SetForegroundWindow(w["hwnd"])
            except Exception as e:
                return "Ошибка: Windows не дал вывести окно «%s» вперёд: %s" % (w["title"], e)
            return "Переключился на «%s»." % w["title"]
    return "Ошибка: окна «%s» нет." % title


# ---- finding and starting programs -------------------------------------------------------------------------------

TRANSLIT = {"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
            "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
            "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
            "э": "e", "ю": "yu", "я": "ya"}
# How people say it → how the program is called
SPOKEN = {"klaud": "claude", "klod": "claude", "klaude": "claude", "teleg": "telegram", "telegramm": "telegram",
          "hrom": "chrome", "brauzer": "browser", "kalkulyator": "calculator", "bloknot": "notepad",
          "provodnik": "explorer", "yandeksmuzyka": "yandex music"}
_menu_cache: dict = {"at": 0.0, "items": []}


def _latin(text: str) -> str:
    low = (text or "").lower()
    return re.sub(r"[^a-z0-9]+", " ", "".join(TRANSLIT.get(c, c) for c in low)).strip()


def _wanted(name: str) -> str:
    latin = _latin(name)
    return SPOKEN.get(latin.replace(" ", ""), latin)


def _start_menu() -> list[dict]:
    """Programs from the Start menu shortcuts: name, the .lnk and the program it starts."""
    if time.time() - _menu_cache["at"] < 120 and _menu_cache["items"]:
        return _menu_cache["items"]
    import win32com.client

    shell = win32com.client.Dispatch("WScript.Shell")
    items = []
    for base in (os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                 os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")):
        for root, _, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".lnk"):
                    path = os.path.join(root, f)
                    try:
                        target = shell.CreateShortCut(path).Targetpath or ""
                    except Exception:
                        target = ""
                    items.append({"name": f[:-4], "lnk": path, "exe": target})
    _menu_cache.update(at=time.time(), items=items)
    return items


def _start_apps() -> list[dict]:
    """Installed apps (Store apps included) as the Start menu lists them: name and AppID."""
    import subprocess

    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                              "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"],
                             capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(out.stdout.decode("utf-8", errors="replace") or "[]")
    except Exception:
        return []
    data = [data] if isinstance(data, dict) else data
    return [{"name": d.get("Name", ""), "appid": d.get("AppID", "")} for d in data if d.get("Name")]


def _score(wanted: str, name: str) -> int:
    have = _latin(name)
    if not wanted or not have:
        return 0
    if have == wanted:
        return 100
    if have.startswith(wanted) or wanted.startswith(have):
        return 70
    return 40 if wanted in have else 0


def find_apps(name: str) -> list[dict]:
    """Where a program can be started from, best first. Each: name, how (taught|running|startmenu|startapps),
    launch (a path or AppID), exe, kind (app|terminal|browser)."""
    from core import actions

    wanted, found = _wanted(name), []
    taught = {}
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps.json"), encoding="utf-8") as f:
            taught = json.load(f)
    except (OSError, ValueError):
        pass
    for key, path in taught.items():
        if _score(wanted, key):
            found.append({"name": key, "how": "taught", "launch": path, "exe": path, "kind": kind_of(os.path.basename(path)),
                          "score": 200})
    for w in windows():
        if _score(wanted, w["title"]) or _score(wanted, os.path.splitext(w["process"])[0]):
            if w["exe"] and w["kind"] == "app":
                found.append({"name": w["title"], "how": "running", "launch": w["exe"], "exe": w["exe"], "kind": "app",
                              "score": 150})
    for item in _start_menu():
        s = _score(wanted, item["name"])
        if s:
            found.append({"name": item["name"], "how": "startmenu", "launch": item["lnk"], "exe": item["exe"],
                          "kind": kind_of(os.path.basename(item["exe"])), "score": s + 10})
    if not any(f["score"] >= 100 for f in found):
        for item in _start_apps():
            s = _score(wanted, item["name"])
            if s:
                found.append({"name": item["name"], "how": "startapps", "launch": item["appid"], "exe": "", "kind": "app",
                              "score": s})
    seen, unique = set(), []
    for f in sorted(found, key=lambda f: (-f["score"], f["kind"] != "app")):
        if f["launch"].lower() not in seen:
            seen.add(f["launch"].lower())
            unique.append(f)
    return unique


def _store_appid(exe: str) -> str:
    """The AppID of a Store (MSIX) program from the path of its exe, «» for an ordinary program."""
    found = re.search(r"\\WindowsApps\\([^\\_]+)_[^\\_]+_[^\\_]+__([^\\]+)\\", exe or "", re.I)
    if not found:
        return ""
    family = "%s_%s!" % (found.group(1), found.group(2))
    return next((a["appid"] for a in _start_apps() if a["appid"].lower().startswith(family.lower())), "")


def open_smart(name: str) -> str:
    """Starts the program the owner means: a taught path, a running app, a Start menu shortcut or a Store app, and
    only then whatever the name happens to be on PATH (which can be a terminal tool with the same name)."""
    import subprocess

    from core import actions

    candidates = [c for c in find_apps(name) if c["kind"] == "app"]
    if not candidates:
        return actions.open_app(name)
    best = candidates[0]
    appid = best["launch"] if best["how"] == "startapps" else _store_appid(best["exe"])
    if appid:
        # Store apps cannot be started from their folder under WindowsApps, only by their AppID
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + appid],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        os.startfile(best["launch"])
    return "Запускаю «%s» (%s: %s)." % (best["name"], {"taught": "из моих путей", "running": "уже запущено",
                                                      "startmenu": "из меню Пуск", "startapps": "приложение Windows"}[best["how"]],
                                         best["launch"])


# ---- the taskbar -------------------------------------------------------------------------------------------------

_com = threading.local()


def _uia():
    """A UI Automation client for this thread (COM must be set up in every thread that uses it)."""
    import comtypes
    import comtypes.client

    if not getattr(_com, "ready", False):
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass  # already initialised in this thread with another model: usable as it is
        _com.ready = True
    try:
        from comtypes.gen import UIAutomationClient as uia
    except ImportError:
        uia = comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as uia
    client = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)
    return uia, client


def taskbar() -> list[dict]:
    """The buttons on the taskbar: name, whether the program runs, and where the button is."""
    uia, client = _uia()
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        raise RuntimeError("панели задач нет")
    root = client.ElementFromHandle(hwnd)
    found = root.FindAll(uia.TreeScope_Descendants, client.CreateTrueCondition())
    buttons = []
    for i in range(found.Length):
        el = found.GetElement(i)
        try:
            cls, name, ctype = el.CurrentClassName or "", el.CurrentName or "", el.CurrentControlType
            rect = el.CurrentBoundingRectangle
        except Exception:
            continue
        if ctype != uia.UIA_ButtonControlTypeId or not name:
            continue
        if "TaskListButton" not in cls and "Taskbar." not in cls and cls not in ("MSTaskListWClass", ""):
            continue
        if "TaskListButton" not in cls and "Taskbar.TaskListButton" not in cls:
            continue
        if rect.right - rect.left < 4:
            continue
        running = bool(re.search(r"running window|окн|запущен", name, re.I))
        clean = re.sub(r"\s*[-—]\s*\d+\s+(?:running windows?|окн\w*).*$", "", name, flags=re.I).strip()
        clean = re.sub(r"\s*[-—]\s*(?:запущено|running).*$", "", clean, flags=re.I).strip()
        buttons.append({"name": clean or name, "full": name, "running": running,
                        "x": (rect.left + rect.right) // 2, "y": (rect.top + rect.bottom) // 2})
    return buttons


def taskbar_text() -> str:
    try:
        buttons = taskbar()
    except Exception as e:
        return "Ошибка: панель задач недоступна: %s" % e
    if not buttons:
        return "На панели задач кнопок не нашлось."
    return "\n".join("tb_%d: «%s»%s" % (i, b["name"], " (запущено)" if b["running"] else "") for i, b in enumerate(buttons))


def taskbar_click(which: str) -> str:
    """Presses a taskbar button by its tb_N id or by (part of) its name; says which window came to the front."""
    buttons = taskbar()
    key = str(which or "").strip()
    target = None
    if re.fullmatch(r"tb_\d+", key):
        index = int(key[3:])
        target = buttons[index] if index < len(buttons) else None
    else:
        low = key.lower()
        target = next((b for b in buttons if b["name"].lower() == low), None) or \
            next((b for b in buttons if low and low in b["name"].lower()), None)
    if target is None:
        return "Ошибка: кнопки «%s» на панели задач нет. Кнопки:\n%s" % (which, taskbar_text())
    before = {w["hwnd"] for w in windows()}
    click(target["x"], target["y"])
    time.sleep(1.5)
    after = windows()
    new = [w for w in after if w["hwnd"] not in before]
    front = after[-1] if after else None
    said = "Нажал кнопку «%s» на панели задач." % target["name"]
    if new:
        said += " Появились окна: " + ", ".join("«%s» (%s)" % (w["title"], w["process"]) for w in new) + "."
    elif front:
        said += " Впереди окно «%s» (%s)." % (front["title"], front["process"])
    return said


# ---- sound -------------------------------------------------------------------------------------------------------

def audio_status() -> str:
    """Is anything really making sound? The peak level of each program's audio session, over half a second."""
    try:
        from pycaw.pycaw import AudioUtilities
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioMeterInformation
    except ImportError as e:
        return "Ошибка: нет pycaw: %s" % e
    peaks: dict[str, float] = {}
    try:
        sessions = AudioUtilities.GetAllSessions()
        meters = []
        for s in sessions:
            if s.Process is None:
                continue
            meters.append((s.Process.name(), s._ctl.QueryInterface(IAudioMeterInformation)))
        for _ in range(6):
            for name, meter in meters:
                peaks[name] = max(peaks.get(name, 0.0), meter.GetPeakValue())
            time.sleep(0.1)
    except Exception as e:
        return "Ошибка: не смог измерить звук: %s" % e
    loud = {n: v for n, v in peaks.items() if v > 0.003}
    if not loud:
        return "Тишина: ни одна программа не выдаёт звук."
    return "Звук идёт: " + ", ".join("%s (уровень %.2f)" % (n, v) for n, v in sorted(loud.items(), key=lambda x: -x[1]))


# ---- the screen --------------------------------------------------------------------------------------------------

class Screen:
    """The primary monitor as the model sees it: scaled down, with the scale remembered for the clicks."""
    scale = 1.0
    width = height = 0


def _grab():
    from PIL import ImageGrab
    return ImageGrab.grab()


def screenshot_png(long_edge: int = SCREEN_LONG_EDGE) -> bytes:
    img = _grab().convert("RGB")
    width, height = img.size
    scale = min(1.0, long_edge / max(width, height))
    Screen.scale, Screen.width, Screen.height = scale, width, height
    if scale < 1.0:
        from PIL import Image
        img = img.resize((round(width * scale), round(height * scale)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def screenshot() -> dict:
    data = screenshot_png()
    return {"text": "Скриншот %dx%d (экран %dx%d)." % (round(Screen.width * Screen.scale),
                                                      round(Screen.height * Screen.scale), Screen.width, Screen.height),
            "image": base64.b64encode(data).decode()}


def _real(x, y) -> tuple[int, int]:
    """A point in the screenshot the model saw → a point on the screen."""
    if not Screen.width:
        screenshot_png()
    scale = Screen.scale or 1.0
    px, py = round(float(x) / scale), round(float(y) / scale)
    return max(0, min(Screen.width - 1, px)), max(0, min(Screen.height - 1, py))


# ---- mouse and keyboard ------------------------------------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOUSEEVENTF = {"move": 0x0001, "left_down": 0x0002, "left_up": 0x0004, "right_down": 0x0008, "right_up": 0x0010,
               "middle_down": 0x0020, "middle_up": 0x0040, "wheel": 0x0800, "hwheel": 0x1000}
KEYEVENTF_KEYUP, KEYEVENTF_UNICODE = 0x0002, 0x0004

VK = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "super": 0x5B, "win": 0x5B, "windows": 0x5B,
      "cmd": 0x5B, "meta": 0x5B, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
      "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
      "pageup": 0x21, "page_up": 0x21, "pagedown": 0x22, "page_down": 0x22, "up": 0x26, "down": 0x28, "left": 0x25,
      "right": 0x27, "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D}
for _n in range(1, 25):
    VK["f%d" % _n] = 0x6F + _n


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


def _send(inputs: list) -> None:
    array = (INPUT * len(inputs))(*inputs)
    sent = user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        raise OSError("Windows не принял ввод (%d из %d): возможно, активно окно с повышенными правами" % (sent, len(inputs)))


def _mouse(flags: int, data: int = 0) -> INPUT:
    i = INPUT(type=INPUT_MOUSE)
    i.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
    return i


def _key(vk: int, up: bool = False, scan: int = 0, unicode_: bool = False) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_UNICODE if unicode_ else 0)
    i.ki = KEYBDINPUT(0 if unicode_ else vk, scan, flags, 0, 0)
    return i


def move(x: int, y: int) -> None:
    user32.SetCursorPos(int(x), int(y))


def cursor() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _modifiers(text: str) -> list[int]:
    return [VK[m] for m in re.split(r"[+\s]+", (text or "").lower()) if m in VK]


def click(x=None, y=None, button: str = "left", count: int = 1, modifiers: str = "") -> None:
    """x, y are screen pixels (the caller already mapped them); None means where the cursor is."""
    if x is not None:
        move(x, y)
        time.sleep(0.08)
    mods = _modifiers(modifiers)
    _send([_key(vk) for vk in mods]) if mods else None
    down, up = MOUSEEVENTF[button + "_down"], MOUSEEVENTF[button + "_up"]
    for _ in range(count):
        _send([_mouse(down)])
        time.sleep(0.04)
        _send([_mouse(up)])
        time.sleep(0.07)
    _send([_key(vk, up=True) for vk in reversed(mods)]) if mods else None


def drag(x0: int, y0: int, x1: int, y1: int, modifiers: str = "") -> None:
    move(x0, y0)
    time.sleep(0.1)
    _send([_mouse(MOUSEEVENTF["left_down"])])
    steps = 12
    for i in range(1, steps + 1):
        move(x0 + (x1 - x0) * i // steps, y0 + (y1 - y0) * i // steps)
        time.sleep(0.02)
    time.sleep(0.1)
    _send([_mouse(MOUSEEVENTF["left_up"])])


def scroll(direction: str, amount: int, x=None, y=None, modifiers: str = "") -> None:
    if x is not None:
        move(x, y)
        time.sleep(0.05)
    delta = 120 * int(amount)
    if direction == "up":
        _send([_mouse(MOUSEEVENTF["wheel"], delta)])
    elif direction == "down":
        _send([_mouse(MOUSEEVENTF["wheel"], -delta)])
    elif direction == "right":
        _send([_mouse(MOUSEEVENTF["hwheel"], delta)])
    else:
        _send([_mouse(MOUSEEVENTF["hwheel"], -delta)])


def _vk(name: str) -> int:
    key = name.strip().lower()
    if key in VK:
        return VK[key]
    if len(key) == 1:
        found = user32.VkKeyScanW(ord(key)) & 0xFF
        if found != 0xFF:
            return found
    raise ValueError("не знаю клавишу «%s»" % name)


def press(combo: str, repeat: int = 1) -> None:
    """«ctrl+s», «alt+Tab», «Return»: the modifiers go down first, the last key is tapped, then they come up."""
    parts = [p for p in re.split(r"\+(?!$)", combo.strip()) if p] or [combo.strip()]
    codes = [_vk(p) for p in parts]
    for _ in range(max(1, min(int(repeat), 100))):
        _send([_key(c) for c in codes])
        time.sleep(0.03)
        _send([_key(c, up=True) for c in reversed(codes)])
        time.sleep(0.05)


def type_text(text: str) -> None:
    """Literal text, any language, through unicode key events (no keyboard layout games)."""
    for ch in text:
        if ch == "\n":
            press("enter")
            continue
        code = ord(ch)
        units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        _send([_key(0, scan=u, unicode_=True) for u in units] + [_key(0, up=True, scan=u, unicode_=True) for u in units])
        time.sleep(0.008)


def hold(combo: str, seconds: float) -> None:
    codes = [_vk(p) for p in re.split(r"\+", combo.strip()) if p]
    _send([_key(c) for c in codes])
    time.sleep(min(float(seconds), 300))
    _send([_key(c, up=True) for c in reversed(codes)])


def zoom_png(region) -> bytes:
    x0, y0, x1, y1 = (float(v) for v in region)
    px0, py0 = _real(x0, y0)
    px1, py1 = _real(x1, y1)
    if px1 <= px0 or py1 <= py0:
        raise ValueError("область для zoom пустая")
    img = _grab().convert("RGB").crop((px0, py0, px1, py1))
    scale = min(1.0, SCREEN_LONG_EDGE / max(img.size))
    if scale < 1.0 or max(img.size) < 400:
        from PIL import Image
        factor = scale if scale < 1.0 else min(3.0, 800 / max(img.size))
        img = img.resize((max(1, round(img.size[0] * factor)), max(1, round(img.size[1] * factor))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def screen_do(name: str, **a) -> dict:
    """One member of Anthropic's computer toolset, run on this screen. Coordinates are in the screenshot's pixels."""
    def xy(key="coordinate"):
        v = a.get(key)
        return _real(v[0], v[1]) if v else (None, None)

    mods = a.get("text") if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click",
                                     "left_click_drag", "scroll") else ""
    if name == "screenshot":
        return screenshot()
    if name == "zoom":
        return {"text": "Область увеличена.", "image": base64.b64encode(zoom_png(a["region"])).decode()}
    if name in ("left_click", "right_click", "middle_click", "double_click", "triple_click"):
        x, y = xy()
        button = {"right_click": "right", "middle_click": "middle"}.get(name, "left")
        click(x, y, button, {"double_click": 2, "triple_click": 3}.get(name, 1), mods or "")
        return {"text": "OK"}
    if name == "left_click_drag":
        x0, y0 = xy("start_coordinate")
        x1, y1 = xy()
        drag(x0, y0, x1, y1, mods or "")
        return {"text": "OK"}
    if name == "mouse_move":
        x, y = xy()
        move(x, y)
        return {"text": "OK"}
    if name in ("left_mouse_down", "left_mouse_up"):
        _send([_mouse(MOUSEEVENTF["left_down" if name.endswith("down") else "left_up"])])
        return {"text": "OK"}
    if name == "cursor_position":
        px, py = cursor()
        scale = Screen.scale or 1.0
        return {"text": "[%d, %d]" % (round(px * scale), round(py * scale))}
    if name == "scroll":
        x, y = xy()
        scroll(a.get("scroll_direction", "down"), int(a.get("scroll_amount", 3)), x, y, mods or "")
        return {"text": "OK"}
    if name == "type":
        type_text(str(a.get("text", "")))
        return {"text": "OK"}
    if name == "key":
        press(str(a.get("text", "")), int(a.get("repeat") or 1))
        return {"text": "OK"}
    if name == "hold_key":
        hold(str(a.get("text", "")), float(a.get("duration") or 1))
        return {"text": "OK"}
    if name == "wait":
        time.sleep(min(float(a.get("duration") or 1), 300))
        return {"text": "OK"}
    raise ValueError("computer-действия «%s» нет" % name)
