"""Independent tasks: one desktop owner and two background text workers."""
from dataclasses import dataclass, field
import logging
import queue
import threading
import time
import uuid

from core import actions, ai


TERMINAL = {"completed", "failed", "cancelled"}
WAITING = {"waiting_confirmation", "waiting_input", "paused"}


@dataclass
class Task:
    id: str
    goal: str
    kind: str
    target: int = 0
    generation: int = 0
    state: str = "queued"
    progress: str = "В очереди"
    message: str = ""
    steps: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list = field(default_factory=list)
    checkpoint: dict = field(default_factory=dict)
    pending: dict | None = None
    resume_args: dict = field(default_factory=dict)
    cancelled: threading.Event = field(default_factory=threading.Event)
    pause_requested: threading.Event = field(default_factory=threading.Event)


class TaskManager:
    def __init__(self, on_change=lambda items: None, on_message=lambda text: None,
                 agent_factory=None, background_runner=None, target_context=None):
        self.on_change = on_change
        self.on_message = on_message
        self.agent_factory = agent_factory
        self.background_runner = background_runner or ai.chat
        self.target_context = target_context
        self._lock = threading.RLock()
        self._tasks = {}
        self._queues = {kind: queue.Queue(maxsize=24) for kind in ("computer", "background")}
        self._stop = threading.Event()
        self._threads = []

    def start(self):
        if self._threads:
            return
        for index, kind in enumerate(("computer", "background", "background")):
            thread = threading.Thread(target=self._worker, args=(kind,),
                                      name="Jarvis-task-%d" % index, daemon=True)
            self._threads.append(thread)
            thread.start()

    @staticmethod
    def _view(task):
        return {"id": task.id, "goal": task.goal, "kind": task.kind, "state": task.state,
                "progress": task.progress, "message": task.message, "steps": task.steps,
                "created_at": task.created_at, "updated_at": task.updated_at,
                "pending": {key: task.pending.get(key) for key in ("id", "description", "expires_at")}
                if task.pending else None}

    def snapshot(self):
        with self._lock:
            return [self._view(task) for task in sorted(self._tasks.values(),
                                                       key=lambda item: item.created_at, reverse=True)]

    def _notify(self):
        try:
            self.on_change(self.snapshot())
        except Exception:
            logging.exception("Task UI update failed")

    def _message(self, text):
        try:
            self.on_message(text)
        except Exception:
            logging.exception("Task notification failed")

    def _enqueue_task(self, task):
        """Coalesce stale generations while holding the registry lock."""
        pending = self._queues[task.kind]
        keep = []
        while True:
            try:
                item_id, generation = pending.get_nowait()
            except queue.Empty:
                break
            item = self._tasks.get(item_id)
            if (item_id != task.id and item and item.generation == generation
                    and item.state == "queued" and not item.cancelled.is_set()
                    and (item_id, generation) not in keep):
                keep.append((item_id, generation))
        for item in keep:
            pending.put_nowait(item)
        pending.put_nowait((task.id, task.generation))

    def _update(self, task, **fields):
        with self._lock:
            for key, value in fields.items():
                setattr(task, key, value)
            task.updated_at = time.time()
        self._notify()

    def submit(self, goal, kind="computer", target=0, history=()):
        goal = str(goal or "").strip()
        if not goal:
            raise ValueError("Напишите задачу.")
        if kind not in self._queues:
            raise ValueError("Неизвестный тип задачи.")
        if self._stop.is_set():
            raise RuntimeError("Jarvis завершает работу.")
        with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Jarvis завершает работу.")
            if sum(item.state not in TERMINAL for item in self._tasks.values()) >= 24:
                raise ValueError("Уже есть 24 незавершённые задачи. Завершите или отмените часть из них.")
            task = Task(uuid.uuid4().hex[:8], goal[:16000], kind, int(target or 0),
                        history=list(history)[-24:])
            self._tasks[task.id] = task
            completed = [item.id for item in self._tasks.values() if item.state in TERMINAL]
            for old in completed[:-60]:
                self._tasks.pop(old, None)
            task.generation += 1
            self._enqueue_task(task)
        self._notify()
        return task.id

    def computer_busy(self):
        with self._lock:
            return any(task.kind == "computer" and task.state in ("running", "pausing", "cancelling")
                       for task in self._tasks.values())

    def cancel(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.state in TERMINAL:
                return False
            task.cancelled.set()
            task.pending = None
            waiting = task.state not in ("running", "pausing", "cancelling")
            task.state = "cancelled" if waiting else "cancelling"
            task.progress = "Отменено" if waiting else "Останавливаю после текущей операции"
            task.updated_at = time.time()
        self._notify()
        return True

    def cancel_all(self):
        for task in self.snapshot():
            self.cancel(task["id"])

    def pause(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.kind != "computer" or task.state not in ("queued", "running"):
                return False
            task.pause_requested.set()
            state = "pausing" if task.state == "running" else "paused"
            if state == "paused":
                task.generation += 1
            task.state = state
            task.progress = "Пауза после текущего шага" if state == "pausing" else "На паузе"
            task.updated_at = time.time()
        self._notify()
        return True

    def pause_running_computer(self):
        for task in self.snapshot():
            if task["kind"] == "computer" and task["state"] == "running":
                self.pause(task["id"])

    def resume(self, task_id, confirmation_id=None, approved=None, user_reply=None):
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.state not in WAITING or task.cancelled.is_set():
                return False
            if task.state == "waiting_confirmation":
                if not task.pending or confirmation_id != task.pending.get("id"):
                    return False
                if time.time() >= task.pending.get("expires_at", 0):
                    # Resume without authorization; the agent observes and asks again.
                    approved = None
                    confirmation_id = None
                task.resume_args = {"approval_id": confirmation_id, "approved": approved}
            elif task.state == "waiting_input":
                if not str(user_reply or "").strip():
                    return False
                task.resume_args = {"user_reply": str(user_reply).strip()[:8000]}
            else:
                task.resume_args = {}
            task.pending = None
            task.pause_requested.clear()
            task.state = "queued"
            task.progress = "Продолжение в очереди"
            task.generation += 1
            self._enqueue_task(task)
        self._notify()
        return True

    def _progress(self, task, status):
        self._update(task, progress=str(status.get("message", "Выполняю"))[:1000],
                     steps=int(status.get("step", task.steps)))

    def _worker(self, kind):
        while not self._stop.is_set():
            try:
                task_id, generation = self._queues[kind].get(timeout=0.1)
            except queue.Empty:
                continue
            with self._lock:
                task = self._tasks.get(task_id)
                if not task or task.generation != generation or task.state != "queued" or task.cancelled.is_set():
                    continue
                if task.pause_requested.is_set():
                    task.state = "paused"
                    continue
                task.state = "running"
            self._notify()
            try:
                if kind == "background":
                    self._update(task, progress="Готовлю ответ в фоне")
                    message = self.background_runner(task.goal, history=task.history)
                    if not task.cancelled.is_set():
                        self._finish(task, "completed", str(message))
                else:
                    self._run_computer(task)
            except Exception as exc:
                if not task.cancelled.is_set():
                    self._finish(task, "failed", "Не удалось продолжить: %s" % exc)
            finally:
                if task.cancelled.is_set():
                    self._update(task, state="cancelled", pending=None,
                                 progress="Остановлено. Выполненные действия остаются в силе.")

    def _run_computer(self, task):
        from core import desktop_lock
        from core.computer_agent import ComputerAgent
        cancelled = lambda: task.cancelled.is_set() or task.pause_requested.is_set() or self._stop.is_set()
        while not cancelled():
            with desktop_lock.acquire(blocking=False) as acquired:
                if acquired:
                    if cancelled():
                        result = None
                        break
                    context = self.target_context(task.target) if self.target_context else actions.target_window(task.target)
                    with context:
                        if not self.target_context:
                            actions._ensure_target()
                        factory = self.agent_factory or ComputerAgent
                        agent = factory(task.goal, history=task.history, checkpoint=task.checkpoint,
                                        cancel_flag=cancelled, on_status=lambda status: self._progress(task, status))
                        args, task.resume_args = task.resume_args, {}
                        result = agent.run(**args)
                        task.checkpoint = result.checkpoint or {}
                        # Resumption should return to the last actual app, including a newly opened one.
                        if not self.target_context:
                            task.target = actions.capture_target().get("hwnd", task.target)
                    break
            self._update(task, progress="Ожидаю управление рабочим столом")
            self._stop.wait(0.1)
        else:
            result = None
        if task.cancelled.is_set():
            return
        if task.pause_requested.is_set():
            self._update(task, state="paused", progress="На паузе", pending=None)
            return
        if result is None:
            return
        if result.status == "done":
            self._finish(task, "completed", result.message)
        elif result.status == "needs_confirmation":
            self._update(task, state="waiting_confirmation", pending=result.pending,
                         progress=result.message, message=result.message)
            self._message("Задача %s ждёт подтверждения: %s" % (task.id, result.message))
        elif result.status == "needs_input":
            self._update(task, state="waiting_input", pending=result.pending,
                         progress=result.message, message=result.message)
            self._message("Задача %s: %s" % (task.id, result.message))
        else:
            self._finish(task, "cancelled" if result.status == "cancelled" else "failed", result.message)

    def _finish(self, task, state, message):
        with self._lock:
            if task.cancelled.is_set():
                task.state = "cancelled"
                task.pending = None
                task.progress = "Отменено"
                task.updated_at = time.time()
                notify_message = False
            else:
                task.state, task.message, task.progress = state, message, message
                task.pending = None
                task.updated_at = time.time()
                notify_message = True
        self._notify()
        if not notify_message:
            return
        self._message("Задача %s — %s\n%s" % (task.id,
                        "готово" if state == "completed" else "остановлена", message))

    def stop(self):
        with self._lock:
            self._stop.set()
            self.cancel_all()
