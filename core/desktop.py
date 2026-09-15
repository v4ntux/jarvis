"""Bounded Windows observations and actions grounded in the latest UIA snapshot.

COM objects never cross the dedicated MTA worker boundary. Screenshots are optional,
kept in memory, and limited to the selected application window.
"""
import base64
from collections import deque
import ctypes
import copy
from ctypes import wintypes
from dataclasses import dataclass, field
import hashlib
import io
import os
import queue
import threading
import time
import uuid

from core import actions, desktop_lock
from core.skills import ActionResult


class DesktopError(actions.ActionError):
    pass


def _check_stop(stop_flag):
    if stop_flag and stop_flag():
        raise DesktopError("Управление компьютером остановлено.")


def _rect(value):
    return {"left": int(value[0]), "top": int(value[1]),
            "width": max(0, int(value[2]) - int(value[0])),
            "height": max(0, int(value[3]) - int(value[1]))}


def _edges(value):
    return (value["left"], value["top"], value["left"] + value["width"],
            value["top"] + value["height"])


def _intersection(first, second):
    left, top = max(first[0], second[0]), max(first[1], second[1])
    right, bottom = min(first[2], second[2]), min(first[3], second[3])
    return (left, top, right, bottom) if right > left and bottom > top else None


class DesktopEngine:
    """Platform-independent validation, with an injectable native UIA adapter."""
    def __init__(self, native, max_elements=120, max_nodes=250, max_depth=10,
                 observation_seconds=3.0, snapshot_seconds=60.0):
        self.native = native
        self.max_elements = max_elements
        self.max_nodes = max_nodes
        self.max_depth = max_depth
        self.observation_seconds = observation_seconds
        self.snapshot_seconds = snapshot_seconds
        self.snapshot = None
        self.entries = {}

    def _window(self):
        hwnd = self.native.foreground()
        if not hwnd:
            raise DesktopError("Нет доступного активного окна Windows.")
        window = self.native.window(hwnd)
        if not window or window["pid"] == os.getpid():
            raise DesktopError("Выберите окно другой программы: Джарвис не управляет собственным интерфейсом.")
        return window

    def observe(self, include_screenshot=False, stop_flag=None):
        _check_stop(stop_flag)
        # Failure must never leave the previous observation actionable.
        self.snapshot, self.entries = None, {}
        window = self._window()
        desktop = self.native.desktop()
        clipping = _intersection(window["rect"], _edges(desktop))
        if not clipping:
            raise DesktopError("Активное окно находится вне видимой области экранов.")
        root = self.native.root(window["hwnd"])
        pending = deque([(root, 0)])
        entries, elements, password_rects = {}, [], []
        visited, truncated = 0, False
        deadline = time.monotonic() + self.observation_seconds
        while pending:
            _check_stop(stop_flag)
            if (visited >= self.max_nodes or len(elements) >= self.max_elements
                    or time.monotonic() >= deadline):
                truncated = True
                break
            element, depth = pending.popleft()
            visited += 1
            try:
                details = self.native.describe(element)
            except Exception:
                truncated = True
                continue
            visible = _intersection(details["rect"], clipping)
            if visible and not details.get("offscreen", False):
                runtime = tuple(details.get("runtime_id") or ())
                # Without a provider identity, this element cannot safely receive later input.
                if runtime:
                    identity = "%s:%s" % (window["pid"], runtime)
                    element_id = "e_" + hashlib.sha256(identity.encode()).hexdigest()[:14]
                    password = bool(details.get("password"))
                    public = {"id": element_id, "role": details.get("role", "control"),
                              "name": "" if password else str(details.get("name", ""))[:300],
                              "value": "" if password else str(details.get("value", ""))[:600],
                              "bounds": _rect(visible), "enabled": bool(details.get("enabled", True)),
                              "password": password, "editable": bool(details.get("editable", False)) and not password}
                    if element_id not in entries:
                        entries[element_id] = (element, details, public)
                        elements.append(public)
                    if password:
                        password_rects.append(visible)
            if details.get("password"):
                continue
            if depth < self.max_depth:
                try:
                    for child in self.native.children(element, min(60, self.max_nodes - visited),
                                                      deadline, stop_flag):
                        pending.append((child, depth + 1))
                except DesktopError:
                    raise
                except Exception:
                    truncated = True
            elif self.native.may_have_children(element):
                truncated = True
        _check_stop(stop_flag)
        current = self._window()
        if (current["hwnd"], current["pid"], current["rect"], current["title"]) != (
                window["hwnd"], window["pid"], window["rect"], window["title"]):
            raise DesktopError("Окно изменилось во время наблюдения. Нужно посмотреть ещё раз.")
        screenshot = None
        if include_screenshot:
            _check_stop(stop_flag)
            screenshot = self.native.screenshot(clipping, password_rects)
            if self.native.foreground() != window["hwnd"]:
                raise DesktopError("Активное окно изменилось во время снимка. Снимок отброшен.")
        _check_stop(stop_flag)
        snapshot = {"snapshot_id": uuid.uuid4().hex, "captured_at": time.time(),
                    "window": {key: window[key] for key in ("hwnd", "pid", "title")},
                    "desktop": desktop, "elements": elements, "truncated": truncated,
                    "screenshot": screenshot}
        self.snapshot = {**snapshot, "_monotonic": time.monotonic(), "_window": window,
                         "_capture_rect": clipping, "_password_rects": password_rects}
        self.entries = entries
        return copy.deepcopy(snapshot)

    def _validate_snapshot(self, snapshot_id, stop_flag):
        _check_stop(stop_flag)
        snapshot = self.snapshot
        if not snapshot or snapshot_id != snapshot["snapshot_id"]:
            raise DesktopError("Наблюдение устарело. Сначала получите новый снимок интерфейса.")
        if time.monotonic() - snapshot["_monotonic"] > self.snapshot_seconds:
            raise DesktopError("Слишком старое наблюдение. Нужно посмотреть на окно ещё раз.")
        window = self._window()
        saved = snapshot["_window"]
        if (window["hwnd"], window["pid"], window["rect"], window.get("created"), window["title"]) != (
                saved["hwnd"], saved["pid"], saved["rect"], saved.get("created"), saved["title"]):
            raise DesktopError("Активное окно изменилось. Действие отменено до нового наблюдения.")
        return window

    def _element(self, element_id):
        if not isinstance(element_id, str) or element_id not in self.entries:
            raise DesktopError("Этого элемента нет в текущем наблюдении.")
        element, saved, public = self.entries[element_id]
        fresh = self.native.refresh(element)
        details = self.native.describe(fresh)
        if (tuple(details.get("runtime_id") or ()) != tuple(saved.get("runtime_id") or ())
                or tuple(details["rect"]) != tuple(saved["rect"])
                or details.get("name", "") != saved.get("name", "")
                or details.get("value", "") != saved.get("value", "")
                or details.get("role") != saved.get("role")):
            raise DesktopError("Элемент интерфейса изменился. Нужно посмотреть на окно ещё раз.")
        if details.get("password"):
            raise DesktopError("Пароль и другие защищённые поля заполняются пользователем.")
        if details.get("offscreen") or not details.get("enabled", True):
            raise DesktopError("Элемент сейчас скрыт или недоступен.")
        return fresh, details, public

    def execute(self, action, params, snapshot_id, stop_flag=None):
        try:
            window = self._validate_snapshot(snapshot_id, stop_flag)
            if not isinstance(params, dict):
                raise DesktopError("Параметры действия должны быть объектом.")
            allowed = {"click_element": {"element_id", "button", "count"},
                       "click_at": {"x", "y", "button", "count"},
                       "type_into_element": {"element_id", "text"},
                       "scroll": {"element_id", "direction", "amount"}, "press_key": {"keys"}}
            if action not in allowed or set(params) - allowed.get(action, set()):
                raise DesktopError("Неизвестное действие или параметр управления интерфейсом.")
            if action == "click_at":
                screenshot = self.snapshot.get("screenshot")
                if not screenshot:
                    raise DesktopError("Для нажатия по координатам сначала нужен снимок этого окна.")
                x, y = params.get("x"), params.get("y")
                button, count = params.get("button", "left"), params.get("count", 1)
                if (type(x) is not int or type(y) is not int or not 0 <= x < screenshot["width"]
                        or not 0 <= y < screenshot["height"] or button not in ("left", "right")
                        or type(count) is not int or count not in (1, 2)):
                    raise DesktopError("Координаты должны находиться внутри показанного снимка окна.")
                rect = self.snapshot["_capture_rect"]
                point = (rect[0] + min(rect[2] - rect[0] - 1,
                                     int((x + .5) * (rect[2] - rect[0]) / screenshot["width"])),
                         rect[1] + min(rect[3] - rect[1] - 1,
                                     int((y + .5) * (rect[3] - rect[1]) / screenshot["height"])))
                if any(left <= point[0] < right and top <= point[1] < bottom
                       for left, top, right, bottom in self.snapshot["_password_rects"]):
                    raise DesktopError("Защищённое поле заполняется пользователем.")
                self._validate_snapshot(snapshot_id, stop_flag)
                self.native.click_at(window["hwnd"], point, button, count, stop_flag)
                return ActionResult(True, "Нажатие передано указанному месту снимка. Нужно проверить результат.")
            if action == "press_key":
                keys = params.get("keys")
                if not isinstance(keys, str) or not keys.strip():
                    raise DesktopError("Укажите сочетание клавиш.")
                _check_stop(stop_flag)
                self.native.press(keys)
                return ActionResult(True, "Сочетание клавиш передано окну. Нужно проверить результат.")
            element, details, public = self._element(params.get("element_id"))
            rect = _edges(public["bounds"])
            if not _intersection(rect, _edges(self.snapshot["desktop"])):
                raise DesktopError("Элемент находится вне экрана.")
            # Recheck after provider calls, immediately before any actual side effect.
            self._validate_snapshot(snapshot_id, stop_flag)
            if action == "click_element":
                button, count = params.get("button", "left"), params.get("count", 1)
                if button not in ("left", "right") or type(count) is not int or count not in (1, 2):
                    raise DesktopError("Нужен один или два щелчка левой или правой кнопкой.")
                self.native.click(element, window["hwnd"], rect, button, count, stop_flag)
                return ActionResult(True, "Нажатие передано элементу. Нужно проверить результат.")
            if action == "type_into_element":
                text = params.get("text")
                if not isinstance(text, str) or len(text) > 12000:
                    raise DesktopError("Поле принимает текст длиной до 12000 символов.")
                if not details.get("editable"):
                    raise DesktopError("Этот элемент не является доступным полем ввода.")
                self.native.set_value(element, text, stop_flag)
                return ActionResult(True, "Текст введён и проверен в выбранном поле.", {"verified": True})
            direction, amount = params.get("direction", "down"), params.get("amount", 3)
            if direction not in ("up", "down") or type(amount) is not int or not 1 <= amount <= 10:
                raise DesktopError("Прокрутка: up или down, от 1 до 10 шагов.")
            self.native.scroll(element, window["hwnd"], rect, direction, amount, stop_flag)
            return ActionResult(True, "Прокрутка передана окну. Нужно проверить результат.")
        except actions.ActionError as exc:
            return ActionResult(False, str(exc))
        except Exception as exc:
            return ActionResult(False, "Windows не выполнила действие интерфейса (%s)." % type(exc).__name__)
        finally:
            self.snapshot, self.entries = None, {}


class NativeUIA:
    """All methods are called only from the single initialized MTA thread."""
    def __init__(self):
        import comtypes.client
        try:
            from comtypes.gen import UIAutomationClient
            self.uia = UIAutomationClient
        except ImportError:
            self.uia = comtypes.client.GetModule("UIAutomationCore.dll")
        self.client = comtypes.client.CreateObject(self.uia.CUIAutomation8,
                                                   interface=self.uia.IUIAutomation2)
        self.client.ConnectionTimeout = 750
        self.client.TransactionTimeout = 750
        self.walker = self.client.ControlViewWalker
        self.cache = self.client.CreateCacheRequest()
        self.cache.TreeScope = self.uia.TreeScope_Element
        for prop in ("RuntimeId", "Name", "ControlType", "BoundingRectangle", "IsOffscreen",
                     "IsEnabled", "IsPassword", "IsKeyboardFocusable", "IsValuePatternAvailable"):
            self.cache.AddProperty(getattr(self.uia, "UIA_%sPropertyId" % prop))
        self.user32 = actions.user32
        self.user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        self.user32.GetWindowRect.restype = wintypes.BOOL
        try:
            self.user32.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            self.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except AttributeError:
            pass
        self.roles = {getattr(self.uia, "UIA_%sControlTypeId" % name): name.lower()
                      for name in ("Button", "Calendar", "CheckBox", "ComboBox", "Edit", "Hyperlink",
                                   "Image", "ListItem", "List", "Menu", "MenuBar", "MenuItem", "ProgressBar",
                                   "RadioButton", "ScrollBar", "Slider", "Spinner", "StatusBar", "Tab",
                                   "TabItem", "Text", "ToolBar", "ToolTip", "Tree", "TreeItem", "Custom",
                                   "Group", "Thumb", "DataGrid", "DataItem", "Document", "SplitButton",
                                   "Window", "Pane", "Header", "HeaderItem", "Table", "TitleBar", "Separator")}

    def foreground(self):
        return actions.foreground_window()

    def window(self, hwnd):
        if not self.user32.IsWindow(hwnd):
            return None
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise DesktopError("Не удалось определить границы окна.")
        pid = actions.window_pid(hwnd)
        import psutil
        try:
            created = psutil.Process(pid).create_time()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            created = None
        return {"hwnd": int(hwnd), "pid": pid, "title": actions._window_title(hwnd)[:300],
                "rect": (rect.left, rect.top, rect.right, rect.bottom), "created": created}

    def desktop(self):
        return {"left": self.user32.GetSystemMetrics(76), "top": self.user32.GetSystemMetrics(77),
                "width": self.user32.GetSystemMetrics(78), "height": self.user32.GetSystemMetrics(79)}

    def root(self, hwnd):
        return self.client.ElementFromHandleBuildCache(hwnd, self.cache)

    def refresh(self, element):
        return element.BuildUpdatedCache(self.cache)

    def _pattern(self, element, name):
        try:
            unknown = element.GetCurrentPattern(getattr(self.uia, "UIA_%sPatternId" % name))
            return unknown.QueryInterface(getattr(self.uia, "IUIAutomation%sPattern" % name)) if unknown else None
        except Exception:
            return None

    def describe(self, element):
        password = bool(element.CachedIsPassword)
        rect = element.CachedBoundingRectangle
        runtime = element.GetCachedPropertyValue(self.uia.UIA_RuntimeIdPropertyId)
        editable = bool(element.GetCachedPropertyValue(self.uia.UIA_IsValuePatternAvailablePropertyId))
        value = ""
        if editable and not password:
            pattern = self._pattern(element, "Value")
            if pattern:
                editable = not bool(pattern.CurrentIsReadOnly)
                value = str(pattern.CurrentValue or "")[:600]
            else:
                editable = False
        return {"runtime_id": tuple(runtime) if runtime is not None else (),
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
                "name": "" if password else str(element.CachedName or "")[:300],
                "value": value, "password": password, "editable": editable,
                "enabled": bool(element.CachedIsEnabled), "offscreen": bool(element.CachedIsOffscreen),
                "role": self.roles.get(element.CachedControlType, "control")}

    def children(self, element, limit, deadline, stop_flag):
        _check_stop(stop_flag)
        if limit <= 0 or time.monotonic() >= deadline:
            return
        child = self.walker.GetFirstChildElementBuildCache(element, self.cache)
        for _ in range(limit):
            _check_stop(stop_flag)
            if not child or time.monotonic() >= deadline:
                break
            yield child
            child = self.walker.GetNextSiblingElementBuildCache(child, self.cache)

    def may_have_children(self, element):
        return True  # Conservative truncation indication at the explicit depth budget.

    def screenshot(self, rect, password_rects):
        from PIL import ImageGrab, ImageDraw
        image = ImageGrab.grab(bbox=rect, all_screens=True)
        painter = ImageDraw.Draw(image)
        for left, top, right, bottom in password_rects:
            painter.rectangle((left - rect[0], top - rect[1], right - rect[0], bottom - rect[1]), fill="#222222")
        original = image.size
        image.thumbnail((1600, 1200))
        data = io.BytesIO()
        image.save(data, format="PNG")
        return {"mime_type": "image/png", "base64": base64.b64encode(data.getvalue()).decode("ascii"),
                "width": image.width, "height": image.height, "origin": {"x": rect[0], "y": rect[1]},
                "original_width": original[0], "original_height": original[1]}

    def _point(self, hwnd, rect, stop_flag):
        _check_stop(stop_flag)
        point = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        if actions.foreground_window() != hwnd or actions._cursor_window(*point) != hwnd:
            raise DesktopError("Элемент закрыт другим окном. Нужно новое наблюдение.")
        if not self.user32.SetCursorPos(*point):
            raise DesktopError("Windows не разрешила переместить курсор.")
        _check_stop(stop_flag)
        return point

    def click(self, element, hwnd, rect, button, count, stop_flag):
        _check_stop(stop_flag)
        if button == "left" and count == 1:
            invoke = self._pattern(element, "Invoke")
            if invoke:
                _check_stop(stop_flag)
                if actions.foreground_window() != hwnd:
                    raise DesktopError("Активное окно изменилось перед нажатием.")
                invoke.Invoke()
                return
        self._point(hwnd, rect, stop_flag)
        self._inject_click(button, count, stop_flag)

    def _inject_click(self, button, count, stop_flag):
        flags = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        events = [actions._INPUT(type=0, u=actions._INPUTUNION(mi=actions._MOUSEINPUT(dwFlags=flag)))
                  for _ in range(count) for flag in flags]
        _check_stop(stop_flag)
        actions._send(events)

    def click_at(self, hwnd, point, button, count, stop_flag):
        _check_stop(stop_flag)
        desktop = _edges(self.desktop())
        if (not desktop[0] <= point[0] < desktop[2] or not desktop[1] <= point[1] < desktop[3]
                or actions.foreground_window() != hwnd or actions._cursor_window(*point) != hwnd):
            raise DesktopError("Указанное место больше не принадлежит выбранному окну.")
        if not self.user32.SetCursorPos(*point):
            raise DesktopError("Windows не разрешила переместить курсор.")
        _check_stop(stop_flag)
        self._inject_click(button, count, stop_flag)

    def set_value(self, element, text, stop_flag):
        pattern = self._pattern(element, "Value")
        if not pattern or pattern.CurrentIsReadOnly:
            raise DesktopError("Поле не поддерживает безопасную замену текста через Windows.")
        _check_stop(stop_flag)
        pattern.SetValue(text)
        _check_stop(stop_flag)
        if str(pattern.CurrentValue or "") != text:
            raise DesktopError("Приложение не подтвердило введённый текст.")

    def scroll(self, element, hwnd, rect, direction, amount, stop_flag):
        pattern = self._pattern(element, "Scroll")
        if pattern and pattern.CurrentVerticallyScrollable:
            vertical = self.uia.ScrollAmount_SmallIncrement if direction == "down" else self.uia.ScrollAmount_SmallDecrement
            for _ in range(amount):
                _check_stop(stop_flag)
                if actions.foreground_window() != hwnd:
                    raise DesktopError("Активное окно изменилось во время прокрутки.")
                pattern.Scroll(self.uia.ScrollAmount_NoAmount, vertical)
            return
        self._point(hwnd, rect, stop_flag)
        delta = 120 * amount * (1 if direction == "up" else -1)
        event = actions._INPUT(type=0, u=actions._INPUTUNION(mi=actions._MOUSEINPUT(
            mouseData=delta & 0xFFFFFFFF, dwFlags=0x0800)))
        _check_stop(stop_flag)
        actions._send([event])

    def press(self, keys):
        actions.hotkey(keys)


@dataclass
class _Request:
    operation: str
    args: tuple
    stop_flag: object = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: object = None


class DesktopWorker:
    def __init__(self, factory=NativeUIA):
        # Both packages initialize COM on their first importing thread. Import here,
        # so their implicit STA initialization never conflicts with the UIA MTA thread.
        import pythoncom
        import comtypes.client
        self.factory = factory
        self.requests = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name="Jarvis-UIAutomation", daemon=True)
        self.thread.start()

    def _run(self):
        import pythoncom
        pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        engine = None
        try:
            while True:
                request = self.requests.get()
                if request.cancelled.is_set():
                    request.done.set()
                    continue
                desktop_lock.IN_FLIGHT.set()
                try:
                    if engine is None:
                        engine = DesktopEngine(self.factory())
                    stop = lambda: request.cancelled.is_set() or bool(request.stop_flag and request.stop_flag())
                    _check_stop(stop)
                    request.result = getattr(engine, request.operation)(*request.args, stop_flag=stop)
                except Exception as exc:
                    request.error = exc
                finally:
                    desktop_lock.IN_FLIGHT.clear()
                    request.done.set()
        finally:
            engine = None
            pythoncom.CoUninitialize()

    def call(self, operation, *args, stop_flag=None, timeout=12):
        _check_stop(stop_flag)
        with desktop_lock.acquire(blocking=False) as acquired:
            if not acquired:
                raise DesktopError("Компьютером управляет другая задача или Windows ещё завершает предыдущую операцию.")
            request = _Request(operation, args, stop_flag)
            try:
                self.requests.put_nowait(request)
            except queue.Full:
                raise DesktopError("Очередь управления компьютером занята.")
            deadline = time.monotonic() + timeout
            while not request.done.wait(0.05):
                if (stop_flag and stop_flag()) or time.monotonic() >= deadline:
                    request.cancelled.set()
                    raise DesktopError("Управление остановлено." if stop_flag and stop_flag() else
                                       "Windows не ответила вовремя. Дождитесь завершения текущего вызова.")
            _check_stop(stop_flag)
            if request.error:
                if isinstance(request.error, actions.ActionError):
                    raise request.error
                raise DesktopError("Не удалось прочитать интерфейс Windows (%s)." % type(request.error).__name__)
            return request.result


_worker = None
_worker_lock = threading.Lock()


def _get_worker():
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = DesktopWorker()
        return _worker


def observe(include_screenshot=False, stop_flag=None):
    return _get_worker().call("observe", bool(include_screenshot), stop_flag=stop_flag)


def execute(action, params, snapshot_id, stop_flag=None):
    try:
        return _get_worker().call("execute", action, params, snapshot_id, stop_flag=stop_flag)
    except actions.ActionError as exc:
        return ActionResult(False, str(exc))
