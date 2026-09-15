"""Task scheduling adversarial checks with fake AI/agents and no desktop input."""
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch

from core.tasks import TaskManager, TERMINAL


def outcome(status="done", message="done", checkpoint=None, pending=None):
    return SimpleNamespace(status=status, message=message,
                           checkpoint=checkpoint or {}, pending=pending)


class FakeAgent:
    def __init__(self, run):
        self.run = run


class TaskManagerTests(unittest.TestCase):
    def setUp(self):
        self.changed = threading.Condition()
        self.managers = []
        self.releases = []
        self.messages = []
        self.views = []

    def event(self):
        event = threading.Event()
        self.releases.append(event)
        return event

    def manager(self, agent_factory=None, background_runner=None, on_message=None, on_change=None):
        def changed(items):
            with self.changed:
                self.views.append(items)
                self.changed.notify_all()
            if on_change:
                on_change(items)

        def message(text):
            with self.changed:
                self.messages.append(text)
                self.changed.notify_all()
            if on_message:
                on_message(text)

        manager = TaskManager(
            on_change=changed, on_message=message,
            agent_factory=agent_factory or (lambda goal, **_: FakeAgent(lambda **__: outcome(message=goal))),
            background_runner=background_runner or (lambda goal, history=(): "answer: " + goal),
            target_context=lambda _target: nullcontext(),
        )
        self.managers.append(manager)
        return manager

    def tearDown(self):
        for manager in self.managers:
            manager.stop()
        for event in self.releases:
            event.set()
        for manager in self.managers:
            for thread in manager._threads:
                thread.join(3)
                self.assertFalse(thread.is_alive(), "scheduler thread did not stop")

    def wait_until(self, predicate, timeout=3):
        deadline = time.monotonic() + timeout
        with self.changed:
            while not predicate():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.fail("scheduler condition was not reached")
                self.changed.wait(remaining)

    @staticmethod
    def task(manager, task_id):
        return next(task for task in manager.snapshot() if task["id"] == task_id)

    def wait_state(self, manager, task_id, state):
        self.wait_until(lambda: self.task(manager, task_id)["state"] == state)
        return self.task(manager, task_id)

    def test_two_background_jobs_run_while_only_one_computer_job_owns_desktop(self):
        computer_started = self.event()
        backgrounds_started = self.event()
        release_computer = self.event()
        release_background = self.event()
        lock = threading.Lock()
        active = {"computer": 0, "background": 0}
        peak = {"computer": 0, "background": 0}

        def work(kind, goal):
            with lock:
                active[kind] += 1
                peak[kind] = max(peak[kind], active[kind])
                if kind == "computer":
                    computer_started.set()
                elif active[kind] == 2:
                    backgrounds_started.set()
            event = release_computer if kind == "computer" else release_background
            if not event.wait(3):
                raise RuntimeError("test work was not released")
            with lock:
                active[kind] -= 1
            return goal

        manager = self.manager(
            agent_factory=lambda goal, **_: FakeAgent(lambda **__: outcome(message=work("computer", goal))),
            background_runner=lambda goal, history=(): work("background", goal),
        )
        computer_ids = [manager.submit("computer-%d" % n) for n in range(2)]
        background_ids = [manager.submit("background-%d" % n, kind="background") for n in range(3)]
        manager.start()
        self.assertTrue(computer_started.wait(2))
        self.assertTrue(backgrounds_started.wait(2))
        self.assertEqual(self.task(manager, computer_ids[1])["state"], "queued")
        self.assertEqual(self.task(manager, background_ids[2])["state"], "queued")
        release_computer.set()
        release_background.set()
        self.wait_until(lambda: all(task["state"] in TERMINAL for task in manager.snapshot()))
        self.assertEqual(peak, {"computer": 1, "background": 2})
        self.assertTrue(all(task["state"] == "completed" for task in manager.snapshot()))

    def test_cancelled_blocked_ai_result_cannot_publish_or_cancel_another_task(self):
        ai_started = self.event()
        release_ai = self.event()

        def background(goal, history=()):
            if goal == "old":
                ai_started.set()
                release_ai.wait(3)
                return "obsolete response"
            return "independent response"

        manager = self.manager(background_runner=background)
        old = manager.submit("old", kind="background")
        manager.start()
        self.assertTrue(ai_started.wait(2))
        self.assertTrue(manager.cancel(old))
        new = manager.submit("new", kind="background")
        self.wait_state(manager, new, "completed")
        release_ai.set()
        self.wait_state(manager, old, "cancelled")
        self.assertTrue(manager._tasks[old].cancelled.is_set())
        self.assertEqual(self.task(manager, new)["message"], "independent response")
        self.assertFalse(any("obsolete response" in message for message in self.messages))
        self.assertFalse(manager.resume(old))

    def test_pause_preserves_checkpoint_and_resume_does_not_repeat_first_step(self):
        first_started = self.event()
        release_first = self.event()
        calls = []

        def factory(goal, checkpoint, cancel_flag, **_):
            calls.append(dict(checkpoint))

            def run(**args):
                if not checkpoint:
                    first_started.set()
                    release_first.wait(3)
                    if not cancel_flag():
                        raise RuntimeError("pause was not visible to the active agent")
                    return outcome("cancelled", "checkpoint reached", {"next_step": 2})
                return outcome("done", "second step completed", {"next_step": 3})
            return FakeAgent(run)

        manager = self.manager(agent_factory=factory)
        task_id = manager.submit("two-step task")
        manager.start()
        self.assertTrue(first_started.wait(2))
        self.assertTrue(manager.pause(task_id))
        release_first.set()
        self.wait_state(manager, task_id, "paused")
        self.assertFalse(manager._tasks[task_id].cancelled.is_set())
        self.assertEqual(manager._tasks[task_id].checkpoint, {"next_step": 2})
        self.assertTrue(manager.resume(task_id))
        self.wait_state(manager, task_id, "completed")
        self.assertEqual(calls, [{}, {"next_step": 2}])

    def test_confirmation_ids_belong_to_task_and_duplicate_cannot_resume_twice(self):
        approvals = []

        def factory(goal, **_):
            def run(**args):
                if args.get("approved"):
                    approvals.append((goal, args.get("approval_id")))
                    return outcome(message=goal)
                pending = {"id": "confirm-" + goal, "description": goal, "expires_at": time.time() + 60}
                return outcome("needs_confirmation", goal, {"goal": goal}, pending)
            return FakeAgent(run)

        manager = self.manager(agent_factory=factory)
        first = manager.submit("first")
        second = manager.submit("second")
        manager.start()
        self.wait_state(manager, first, "waiting_confirmation")
        self.wait_state(manager, second, "waiting_confirmation")
        self.assertFalse(manager.resume(first, "confirm-second", True))
        self.assertTrue(manager.resume(first, "confirm-first", True))
        self.assertFalse(manager.resume(first, "confirm-first", True))
        self.wait_state(manager, first, "completed")
        self.assertEqual(self.task(manager, second)["pending"]["id"], "confirm-second")
        self.assertTrue(manager.resume(second, "confirm-second", True))
        self.wait_state(manager, second, "completed")
        self.assertEqual(approvals, [("first", "confirm-first"), ("second", "confirm-second")])

    def test_expired_confirmation_resumes_without_authorization(self):
        received = []

        def factory(goal, **_):
            def run(**args):
                received.append(args)
                pending = {"id": "old" if len(received) == 1 else "fresh", "description": goal,
                           "expires_at": time.time() - 1 if len(received) == 1 else time.time() + 60}
                return outcome("needs_confirmation", goal, {"goal": goal}, pending)
            return FakeAgent(run)

        manager = self.manager(agent_factory=factory)
        task_id = manager.submit("approval")
        manager.start()
        self.wait_state(manager, task_id, "waiting_confirmation")
        self.assertTrue(manager.resume(task_id, "old", True))
        self.wait_until(lambda: (self.task(manager, task_id)["pending"] or {}).get("id") == "fresh")
        self.assertEqual(received[1], {"approval_id": None, "approved": None})

    def test_stale_queued_generations_never_execute_twice(self):
        calls = []
        manager = self.manager(agent_factory=lambda goal, **_: FakeAgent(
            lambda **__: (calls.append(goal), outcome(message=goal))[1]))
        task_id = manager.submit("once")
        for _ in range(5):
            self.assertTrue(manager.pause(task_id))
            self.assertTrue(manager.resume(task_id))
        manager.start()
        self.wait_state(manager, task_id, "completed")
        self.assertEqual(calls, ["once"])

    def test_repeated_queued_pause_resume_does_not_exhaust_queue(self):
        manager = self.manager()
        task_id = manager.submit("one live task")
        for _ in range(35):
            self.assertTrue(manager.pause(task_id))
            self.assertTrue(manager.resume(task_id))
        manager.start()
        self.wait_state(manager, task_id, "completed")

    def test_out_of_order_background_results_keep_task_identity_and_history(self):
        slow_started = self.event()
        release_slow = self.event()
        received = {}

        def background(goal, history):
            received[goal] = list(history)
            if goal == "slow":
                slow_started.set()
                release_slow.wait(3)
            return "answer for " + goal

        manager = self.manager(background_runner=background)
        slow = manager.submit("slow", "background", history=[("user", "slow-only")])
        fast = manager.submit("fast", "background", history=[("user", "fast-only")])
        manager.start()
        self.assertTrue(slow_started.wait(2))
        self.wait_state(manager, fast, "completed")
        self.assertEqual(self.task(manager, slow)["state"], "running")
        release_slow.set()
        self.wait_state(manager, slow, "completed")
        self.assertEqual(received, {"slow": [("user", "slow-only")], "fast": [("user", "fast-only")]})
        self.assertEqual(self.task(manager, slow)["message"], "answer for slow")
        self.assertEqual(self.task(manager, fast)["message"], "answer for fast")
        self.assertTrue(any(fast in message and "answer for fast" in message for message in self.messages))
        self.assertTrue(any(slow in message and "answer for slow" in message for message in self.messages))

    def test_cancel_between_ai_return_and_finish_cannot_publish_success(self):
        finishing = self.event()
        release_finish = self.event()
        manager = self.manager()
        original_finish = manager._finish

        def delayed_finish(task, state, message):
            if state == "completed":
                finishing.set()
                release_finish.wait(3)
            return original_finish(task, state, message)

        with patch.object(manager, "_finish", side_effect=delayed_finish):
            task_id = manager.submit("late result", "background")
            manager.start()
            self.assertTrue(finishing.wait(2))
            self.assertTrue(manager.cancel(task_id))
            release_finish.set()
            self.wait_state(manager, task_id, "cancelled")
        self.assertFalse(any(task_id in message and "готово" in message for message in self.messages))
        self.assertFalse(any(task["id"] == task_id and task["state"] == "completed"
                             for snapshot in self.views for task in snapshot))

    def test_callback_failure_does_not_erase_success_or_kill_computer_worker(self):
        def bad_message(_):
            raise RuntimeError("UI callback failed")
        manager = self.manager(on_message=bad_message)
        first = manager.submit("first")
        second = manager.submit("second")
        manager.start()
        self.wait_until(lambda: self.task(manager, second)["state"] in TERMINAL)
        self.assertEqual(self.task(manager, first)["state"], "completed")
        self.assertEqual(self.task(manager, second)["state"], "completed")

    def test_status_callback_failure_does_not_kill_worker_before_agent_runs(self):
        def bad_status(items):
            if any(task["state"] == "running" for task in items):
                raise RuntimeError("status callback failed")
        manager = self.manager(on_change=bad_status)
        task_id = manager.submit("still works")
        manager.start()
        self.wait_state(manager, task_id, "completed")

    def test_cancel_after_desktop_acquisition_does_not_start_agent(self):
        from core import desktop_lock
        acquired = self.event()
        release_lease = self.event()
        calls = []
        manager = self.manager(agent_factory=lambda goal, **_: (
            calls.append(goal), FakeAgent(lambda **__: outcome(message=goal)))[1])

        @contextmanager
        def lease(**_):
            acquired.set()
            release_lease.wait(3)
            yield True

        with patch.object(desktop_lock, "acquire", side_effect=lease):
            task_id = manager.submit("cancel before action")
            manager.start()
            self.assertTrue(acquired.wait(2))
            self.assertTrue(manager.cancel(task_id))
            release_lease.set()
            self.wait_state(manager, task_id, "cancelled")
        self.assertEqual(calls, [])

    def test_shutdown_marks_stopped_before_cancel_all_so_new_task_is_rejected(self):
        cancel_started = self.event()
        release_cancel = self.event()
        manager = self.manager()
        original_cancel_all = manager.cancel_all

        def delayed_cancel():
            original_cancel_all()
            cancel_started.set()
            release_cancel.wait(3)

        with patch.object(manager, "cancel_all", side_effect=delayed_cancel):
            stop_thread = threading.Thread(target=manager.stop)
            stop_thread.start()
            self.assertTrue(cancel_started.wait(2))
            try:
                with self.assertRaises(RuntimeError):
                    manager.submit("must not start")
            finally:
                release_cancel.set()
                stop_thread.join(2)
        self.assertFalse(stop_thread.is_alive())

    def test_cancel_waiting_for_desktop_never_starts_agent(self):
        from core import desktop_lock
        attempted = self.event()
        calls = []
        manager = self.manager(agent_factory=lambda goal, **_: calls.append(goal))

        def unavailable(**_):
            attempted.set()
            return nullcontext(False)

        with patch.object(desktop_lock, "acquire", side_effect=unavailable):
            task_id = manager.submit("waiting")
            manager.start()
            self.assertTrue(attempted.wait(2))
            self.assertTrue(manager.cancel(task_id))
            self.wait_state(manager, task_id, "cancelled")
        self.assertEqual(calls, [])

    def test_cancel_queued_task_keeps_other_task_runnable(self):
        manager = self.manager()
        first = manager.submit("cancel me")
        second = manager.submit("keep me")
        self.assertTrue(manager.cancel(first))
        manager.start()
        self.wait_state(manager, second, "completed")
        self.assertEqual(self.task(manager, first)["state"], "cancelled")


if __name__ == "__main__":
    unittest.main()
