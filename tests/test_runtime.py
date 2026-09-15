"""Runtime recovery/cancellation checks without opening audio or touching Windows."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

from core.runtime import AssistantRuntime
from ui.island import UiState


class FakeSession:
    def __init__(self):
        self.voice = Mock()
        self.active = False
        self.history = []
        self.cancel_flag = lambda: False
        self.commands = []
        self.reasons = []
        self.audio_failed = threading.Event()

    def wake_up(self, reason):
        self.reasons.append(reason)
        self.active = True

    def extend(self, kind):
        pass

    def go_to_sleep(self, reason):
        self.active = False

    def handle(self, text):
        self.commands.append(text)

    def run(self, stop_flag, paused_flag):
        self.audio_failed.set()
        raise OSError("microphone disconnected")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.state = UiState()
        self.runtime = AssistantRuntime(self.session, self.state, log=lambda *_: None)

    def tearDown(self):
        self.runtime.stop()
        if self.runtime.thread:
            self.runtime.thread.join(2)
            self.assertFalse(self.runtime.thread.is_alive())

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(predicate())

    def test_text_survives_disconnected_microphone(self):
        self.runtime.start()
        self.assertTrue(self.session.audio_failed.wait(1))
        self.runtime.submit_text("Сколько времени?")
        self.wait_until(lambda: bool(self.session.commands))
        self.assertEqual(self.session.commands, ["Сколько времени?"])
        self.assertTrue(self.runtime.thread.is_alive())
        self.assertIn("keyboard", self.session.reasons)

    def test_microphone_pause_does_not_disable_chat(self):
        self.runtime.set_paused(True)
        self.runtime.submit_text("Привет")
        self.runtime.start()
        self.wait_until(lambda: self.session.commands == ["Привет"])
        self.assertFalse(self.session.audio_failed.is_set())

    def test_stop_discards_queued_actions_and_stops_speech(self):
        self.runtime.submit_text("first")
        self.runtime.submit_text("second")
        self.runtime.cancel()
        self.assertTrue(self.runtime.cancelled.is_set())
        self.assertTrue(self.runtime.requests.empty())
        self.session.voice.stop.assert_called_once()

    def test_queue_is_bounded_and_reports_overload(self):
        for i in range(8):
            self.assertTrue(self.runtime.submit_text(str(i)))
        self.assertFalse(self.runtime.submit_text("overflow"))
        self.assertIn("Очередь", self.state.snapshot()["error"])

    def test_new_conversation_resets_history_and_transcript(self):
        self.session.history = [("user", "old")]
        self.state.add_message("user", "old")
        self.runtime.new_conversation()
        self.runtime.set_paused(True)
        self.runtime.start()
        self.wait_until(lambda: not self.session.history)
        self.assertEqual(self.state.snapshot()["messages"], [])

    def test_diagnostic_and_text_share_one_execution_thread(self):
        seen = []
        self.session.handle = lambda text: seen.append((text, threading.get_ident()))
        self.runtime.set_paused(True)
        self.runtime.diagnostic(lambda: threading.get_ident(), lambda result: seen.append(("diagnostic", result)))
        self.runtime.submit_text("text")
        self.runtime.start()
        self.wait_until(lambda: len(seen) == 2)
        self.assertEqual([x[0] for x in seen], ["diagnostic", "text"])
        self.assertEqual(seen[0][1], seen[1][1])

    def test_failed_command_does_not_kill_worker_or_next_command(self):
        def handle(text):
            if text == "bad":
                raise ValueError("bad input")
            self.session.commands.append(text)
        self.session.handle = handle
        self.runtime.set_paused(True)
        self.runtime.submit_text("bad")
        self.runtime.submit_text("good")
        self.runtime.start()
        self.wait_until(lambda: self.session.commands == ["good"])
        self.assertTrue(any("bad input" in x["text"] for x in self.state.snapshot()["messages"]))

    def test_explicit_target_is_scoped_to_command(self):
        self.runtime.set_paused(True)
        self.runtime.submit_text("type", target=123)
        with patch("core.runtime.actions.target_window") as target:
            self.runtime.start()
            self.wait_until(lambda: bool(self.session.commands))
            target.assert_called_once_with(123)
            target.return_value.__enter__.assert_called_once()
            target.return_value.__exit__.assert_called_once()

    def test_confirmation_survives_status_updates(self):
        confirmation = {"description": "close", "expires_at": time.time() + 20}
        self.state.set(status="asking", confirmation=confirmation)
        self.state.set(status="listening", level=0.1)
        self.state.set(status="paused")
        self.assertEqual(self.state.snapshot()["confirmation"], confirmation)

    def test_duplicate_confirmation_cannot_authorize_next_action(self):
        self.runtime.set_paused(True)
        self.session.pending = {"id": "first"}
        self.state.set(confirmation={"id": "first"})
        def handle(text):
            self.session.commands.append(text)
            self.session.pending = {"id": "second"}
            self.state.set(confirmation={"id": "second"})
        self.session.handle = handle
        self.runtime.submit_confirmation("да", "first")
        self.runtime.submit_confirmation("да", "first")
        self.runtime.start()
        self.wait_until(lambda: "изменилось" in self.state.snapshot()["error"])
        self.assertEqual(self.session.commands, ["да"])

    def test_yes_queued_before_question_does_not_confirm_future_action(self):
        self.runtime.set_paused(True)
        self.runtime.submit_text("да")
        self.session.pending = {"id": "later"}
        self.runtime.start()
        self.wait_until(lambda: "изменилось" in self.state.snapshot()["error"])
        self.assertEqual(self.session.commands, [])

    def test_transcript_is_bounded_and_snapshot_is_separate(self):
        for i in range(220):
            self.state.add_message("user", str(i))
        snap = self.state.snapshot()
        self.assertEqual(len(snap["messages"]), 200)
        snap["messages"].clear()
        self.assertEqual(len(self.state.snapshot()["messages"]), 200)


if __name__ == "__main__":
    unittest.main()
