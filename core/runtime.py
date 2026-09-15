"""One owner for conversation state, audio capture and desktop execution."""
from contextlib import nullcontext
from dataclasses import dataclass
import queue
import threading
import time
import traceback

from core import actions, router, skills


@dataclass(frozen=True)
class Request:
    kind: str
    payload: object = None
    target: int = 0
    confirmation_id: str = ""


class AssistantRuntime:
    def __init__(self, session, state, log=print):
        self.session = session
        self.state = state
        self.log = log
        self.requests = queue.Queue(maxsize=8)
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.cancelled = threading.Event()
        self.session.cancel_flag = self.cancelled.is_set
        self.thread = None
        self._retry_audio_at = 0.0
        self._voice_target = 0
        self._request_target = 0
        self.task_manager = None
        self.target_provider = lambda: 0
        self.session.task_dispatch = self._dispatch_task

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="Jarvis-runtime", daemon=True)
        self.thread.start()

    def _enqueue(self, request):
        if self.stopped.is_set():
            return False
        try:
            self.requests.put_nowait(request)
            return True
        except queue.Full:
            self.state.set(error="Очередь заполнена. Дождитесь ответа или нажмите «Стоп».")
            return False

    def submit_text(self, text, target=0, mode="auto"):
        text = str(text or "").strip()
        if mode in ("computer", "background"):
            return self.submit_task(text, mode, target)
        pending = self.state.snapshot().get("confirmation") or {}
        return bool(text) and self._enqueue(Request("text", text[:16000], int(target or 0), pending.get("id", "")))

    def submit_confirmation(self, answer, confirmation_id, target=0):
        return self._enqueue(Request("confirm", str(answer), int(target or 0), str(confirmation_id)))

    def configure(self):
        return self._enqueue(Request("configure"))

    def submit_task(self, text, kind="computer", target=0):
        text = str(text or "").strip()
        return bool(text) and self._enqueue(Request("task", (text[:16000], kind), int(target or 0)))

    def _dispatch_task(self, goal, kind="computer"):
        if self.task_manager is None:
            return "Менеджер задач недоступен в этом режиме."
        target = self._request_target or self._voice_target or self.target_provider()
        task_id = self.task_manager.submit(goal, kind, target, self.session.history[:-1])
        return "Задача %s принята. Ход работы — во вкладке «Задачи»." % task_id

    def task_notification(self, message):
        self.state.add_message("assistant", message)
        try:
            self.requests.put_nowait(Request("context", str(message)[:12000]))
        except queue.Full:
            pass

    def listen(self, target=0):
        self.session.voice.stop()
        if self.task_manager:
            self.task_manager.pause_running_computer()
        return self._enqueue(Request("listen", target=int(target or 0)))

    def diagnostic(self, handler, done):
        return self._enqueue(Request("diagnostic", (handler, done)))

    def new_conversation(self):
        self.cancel(include_tasks=False)
        return self._enqueue(Request("new"))

    def set_paused(self, value):
        self.paused.set() if value else self.paused.clear()
        self.state.set(paused=bool(value))
        self._retry_audio_at = 0.0

    def cancel(self, include_tasks=True):
        self.cancelled.set()
        self.session.voice.stop()
        if include_tasks and self.task_manager:
            self.task_manager.cancel_all()
        while True:
            try:
                self.requests.get_nowait()
            except queue.Empty:
                break
        self.state.set(progress="Останавливаю…")

    def stop(self):
        self.stopped.set()
        self.cancel()
        if self.task_manager:
            self.task_manager.stop()

    def _interrupted(self):
        return self.stopped.is_set() or self.cancelled.is_set() or not self.requests.empty()

    def _target(self, hwnd):
        factory = getattr(actions, "target_window", None)
        return factory(hwnd) if factory else nullcontext()

    def _process(self, request):
        self.state.set(error="", progress="")
        if request.kind in ("text", "confirm"):
            pending = getattr(self.session, "pending", None)
            if pending and request.confirmation_id != pending.get("id"):
                self.state.set(error="Подтверждение изменилось. Проверьте текущий запрос и ответьте ещё раз.")
                return
            if request.kind == "confirm" and not pending:
                return
            if not self.session.active:
                self.session.wake_up("keyboard")
            self._request_target = request.target
            try:
                with self._target(request.target):
                    if self.task_manager and self.task_manager.computer_busy() and not pending:
                        plan = router.route(request.payload, execute=False)
                        if plan.handled and any(skills.uses_desktop(step["skill"]) for step in plan.steps):
                            self.session._record("user", request.payload)
                            self.session._say(self._dispatch_task(request.payload))
                            return
                    self.session.handle(request.payload)
            finally:
                self._request_target = 0
        elif request.kind == "task":
            goal, kind = request.payload
            self._request_target = request.target
            try:
                self.session._record("user", goal)
                message = self._dispatch_task(goal, kind)
                self.session._say(message)
            finally:
                self._request_target = 0
        elif request.kind == "context":
            self.session.history = (self.session.history + [("assistant", request.payload)])[-24:]
        elif request.kind == "listen":
            self._voice_target = request.target
            self.set_paused(False)
            self.state.set(mic_error="")
            if not self.session.active:
                self.session.wake_up("button")
            else:
                self.session.extend("question")
        elif request.kind == "new":
            self.session.go_to_sleep("new_conversation")
            self.session.history.clear()
            self.state.clear_messages()
            self.state.set(progress="Новый разговор. Чем помочь?")
        elif request.kind == "configure":
            self.session.go_to_sleep("settings")
            self.session.voice.refresh_settings()
            ready = self.session.ears.refresh_settings()
            self._retry_audio_at = 0.0
            self.state.set(mic_error="" if ready else self.session.ears.last_error,
                           progress="Настройки голоса и микрофона применены.")
        elif request.kind == "diagnostic":
            handler, done = request.payload
            if getattr(handler, "uses_microphone", False):
                self.set_paused(False)
            self.state.set(status="checking")
            try:
                message = handler()
            except Exception as exc:
                message = "Ошибка: %s: %s" % (type(exc).__name__, exc)
            done(message)
            self.state.set(status="paused" if self.paused.is_set() else "idle")

    def _run(self):
        com = None
        try:
            try:
                import pythoncom
                pythoncom.CoInitialize()
                com = pythoncom
            except ImportError:
                pass
            self.state.set(status="idle")
            while not self.stopped.is_set():
                if self.cancelled.is_set():
                    self.session.go_to_sleep("cancelled")
                    self.cancelled.clear()
                    self.state.set(progress="Остановлено.")
                try:
                    request = self.requests.get_nowait()
                except queue.Empty:
                    request = None
                if request:
                    try:
                        self._process(request)
                    except Exception as exc:
                        self.log(traceback.format_exc())
                        message = "Не удалось выполнить запрос: %s: %s" % (type(exc).__name__, exc)
                        self.state.add_message("assistant", message)
                        self.state.set(status="error", error=message)
                    continue
                if self.paused.is_set() or time.monotonic() < self._retry_audio_at:
                    self.state.set(status="paused" if self.paused.is_set() else "error")
                    self.stopped.wait(0.1)
                    continue
                try:
                    self.state.set(mic_error="")
                    with self._target(self._voice_target):
                        self.session.run(
                            stop_flag=self._interrupted,
                            paused_flag=self.paused.is_set,
                        )
                    self._voice_target = 0
                except Exception as exc:
                    self.log(traceback.format_exc())
                    message = "Голос недоступен: %s. Можно писать в чат." % str(exc)
                    self.state.set(status="error", mic_error=message, level=0.0)
                    self._retry_audio_at = time.monotonic() + 15.0
        finally:
            if com:
                com.CoUninitialize()
