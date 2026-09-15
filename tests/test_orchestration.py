"""Regression coverage for pure plans, observed results and conversation context.

All action and network boundaries are replaced. These tests never control Windows.
"""
import json
from contextlib import nullcontext
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core import actions, ai, memory, router, skills
from core.session import Session


class Voice:
    last_error = ""

    def __init__(self):
        self.said = []

    def say(self, text):
        self.said.append(text)


class UI:
    def __init__(self):
        self.messages = []
        self.state = {}

    def set(self, **fields):
        self.state.update(fields)

    def add_message(self, role, text):
        self.messages.append((role, text))


class ParserTests(unittest.TestCase):
    def test_planning_has_no_side_effects(self):
        with mock.patch.object(actions, "open_app") as app, mock.patch.object(actions, "type_text") as typing:
            result = router.route("открой блокнот и напечатай Привет, Мир!", execute=False)
        app.assert_not_called()
        typing.assert_not_called()
        self.assertEqual(result.steps, [{"skill": "open_app", "params": {"name": "блокнот"}},
                                        {"skill": "type_text", "params": {"text": "Привет, Мир!"}}])

    def test_focus_syntax_strips_only_optional_window_word(self):
        result = router.route("переключись на окно Jarvis Automation Test, затем напечатай Hello, World! Привет, мир 🙂", execute=False)
        self.assertEqual(result.steps, [{"skill": "focus_window", "params": {"title": "Jarvis Automation Test"}},
                                        {"skill": "type_text", "params": {"text": "Hello, World! Привет, мир 🙂"}}])
        self.assertEqual(router.parse("переключись на Окнообразный редактор").steps[0]["params"]["title"], "Окнообразный редактор")
        self.assertEqual(router.parse("покажи окно My Window").steps[0]["params"]["title"], "My Window")

    def test_notes_do_not_change_volume(self):
        result = router.parse("добавь заметку Купить молоко, это мне важно!")
        self.assertEqual(result.steps[0], {"skill": "add_note", "params": {"text": "Купить молоко, это мне важно!"}})

    def test_question_and_negation_do_not_execute(self):
        for text in ("расскажи как изменить яркость", "не выключи звук", "не открывай блокнот", "как добавить заметку", "яркость влияет на глаза"):
            with self.subTest(text=text):
                self.assertFalse(router.parse(text).handled)

    def test_original_typing_payload_is_opaque(self):
        text = "Hello, World! Это мне важно; и открой кавычки."
        result = router.parse("напечатай " + text)
        self.assertEqual(result.steps, [{"skill": "type_text", "params": {"text": text}}])

    def test_partial_plan_never_executes_known_prefix(self):
        result = router.parse("открой блокнот и найди ответы внутри моей головы")
        # Search is an explicit supported web search; arbitrary following command
        # is instead rejected as a whole before executing its known first step.
        self.assertEqual(len(result.steps), 2)
        self.assertFalse(router.parse("открой блокнот и приготовь кофе").handled)

    def test_urls_keep_path_and_query(self):
        self.assertEqual(router.parse("открой https://example.com/Path?q=Hello").steps[0]["params"]["url"],
                         "https://example.com/Path?q=Hello")

    def test_bound_on_local_plan(self):
        result = router.parse(" и ".join(["открой блокнот"] * 9))
        self.assertEqual(result.steps, [])
        self.assertIn("Слишком много", result.reply)


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(memory, "PATH", Path(folder.name) / "learned.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ui, self.voice = UI(), Voice()
        self.stopped = False
        self.session = Session(object(), self.voice, self.ui, log=lambda *args: None,
                               cancel_flag=lambda: self.stopped)
        self.session.wake_up("test")
        self.executed = []

        def execute(name, params):
            self.executed.append((name, params))
            return SimpleNamespace(ok=True, message="Результат " + name)

        patcher = mock.patch.object(skills, "execute", side_effect=execute, create=True)
        self.execute = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(ai, "enabled", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_order_and_exact_payload(self):
        self.session.handle("открой блокнот и напечатай Привет, Мир!")
        self.assertEqual(self.executed, [("open_app", {"name": "блокнот"}), ("type_text", {"text": "Привет, Мир!"})])
        self.assertEqual([role for role, _ in self.ui.messages].count("user"), 1)
        self.assertEqual(self.voice.said, ["Результат open_app", "Результат type_text"])

    def test_first_failure_stops_rest_of_plan(self):
        self.execute.side_effect = lambda *args: SimpleNamespace(ok=False, message="Программа не найдена")
        self.session.handle("открой блокнот и напечатай Опасный ввод")
        self.execute.assert_called_once_with("open_app", {"name": "блокнот"})
        self.assertEqual(self.voice.said, ["Программа не найдена"])
        self.assertEqual(memory.count(), 0)

    def test_disabled_step_prevents_whole_plan(self):
        memory.set_permission("type_text", enabled=False)
        self.session.handle("открой блокнот и напечатай Привет")
        self.execute.assert_not_called()
        self.assertIn("отключено", self.voice.said[-1])

    def test_confirmation_resumes_remaining_steps(self):
        self.session.handle("закрой блокнот и открой калькулятор")
        self.execute.assert_not_called()
        self.session.handle("да")
        self.assertEqual([name for name, _ in self.executed], ["close_app", "open_app"])
        self.assertIsNone(self.session.pending)

    def test_confirmation_restores_original_desktop_target(self):
        target = {"hwnd": 123, "cursor": [10, 20]}
        with mock.patch.object(actions, "capture_target", return_value=target, create=True), \
             mock.patch.object(actions, "restore_target", return_value=nullcontext(), create=True) as restore:
            self.session.handle("закрой блокнот и открой калькулятор")
            self.assertEqual(self.session.pending["target"], target)
            self.session.handle("да")
        restore.assert_called_once_with(target)
        self.assertEqual([name for name, _ in self.executed], ["close_app", "open_app"])

    def test_confirmation_does_not_accept_prefix_and_cancel_clears_plan(self):
        self.session.handle("закрой блокнот и открой калькулятор")
        self.session.handle("давай сначала подумаем")
        self.execute.assert_not_called()
        self.assertIsNotNone(self.session.pending)
        self.session.handle("нет")
        self.execute.assert_not_called()
        self.assertEqual(self.session._remaining_steps, [])

    def test_cancellation_between_steps(self):
        def first(*args):
            self.stopped = True
            return SimpleNamespace(ok=True, message="Открыт")
        self.execute.side_effect = first
        self.session.handle("открой блокнот и напечатай Привет")
        self.assertEqual(self.execute.call_count, 1)
        self.assertEqual(self.session._remaining_steps, [])

    def test_unvalidated_late_step_prevents_first_action(self):
        self.session._start_plan([{"skill": "open_app", "params": {"name": "блокнот"}},
                                  {"skill": "invented", "params": {}}])
        self.execute.assert_not_called()

    def test_ai_success_prediction_never_overrides_failure(self):
        self.execute.return_value = SimpleNamespace(ok=False, message="Нет доступа")
        self.execute.side_effect = None
        with mock.patch.object(ai, "resolve", return_value=ai.Resolution([{"skill": "open_app", "params": {"name": "telegram"}}], "Уже всё открыл!")):
            self.session._ask_ai("покажи мой мессенджер")
        self.assertEqual(self.voice.said, ["Нет доступа"])
        self.assertEqual(memory.count(), 0)

    def test_ai_receives_conversation_across_voice_idle(self):
        with mock.patch.object(ai, "enabled", return_value=True), mock.patch.object(ai, "chain", return_value=["test"]), \
             mock.patch.object(ai, "resolve", side_effect=[ai.Resolution([], "Венера — вторая планета."), ai.Resolution([], "Она горячая.")]) as resolve:
            self.session.handle("расскажи о Венере")
            self.session.go_to_sleep("silence")
            self.session.wake_up("button")
            self.session.handle("а какая там температура")
        self.assertEqual(resolve.call_args.kwargs["history"], [("user", "расскажи о Венере"), ("assistant", "Венера — вторая планета.")])

    def test_ai_cancellation_discards_plan(self):
        def answer(*args, **kwargs):
            self.stopped = True
            return ai.Resolution([{"skill": "open_app", "params": {"name": "notepad"}}])
        with mock.patch.object(ai, "resolve", side_effect=answer):
            self.session._ask_ai("запрос")
        self.execute.assert_not_called()

    def test_clipboard_changed_during_ai_is_not_overwritten(self):
        with mock.patch.object(actions, "clipboard_get", side_effect=["original", "new user value"]), \
             mock.patch.object(actions, "clipboard_set") as write, \
             mock.patch.object(ai, "enabled", return_value=True), mock.patch.object(ai, "chain", return_value=["test"]), \
             mock.patch.object(ai, "transform_text", return_value="improved"):
            self.session._transform_clipboard("улучши")
        write.assert_not_called()
        self.assertIn("Буфер изменился", self.voice.said[-1])

    def test_dictation_uses_shared_skill_execution_boundary(self):
        self.session.dictation = True
        self.session.handle("Привет запятая мир")
        self.execute.assert_called_once_with("type_text", {"text": "Привет, мир "})

    def test_fast_transcript_cannot_execute(self):
        self.assertFalse(self.session._try_fast_path("выключи компьютер"))
        self.execute.assert_not_called()
        self.assertIsNone(self.session.pending)

    def test_voice_result_is_discarded_when_stop_arrives_during_transcription(self):
        ears = mock.Mock()
        ears.record.return_value = object()
        stop = [False]
        def transcribe(audio):
            stop[0] = True
            return "выключи компьютер"
        ears.transcribe_active.side_effect = transcribe
        self.session.ears = ears
        with mock.patch.object(self.session, "handle") as handle:
            self.session.run(lambda: stop[0])
        handle.assert_not_called()
        self.execute.assert_not_called()

    def test_slow_recognition_keeps_authorized_voice_command(self):
        self.session.require_wake = True
        ears = mock.Mock()
        ears.last_stt = "local"
        ears.record.return_value = object()
        stop = [False]
        def transcribe(audio):
            self.session.active_until = 0  # Capture/recognition outlasts 8s.
            return "который час"
        ears.transcribe_active.side_effect = transcribe
        self.session.ears = ears
        original = self.session.handle
        def handle(text):
            original(text)
            stop[0] = True
        with mock.patch.object(self.session, "handle", side_effect=handle):
            self.session.run(lambda: stop[0])
        self.execute.assert_called_once_with("time", {})

    def test_pause_during_record_discards_audio(self):
        ears = mock.Mock()
        paused = [False]
        def record(**kwargs):
            paused[0] = True
            return object()
        ears.record.side_effect = record
        self.session.ears = ears
        with mock.patch.object(self.session, "handle") as handle:
            self.session.run(lambda: False, lambda: paused[0])
        ears.transcribe_active.assert_not_called()
        handle.assert_not_called()

    def test_exact_memory_does_not_replay_similar_action(self):
        memory.remember("покажи любимую программу", "open_app", {"name": "telegram"})
        self.session.handle("покажи любимую программу завтра")
        self.execute.assert_not_called()

    def test_saved_typing_never_overrides_current_payload(self):
        memory.remember("напечатай Hello!", "type_text", {"text": "hello"})
        self.session.handle("напечатай Hello!")
        self.execute.assert_called_once_with("type_text", {"text": "Hello!"})

    def test_explicit_new_conversation_resets_context(self):
        self.session.history = [("user", "Старый контекст"), ("assistant", "Ответ")]
        self.session.handle("новый разговор")
        self.assertFalse(any("Старый" in text for _, text in self.session.history))


class AIProtocolTests(unittest.TestCase):
    def test_structured_plan_and_history(self):
        payload = {"steps": [{"skill": "open_app", "params": {"name": "блокнот"}},
                             {"skill": "type_text", "params": {"text": "Hello!"}}], "say": "Открою и напечатаю."}
        with mock.patch.object(ai, "_try_chain", return_value=json.dumps(payload)) as chain:
            result = ai.resolve("сделай это", history=[("user", "текст Hello!")])
        self.assertEqual(len(result.steps), 2)
        self.assertEqual(chain.call_args.kwargs["history"], [("user", "текст Hello!")])

    def test_invalid_actions_rejected(self):
        for payload in ({"steps": [{"skill": "shell", "params": {}}]},
                        {"steps": [{"skill": "open_app", "params": {}}]},
                        {"steps": [{"skill": "time"}] * 9}):
            with mock.patch.object(ai, "_try_chain", return_value=json.dumps(payload)):
                with self.assertRaises(ai.AiError):
                    ai.resolve("запрос")

    def test_gemini_combines_text_and_drops_thoughts(self):
        payload = {"candidates": [{"content": {"parts": [{"text": "private", "thought": True},
                    {"text": "Hello"}, {"text": " world"}]}}]}
        with mock.patch.object(ai, "_post", return_value=payload):
            self.assertEqual(ai._call("gemini", "s", "u"), "Hello world")


if __name__ == "__main__":
    unittest.main()
