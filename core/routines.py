"""Learned routines: the owner shows a task once, Jarvis repeats it without AI.

Recording binds every click to the UI element under the pointer (role and name via
UI Automation), not to screen coordinates, so a replay survives moved windows and a
changed resolution. Typing into password fields is never recorded: the replay stops
there and asks the owner to type it.
"""
import ctypes
import difflib
import json
import os
import queue
import re
import threading
import time
from ctypes import wintypes

import psutil

from core import actions, settings

PATH = os.path.join(str(settings.ROOT), "routines.json")
MAX_STEPS = 150
MAX_SECONDS = 600
MAX_GAP = 3.0       # a replay never waits longer than this between steps
FIND_TIMEOUT = 8.0  # how long a replay waits for a window or element to appear
DOUBLE_CLICK = 0.5
STOP_HOTKEY = "Ctrl+Alt+R"
# Jarvis's own windows (the panel, consoles) are not part of what the owner shows
OWN_PROCESSES = {"python.exe", "pythonw.exe", "jarvis.exe"}
SPECIAL_KEYS = {vk: name for name, vk in actions.VK.items()
                if name in ("enter", "tab", "esc", "backspace", "delete", "home", "end", "pageup",
                            "pagedown", "left", "up", "right", "down") or re.fullmatch(r"f\d+", name)}
KEY_NAMES = {vk: name for name, vk in sorted(actions.VK.items(), key=lambda item: -len(item[0]))}
MODIFIER_VKS = {0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x14}
# Switching windows is not a step: every step brings its own program forward
SKIPPED_KEYS = {"alt+tab", "alt+shift+tab", "win+tab", "ctrl+alt+tab",
                "ctrl+alt+space", "ctrl+alt+j", "ctrl+alt+x"}  # the last three are Jarvis's own hotkeys
# Nameless areas where only the spot inside the window identifies the click
BLANK_ROLES = {"pane", "document", "window", "custom", "group", "control", "image"}


class RoutineError(actions.ActionError):
    pass


# --- storage -----------------------------------------------------------------
def _norm(name):
    text = str(name or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[«»\"'.,!?]", " ", text)).strip()


def _stem(name):
    """«сделай почту» must find «почта»: Russian endings are cut before comparing."""
    return " ".join(re.sub(r"(?<=\w{3})[аяуюоеыиьйёэ]+$", "", word) for word in _norm(name).split())


def _load():
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PATH)


def find(name):
    """The saved routine for a name, forgiving small differences; None if there is none."""
    data = _load()
    key = _norm(name)
    if key in data:
        return data[key]
    stems = {_stem(k): k for k in data}
    if _stem(name) in stems:
        return data[stems[_stem(name)]]
    close = difflib.get_close_matches(key, list(data), n=1, cutoff=0.85)
    return data[close[0]] if close else None


def names():
    return [r["name"] for r in _load().values()]


def forget(name):
    data = _load()
    routine = find(name)
    if not routine:
        return False
    del data[_norm(routine["name"])]
    _save(data)
    return True


def _steps(n):
    word = "шаг" if n % 10 == 1 and n % 100 != 11 else (
        "шага" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "шагов")
    return "%d %s" % (n, word)


def describe(steps):
    lines = []
    for i, step in enumerate(steps, 1):
        where = step.get("window", {}).get("process", "").removesuffix(".exe")
        if step["type"] == "click":
            what = "«%s»" % step["name"] if step["name"] else "место"
            how = {("left", 1): "нажал", ("left", 2): "дважды нажал", ("right", 1): "правый клик"}.get(
                (step["button"], step["count"]), "нажал")
            lines.append("%d. %s: %s %s (%s)" % (i, where, how, what, step["role"]))
        elif step["type"] == "text":
            lines.append("%d. %s: напечатал «%s»" % (i, where, step["text"][:60]))
        elif step["type"] == "key":
            lines.append("%d. %s: %s" % (i, where, step["keys"]))
        else:
            lines.append("%d. %s: ввод пароля (не записан, при повторе спрошу)" % (i, where))
    return "\n".join(lines)


# --- turning raw input into steps ----------------------------------------------
class StepBuilder:
    """Pure logic: raw clicks and keys in, routine steps out. No Windows calls here."""

    def __init__(self):
        self.steps = []
        self.last_time = None

    def _push(self, step, t):
        step["gap"] = round(t - self.last_time, 2) if self.last_time is not None else 0.0
        self.last_time = t
        self.steps.append(step)

    def _last(self, kind=None):
        if not self.steps:
            return None
        last = self.steps[-1]
        return last if kind is None or last["type"] == kind else None

    def click(self, t, target, button):
        last = self._last("click")
        if (last and last["button"] == button and last["count"] == 1 and t - self.last_time <= DOUBLE_CLICK
                and all(last.get(k) == target.get(k) for k in ("window", "role", "name"))):
            last["count"] = 2
            self.last_time = t
            return
        self._push({"type": "click", "button": button, "count": 1, **target}, t)

    def char(self, t, text, window, password):
        last = self._last()
        if password:
            if not (last and last["type"] == "secret" and last["window"] == window):
                self._push({"type": "secret", "window": window}, t)
            return
        if last and last["type"] == "text" and last["window"] == window:
            last["text"] += text
            self.last_time = t
            return
        self._push({"type": "text", "text": text, "window": window}, t)

    def key(self, t, keys, window):
        last = self._last()
        if keys == "backspace" and last and last["type"] == "text" and last["window"] == window:
            last["text"] = last["text"][:-1]
            if not last["text"]:
                self.steps.pop()
            self.last_time = t
            return
        if last and last["type"] == "secret" and keys == "backspace":
            return
        self._push({"type": "key", "keys": keys, "window": window}, t)


# --- UI Automation on its own thread -------------------------------------------------
class _UIA:
    """One MTA thread owns the UIA client; recording and replay both call through it."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.error = None
        threading.Thread(target=self._run, name="Jarvis-Routines-UIA", daemon=True).start()
        self.ready.wait(10)
        if self.error:
            raise RoutineError("UI Automation не запустилась: %s" % self.error)

    def _run(self):
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        try:
            from core.desktop import NativeUIA
            self.native = NativeUIA()
            self.ids = {role: type_id for type_id, role in self.native.roles.items()}
        except Exception as exc:
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.ready.set()
            return
        self.ready.set()
        while True:
            fn, args, done = self.jobs.get()
            try:
                done(fn(self, *args), None)
            except Exception as exc:
                done(None, exc)

    def call(self, fn, *args, timeout=15):
        box, finished = [], threading.Event()
        self.jobs.put((fn, args, lambda result, error: (box.append((result, error)), finished.set())))
        if not finished.wait(timeout):
            raise RoutineError("Windows долго не отвечает на запрос интерфейса.")
        result, error = box[0]
        if error:
            raise error if isinstance(error, actions.ActionError) else RoutineError(
                "Ошибка интерфейса Windows: %s" % type(error).__name__)
        return result


_uia = None
_uia_lock = threading.Lock()


def _get_uia():
    global _uia
    with _uia_lock:
        if _uia is None:
            _uia = _UIA()
        return _uia


def _window_rect(hwnd):
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _window(hwnd):
    """({"process", "title"}, pid) for a top-level window, or (None, pid) for Jarvis's own."""
    if not hwnd:
        return None, 0
    pid = actions.window_pid(hwnd)
    try:
        process = psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        process = ""
    if not process or pid == os.getpid() or process in OWN_PROCESSES:
        return None, pid
    return {"process": process, "title": actions._window_title(hwnd)[:200]}, pid


def _relative(point, rect):
    left, top, right, bottom = rect
    width, height = max(1, right - left), max(1, bottom - top)
    return [round((point[0] - left) / width, 3), round((point[1] - top) / height, 3)]


def _process_name(pid):
    try:
        return psutil.Process(pid).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return ""


def _resolve_click(u, click):
    """What the owner clicked: window, element role and name, where it sits in the window.

    The hook already noted the window, its program and its rectangle at the moment of the
    press. The element is read a little later, and a menu or dialog may be gone by then:
    whatever UIA finds must belong to the same program, or the click is kept as a spot.
    """
    process = click["process"]
    if not process or click["pid"] == os.getpid() or process in OWN_PROCESSES:
        return None
    x, y, wrect = click["x"], click["y"], click["wrect"]
    window = {"process": process, "title": click["title"]}
    spot = {"window": window, "role": "pane", "name": "", "rel": _relative((x, y), wrect),
            "at": [0.5, 0.5], "point": _relative((x, y), wrect)}
    client = u.native.client
    element = None
    try:
        element = client.ElementFromPoint(u.native.uia.tagPOINT(x, y))
        if element.CurrentProcessId != click["pid"]:
            element = None
            if click["child"] and _user32.IsWindow(click["child"]):
                element = client.ElementFromHandle(click["child"])
    except Exception:
        element = None
    if element is None:
        return spot
    rect = element.CurrentBoundingRectangle
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return spot
    center = ((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
    name = "" if element.CurrentIsPassword else str(element.CurrentName or "")[:200]
    # rel finds the element among look-alikes, at is the spot inside it, point the spot in the window
    return {**spot, "role": u.native.roles.get(element.CurrentControlType, "control"), "name": name,
            "rel": _relative(center, wrect), "at": _relative((x, y), (rect.left, rect.top, rect.right, rect.bottom))}


def _focus_is_password(u):
    try:
        return bool(u.native.client.GetFocusedElement().CurrentIsPassword)
    except Exception:
        return False


# --- recording -------------------------------------------------------------------------
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.CallNextHookEx.argtypes = (wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
_user32.CallNextHookEx.restype = LRESULT
_user32.UnhookWindowsHookEx.argtypes = (wintypes.HHOOK,)
_user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
_user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
_user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
_user32.GetAsyncKeyState.argtypes = (ctypes.c_int,)
_user32.GetAsyncKeyState.restype = ctypes.c_short
_user32.GetKeyState.argtypes = (ctypes.c_int,)
_user32.GetKeyState.restype = ctypes.c_short
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.c_void_p)
_user32.GetKeyboardLayout.argtypes = (wintypes.DWORD,)
_user32.GetKeyboardLayout.restype = ctypes.c_void_p
_user32.ToUnicodeEx.argtypes = (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.LPWSTR,
                                ctypes.c_int, wintypes.UINT, ctypes.c_void_p)
_user32.WindowFromPoint.argtypes = (wintypes.POINT,)
_user32.WindowFromPoint.restype = wintypes.HWND
_user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
_user32.GetAncestor.restype = wintypes.HWND
_user32.IsWindow.argtypes = (wintypes.HWND,)
_user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def _down(vk):
    return bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _to_text(vk, scan, hwnd):
    state = (ctypes.c_ubyte * 256)()
    if _down(0x10):
        state[0x10] = 0x80
    if _user32.GetKeyState(0x14) & 1:
        state[0x14] = 0x01
    layout = _user32.GetKeyboardLayout(_user32.GetWindowThreadProcessId(hwnd, None))
    buf = ctypes.create_unicode_buffer(8)
    # Flag 4: do not disturb the keyboard state (dead keys) of the app being typed into
    count = _user32.ToUnicodeEx(vk, scan, state, buf, 8, 4, layout)
    return buf.value[:count] if count > 0 else ""


class Recorder:
    """Low-level hooks feed a queue; a second thread resolves elements and builds steps."""

    def __init__(self, name, uia, on_stop=None, allow_injected=False):
        self.name = name
        self.uia = uia
        self.on_stop = on_stop
        self.allow_injected = allow_injected  # tests drive the recorder with SendInput
        self.events = queue.Queue()
        self.builder = StepBuilder()
        self.started = time.monotonic()
        self.finished = threading.Event()
        self.hook_thread_id = None
        self._procs = (HOOKPROC(self._keyboard), HOOKPROC(self._mouse))  # keep alive while hooked
        self._names = {}  # pid -> program name, read once per program
        self.reason = ""

    def start(self):
        hooked = threading.Event()
        threading.Thread(target=self._hook_loop, args=(hooked,), name="Jarvis-Routines-Hooks", daemon=True).start()
        threading.Thread(target=self._process, name="Jarvis-Routines-Steps", daemon=True).start()
        if not hooked.wait(5) or self.reason:
            raise RoutineError("Не удалось включить запись: %s" % (self.reason or "Windows не ответила"))

    def stop(self, reason="готово"):
        self.events.put(("stop", reason))
        self.finished.wait(20)
        return self.builder.steps

    def _hook_loop(self, hooked):
        self.hook_thread_id = _kernel32.GetCurrentThreadId()
        try:
            # Hook coordinates are per-monitor physical; WindowFromPoint must read them the same way
            _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
        module = _kernel32.GetModuleHandleW(None)
        hooks = [_user32.SetWindowsHookExW(13, self._procs[0], module, 0),
                 _user32.SetWindowsHookExW(14, self._procs[1], module, 0)]
        if not all(hooks):
            self.reason = "хуки ввода запрещены (код %d)" % ctypes.get_last_error()
        hooked.set()
        msg = wintypes.MSG()
        try:
            while not self.reason and _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                pass
        finally:
            for hook in hooks:
                if hook:
                    _user32.UnhookWindowsHookEx(hook)

    def _keyboard(self, code, wparam, lparam):
        if code == 0 and wparam in (0x100, 0x104):  # WM_KEYDOWN, WM_SYSKEYDOWN
            info = KBDLLHOOKSTRUCT.from_address(lparam)
            if self.allow_injected or not info.flags & 0x10:
                try:
                    self._on_key(info.vkCode, info.scanCode)
                except Exception:
                    pass
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _on_key(self, vk, scan):
        if vk in MODIFIER_VKS:
            return
        ctrl, alt, win, shift = _down(0x11), _down(0x12), _down(0x5B) or _down(0x5C), _down(0x10)
        if ctrl and alt and vk == 0x52:  # Ctrl+Alt+R ends the recording and is not part of it
            self.events.put(("stop", "готово"))
            return
        now, hwnd = time.monotonic(), int(_user32.GetForegroundWindow() or 0)
        if ctrl or alt or win:
            name = KEY_NAMES.get(vk)
            if name:
                mods = [m for m, on in (("ctrl", ctrl), ("alt", alt), ("win", win), ("shift", shift)) if on]
                keys = "+".join(mods + [name])
                if keys not in SKIPPED_KEYS:
                    self.events.put(("key", now, keys, hwnd))
            return
        if vk in SPECIAL_KEYS:
            self.events.put(("key", now, SPECIAL_KEYS[vk], hwnd))
            return
        # VK_PACKET: on-screen keyboards, voice typing and some IMEs put the character in scanCode
        text = chr(scan) if vk == 0xE7 and scan else _to_text(vk, scan, hwnd)
        if text and text.isprintable():
            self.events.put(("char", now, text, hwnd))

    def _mouse(self, code, wparam, lparam):
        if code == 0 and wparam in (0x201, 0x204):  # WM_LBUTTONDOWN, WM_RBUTTONDOWN
            info = MSLLHOOKSTRUCT.from_address(lparam)
            if self.allow_injected or not info.flags & 0x01:
                try:
                    self.events.put(("click", time.monotonic(), self._capture(info.pt.x, info.pt.y),
                                     "left" if wparam == 0x201 else "right"))
                except Exception:
                    pass
        return _user32.CallNextHookEx(None, code, wparam, lparam)

    def _capture(self, x, y):
        """Only quick calls here: the window under the press, before the click changes anything.

        The program name is read now too: a dialog's process may be gone a moment later.
        """
        child = _user32.WindowFromPoint(wintypes.POINT(x, y))
        root = _user32.GetAncestor(child, 2) or child
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        title = ctypes.create_unicode_buffer(200)
        _user32.GetWindowTextW(root, title, 200)
        if pid.value not in self._names:
            self._names[pid.value] = _process_name(pid.value)
        return {"x": x, "y": y, "child": int(child or 0), "pid": int(pid.value), "title": title.value,
                "process": self._names[pid.value], "wrect": _window_rect(root)}

    def _process(self):
        try:
            while True:
                left = MAX_SECONDS - (time.monotonic() - self.started)
                if left <= 0:
                    self.reason = "прошло %d минут" % (MAX_SECONDS // 60)
                    break
                if len(self.builder.steps) >= MAX_STEPS:
                    self.reason = "набралось %d шагов" % MAX_STEPS
                    break
                try:
                    event = self.events.get(timeout=min(left, 1.0))
                except queue.Empty:
                    continue
                if event[0] == "stop":
                    self.reason = event[1]
                    break
                try:
                    self._step(event)
                except Exception:
                    pass  # one unreadable click must not end the recording
        finally:
            if self.hook_thread_id:
                _user32.PostThreadMessageW(self.hook_thread_id, 0x0012, 0, 0)  # WM_QUIT
            # Saved before stop() returns, so a replay right after finds the routine
            if self.on_stop:
                self.on_stop(self)
            self.finished.set()

    def _step(self, event):
        kind, t = event[0], event[1]
        if kind == "click":
            _, _, click, button = event
            target = self.uia.call(_resolve_click, click)
            if target:
                self.builder.click(t, target, button)
            return
        window, _pid = _window(event[3])
        if window is None:
            return
        if kind == "char":
            starting = self.builder._last("text") is None
            password = self.uia.call(_focus_is_password) if starting or self.builder._last("secret") else False
            self.builder.char(t, event[2], window, password)
        else:
            self.builder.key(t, event[2], window)


_recorder = None
_recorder_lock = threading.Lock()
# Set by hands.py: tells the owner about a recording that ended without a command (hotkey, limit)
on_finish = None
BY_COMMAND = "по команде"


def _beep(kind):
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK if kind == "start" else winsound.MB_ICONASTERISK)
    except Exception:
        pass


def _finish(recorder):
    """Saves what was recorded once the recording ends, by hotkey, by command or by limit."""
    global _recorder
    with _recorder_lock:
        if _recorder is recorder:
            _recorder = None
    steps = recorder.builder.steps
    _beep("stop")
    if steps:
        data = _load()
        data[_norm(recorder.name)] = {"name": recorder.name, "created": time.time(), "steps": steps}
        _save(data)
    if on_finish and recorder.reason != BY_COMMAND:
        try:
            on_finish(_summary(recorder))
        except Exception:
            pass


def _summary(recorder):
    steps = recorder.builder.steps
    ended = "" if recorder.reason in ("готово", BY_COMMAND) else "Запись закончилась: %s. " % recorder.reason
    if not steps:
        return ended + "Действий не было, сохранять нечего."
    return "%sЗапомнил приём «%s», %s:\n%s" % (ended, recorder.name, _steps(len(steps)), describe(steps))


def start_recording(name, allow_injected=False):
    global _recorder
    name = str(name or "").strip()
    if not _norm(name):
        raise RoutineError("Нужно название приёма, например «отчёт в телеграм».")
    with _recorder_lock:
        if _recorder is not None:
            raise RoutineError("Уже идёт запись «%s». Закончи её: %s." % (_recorder.name, STOP_HOTKEY))
        recorder = Recorder(name, _get_uia(), on_stop=_finish, allow_injected=allow_injected)
        recorder.start()
        _recorder = recorder
    _beep("start")
    return ("Записываю приём «%s». Делай всё как обычно мышкой и клавиатурой; пароли не записываю. "
            "Когда закончишь, нажми %s на ноутбуке или скажи «готово». Максимум %d минут."
            % (name, STOP_HOTKEY, MAX_SECONDS // 60))


def stop_recording():
    recorder = _recorder
    if recorder is None:
        raise RoutineError("Запись сейчас не идёт.")
    recorder.stop(BY_COMMAND)
    return _summary(recorder)


def recording():
    recorder = _recorder
    return recorder.name if recorder else None


# --- replay ------------------------------------------------------------------------------
def _process_windows(process):
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == process}
    return [(hwnd, title) for hwnd, title in actions._enum_windows() if actions.window_pid(hwnd) in pids]


def _bring(window, stop_flag):
    """Makes a window of the step's program active; returns its hwnd."""
    deadline = time.monotonic() + FIND_TIMEOUT
    while True:
        _check(stop_flag)
        hwnd = actions.foreground_window()
        if hwnd and _window(hwnd)[0] and _window(hwnd)[0]["process"] == window["process"]:
            break
        candidates = _process_windows(window["process"])
        if candidates:
            ratio = lambda c: difflib.SequenceMatcher(None, c[1], window.get("title", "")).ratio()
            hwnd = max(candidates, key=ratio)[0]
            actions._activate(hwnd)
            break
        if time.monotonic() >= deadline:
            raise RoutineError("не нашёл окно программы %s" % window["process"].removesuffix(".exe"))
        time.sleep(0.4)
    actions._selected_target(hwnd)
    return hwnd


def _candidates(u, process, role, name):
    """Visible elements with this role (and name) in every top-level window of the program."""
    native = u.native
    uia = native.uia
    client = native.client
    pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == process}
    type_id = u.ids.get(role)
    found = []
    root = client.GetRootElement()
    for pid in pids:
        tops = root.FindAll(uia.TreeScope_Children, client.CreatePropertyCondition(uia.UIA_ProcessIdPropertyId, pid))
        for i in range(tops.Length):
            top = tops.GetElement(i)
            r = top.CurrentBoundingRectangle
            top_rect = (r.left, r.top, r.right, r.bottom)
            conditions = [client.CreatePropertyCondition(uia.UIA_ControlTypePropertyId, type_id)] if type_id else []
            if name:
                conditions.append(client.CreatePropertyCondition(uia.UIA_NamePropertyId, name))
            if not conditions:
                continue
            condition = conditions[0] if len(conditions) == 1 else client.CreateAndCondition(*conditions)
            matches = top.FindAll(uia.TreeScope_Subtree, condition)
            for j in range(matches.Length):
                element = matches.GetElement(j)
                er = element.CurrentBoundingRectangle
                if er.right <= er.left or er.bottom <= er.top or element.CurrentIsOffscreen:
                    continue
                center = ((er.left + er.right) // 2, (er.top + er.bottom) // 2)
                found.append({"rect": (er.left, er.top, er.right, er.bottom),
                              "rel": _relative(center, top_rect), "pid": pid})
    return found


def _pick(candidates, step):
    if not candidates:
        return None
    distance = lambda c: abs(c["rel"][0] - step["rel"][0]) + abs(c["rel"][1] - step["rel"][1])
    best = min(candidates, key=distance)
    # A nameless element is only trusted when it sits about where the owner clicked
    return best if step["name"] or distance(best) <= 0.12 else None


def _click_point(point, pid, button, count):
    if actions.window_pid(actions._cursor_window(*point)) != pid:
        raise RoutineError("нужное место закрыто другим окном")
    if not actions.user32.SetCursorPos(*point):
        raise RoutineError("Windows не разрешила переместить курсор")
    flags = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
    actions._send([actions._INPUT(type=0, u=actions._INPUTUNION(mi=actions._MOUSEINPUT(dwFlags=flag)))
                   for _ in range(count) for flag in flags])


def _check(stop_flag):
    if stop_flag and stop_flag():
        raise RoutineError("остановлено")


def _inside(rect, frac):
    left, top, right, bottom = rect
    fx, fy = (min(0.95, max(0.05, f)) for f in frac)
    return int(left + fx * (right - left)), int(top + fy * (bottom - top))


def _click_step(u, step, hwnd):
    """On the UIA thread, which is DPI aware: element and cursor coordinates agree there."""
    target = _pick(_candidates(u, step["window"]["process"], step["role"], step["name"]), step)
    if target:
        _click_point(_inside(target["rect"], step.get("at", (0.5, 0.5))), target["pid"],
                     step["button"], step["count"])
        return True
    if not step["name"] and step["role"] in BLANK_ROLES:
        # Blank areas (a text page, a canvas): the spot inside the window is all there is
        _click_point(_inside(_window_rect(hwnd), step["point"]), actions.window_pid(hwnd),
                     step["button"], step["count"])
        return True
    return False


def _do_click(step, stop_flag, uia):
    hwnd = _bring(step["window"], stop_flag)
    deadline = time.monotonic() + FIND_TIMEOUT
    while True:
        _check(stop_flag)
        if uia.call(_click_step, step, hwnd):
            return
        if time.monotonic() >= deadline:
            what = "«%s»" % step["name"] if step["name"] else step["role"]
            raise RoutineError("не нашёл %s в %s" % (what, step["window"]["process"].removesuffix(".exe")))
        time.sleep(0.5)


def replay(name, stop_flag=None, uia=None):
    """Runs a saved routine step by step; returns what happened in one line for the owner."""
    routine = find(name)
    if not routine:
        raise RoutineError("Приёма «%s» нет. Есть: %s." % (name, ", ".join(names()) or "пока ни одного"))
    steps = routine["steps"]
    uia = uia or _get_uia()
    for i, step in enumerate(steps, 1):
        time.sleep(max(0.3, min(float(step.get("gap") or 0), MAX_GAP)))
        try:
            _check(stop_flag)
            if step["type"] == "secret":
                return ("Приём «%s»: дошёл до шага %d из %d, там нужно ввести пароль. Введи его сам, "
                        "дальше я не продолжаю." % (routine["name"], i, len(steps)))
            if step["type"] == "click":
                _do_click(step, stop_flag, uia)
            else:
                _bring(step["window"], stop_flag)
                if step["type"] == "text":
                    actions.type_text(step["text"])
                else:
                    actions.hotkey(step["keys"])
        except actions.ActionError as exc:
            return "Ошибка: приём «%s» остановился на шаге %d из %d: %s.\n%s" % (
                routine["name"], i, len(steps), str(exc).rstrip("."), describe([step]).split(". ", 1)[-1])
    return "Приём «%s» выполнен: %s." % (routine["name"], _steps(len(steps)))


COMMAND = re.compile(r"^(?:сделай|запусти|выполни|повтори|делай)\s+(?:при[её]м\s+)?(?P<name>.+)$|^при[её]м\s+(?P<bare>.+)$",
                     re.I)


def match_command(text):
    """«сделай отчёт» → the routine «отчёт» if one is saved; None otherwise (free to check)."""
    m = COMMAND.match(_norm(text))
    if not m:
        return None
    routine = find(m.group("name") or m.group("bare"))
    return routine["name"] if routine else None
