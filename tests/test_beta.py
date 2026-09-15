"""Jarvis 1.0 Beta invariants: strict wake, bounded confirmation and SQLite services."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import ai, memory, router, skills
from core import actions
from core.session import ConversationEndDetector, Session


class FakeVoice:
    def __init__(self):
        self.said = []

    def say(self, text, wait=True):
        self.said.append(text)

    def stop(self):
        pass


class FakeEars:
    last_stt = "local"


class BetaTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(memory, "PATH", Path(self.tmp.name) / "learned.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        memory._cache = None


class StrictWakeTests(BetaTestCase):
    def test_handle_is_inert_without_active_wake_session(self):
        voice = FakeVoice()
        session = Session(FakeEars(), voice, log=lambda *_: None, require_wake=True)
        with mock.patch.object(router, "route") as route:
            self.assertFalse(session.handle("открой браузер"))
        route.assert_not_called()
        self.assertEqual(voice.said, [])

    def test_verified_session_allows_command(self):
        voice = FakeVoice()
        session = Session(FakeEars(), voice, log=lambda *_: None, require_wake=True)
        session.wake_up("test")
        with mock.patch.object(router, "route", return_value=router.Result("Готово.", action="test")):
            self.assertTrue(session.handle("команда"))
        self.assertIn("Готово.", voice.said)

    def test_safe_route_defers_close(self):
        with mock.patch.object(router.actions, "close_app") as close:
            result = router.route("закрой блокнот", safe=True)
        close.assert_not_called()
        self.assertEqual(result.skill, "close_app")
        self.assertTrue(result.reply.startswith("__CONFIRM__"))

    def test_confirmation_is_bound_to_session(self):
        voice = FakeVoice()
        session = Session(FakeEars(), voice, log=lambda *_: None, require_wake=True)
        session.wake_up("test")
        session._request_confirmation("выключить компьютер", "shutdown", {})
        session.session_id = "different-session"
        with mock.patch.object(skills, "execute") as run:
            session.handle("да")
        run.assert_not_called()
        self.assertTrue(any("устарело" in line for line in voice.said))


class ConversationEndTests(unittest.TestCase):
    def test_explicit_end(self):
        self.assertTrue(ConversationEndDetector.explicit("спасибо, это всё"))

    def test_silence_ends_completed_action(self):
        self.assertGreaterEqual(ConversationEndDetector.score(16), 70)

    def test_pending_question_stays_open(self):
        self.assertLess(ConversationEndDetector.score(
            16, pending=True, last_reply_question=True), 70)


class TextToolsTests(BetaTestCase):
    def test_dictation_is_bounded_to_active_session_and_has_explicit_stop(self):
        voice = FakeVoice()
        session = Session(FakeEars(), voice, log=lambda *_: None, require_wake=True)
        session.wake_up("test")
        session.handle("включи диктовку")
        self.assertTrue(session.dictation)
        with mock.patch.object(actions, "type_text") as type_text:
            session.handle("Привет запятая мир точка")
        type_text.assert_called_once_with("Привет, мир. ")
        session.handle("стоп диктовка")
        self.assertFalse(session.dictation)
        session.go_to_sleep()
        self.assertFalse(session.dictation)

    def test_clipboard_transform_is_text_only_and_reversible(self):
        voice = FakeVoice()
        session = Session(FakeEars(), voice, log=lambda *_: None, require_wake=True)
        session.wake_up("test")
        written = []
        with mock.patch.object(actions, "clipboard_get", return_value="сырой текст"), \
                mock.patch.object(actions, "clipboard_set", side_effect=written.append), \
                mock.patch.object(ai, "enabled", return_value=True), \
                mock.patch.object(ai, "chain", return_value=["test"]), \
                mock.patch.object(ai, "transform_text", return_value="Готовый текст"):
            session.handle("улучши текст в буфере")
            session.handle("верни прошлый буфер")
        self.assertEqual(written, ["Готовый текст", "сырой текст"])


class OperationalMemoryTests(BetaTestCase):
    def test_notes_reminders_and_routines_persist(self):
        note_id = memory.add_note("купить кабель")
        self.assertTrue(note_id)
        self.assertEqual(memory.list_notes()[0]["text"], "купить кабель")

        reminder_id = memory.add_reminder("таймер", time.time() - 1)
        self.assertEqual(memory.due_reminders()[0]["id"], reminder_id)
        self.assertTrue(memory.complete_reminder(reminder_id))
        self.assertEqual(memory.due_reminders(), [])

        routine_id = memory.save_routine(
            "работа", [{"skill": "set_volume", "params": {"percent": 20}}], "рабочий режим"
        )
        self.assertTrue(routine_id)
        self.assertEqual(memory.find_routine("рабочий режим")["name"], "работа")

    def test_skill_parameter_validation(self):
        self.assertEqual(skills.validate_params("set_volume", {"percent": 150})["percent"], 100)
        with self.assertRaises(ValueError):
            skills.validate_params("open_app", {})


if __name__ == "__main__":
    unittest.main()
