"""Small persistent reminder scheduler independent from voice sessions."""
import threading
import time

from core import memory


class ReminderScheduler:
    def __init__(self, on_due=None, poll_seconds=0.5):
        self.on_due = on_due
        self.poll_seconds = max(0.2, float(poll_seconds))
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="jarvis-reminders", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)

    def _run(self):
        while not self._stop.wait(self.poll_seconds):
            try:
                due = memory.due_reminders(limit=10)
            except Exception:
                continue
            for reminder in due:
                if self._stop.is_set():
                    return
                try:
                    memory.complete_reminder(reminder["id"])
                    if self.on_due:
                        self.on_due(reminder)
                except Exception:
                    continue

    def snapshot(self):
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "pending": len(memory.pending_reminders(100)),
            "next": memory.pending_reminders(1)[0] if memory.pending_reminders(1) else None,
            "checked_at": time.time(),
        }
