"""Проверка лесенки решений: правила -> память -> внешний мозг.

Главное, что здесь доказывается: новая фраза обращается к мозгу ровно один раз.
Второй раз та же фраза исполняется из памяти, без сети.
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

os.environ["JARVIS_SERVER_BRAIN"] = "0"  # tests never talk to the real server brain
from core import ai, memory, server_link, session as session_module, skills


class FakeVoice:
    def __init__(self):
        self.said = []

    def say(self, text, wait=True):
        self.said.append(text)


class FakeEars:
    noise_floor = 0.004

    def release_idle(self):
        return False


class LearningTests(unittest.TestCase):
    def setUp(self):
        # память — во временный файл, чтобы не трогать рабочий learned.json
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(memory, "PATH", Path(self.tmp.name) / "learned.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        memory._cache = None
        self.addCleanup(setattr, memory, "_cache", None)

        self.performed = []
        run_patch = mock.patch.object(
            skills, "execute",
            side_effect=lambda name, params=None: self.performed.append((name, params)) or SimpleNamespace(ok=True, message="Сделал."))
        run_patch.start()
        self.addCleanup(run_patch.stop)

        self.voice = FakeVoice()
        self.session = session_module.Session(FakeEars(), self.voice, ui=None,
                                              log=lambda *a: None)

    def test_rule_does_not_touch_ai(self):
        with mock.patch.object(ai, "resolve") as resolve:
            with mock.patch.object(session_module.actions if hasattr(session_module, "actions")
                                   else skills, "run", create=True):
                self.session.handle("который час")
        resolve.assert_not_called()
        self.assertEqual(self.session.stats["rules"], 1)

    def test_new_phrase_asks_ai_once_then_remembers(self):
        with mock.patch.object(ai, "enabled", return_value=True), \
             mock.patch.object(ai, "chain", return_value=["gemini"]), \
             mock.patch.object(ai, "last_provider", return_value="gemini"), \
             mock.patch.object(ai, "resolve",
                               return_value=("open_app", {"name": "telegram"},
                                             "Открываю телеграм.")) as resolve:
            self.session.handle("покажи-ка мне мессенджер телеграм")
            self.assertEqual(resolve.call_count, 1, "первый раз спрашиваем мозг")
            self.assertEqual(self.session.stats["ai"], 1)

            # второй раз — та же фраза, мозг больше не нужен
            self.session.handle("покажи-ка мне мессенджер телеграм")
            self.assertEqual(resolve.call_count, 1, "второй раз мозг дёргать нельзя")
            self.assertEqual(self.session.stats["learned"], 1)

        self.assertEqual(self.performed,
                         [("open_app", {"name": "telegram"}),
                          ("open_app", {"name": "telegram"})])
        self.assertEqual(memory.count(), 1)

    def test_similar_wording_uses_memory(self):
        memory.remember("открой мой любимый мессенджер", "open_app", {"name": "telegram"})
        with mock.patch.object(ai, "enabled", return_value=True), \
             mock.patch.object(ai, "chain", return_value=["gemini"]), \
             mock.patch.object(ai, "resolve") as resolve:
            self.session.handle("открой мой любимый мессенджер!")
        resolve.assert_not_called()
        self.assertEqual(self.session.stats["learned"], 1)

    def test_chat_is_not_remembered(self):
        with mock.patch.object(ai, "enabled", return_value=True), \
             mock.patch.object(ai, "chain", return_value=["gemini"]), \
             mock.patch.object(ai, "last_provider", return_value="gemini"), \
             mock.patch.object(ai, "resolve", return_value=(None, {}, "Думаю, да.")):
            self.session.handle("как считаешь, стоит ли учить питон")
        self.assertEqual(memory.count(), 0, "разговор запоминать нельзя")
        self.assertIn("Думаю, да.", self.voice.said)

    def test_server_brain_answers_what_local_rules_cannot(self):
        with mock.patch.object(server_link, "enabled", return_value=True), \
             mock.patch.object(server_link, "ask", return_value=("Осень пришла.", 0.01)) as ask, \
             mock.patch.object(ai, "resolve") as resolve:
            self.session.handle("сочини стихотворение про осень")
        ask.assert_called_once_with("сочини стихотворение про осень")
        resolve.assert_not_called()
        self.assertEqual(self.session.stats["server"], 1)
        self.assertIn("Осень пришла.", self.voice.said)

    def test_local_rules_still_skip_the_server(self):
        with mock.patch.object(server_link, "enabled", return_value=True), \
             mock.patch.object(server_link, "ask") as ask:
            self.session.handle("который час")
        ask.assert_not_called()
        self.assertEqual(self.session.stats["rules"], 1)

    def test_server_down_falls_back_to_old_ai(self):
        down = server_link.ServerUnavailable("connection refused")
        with mock.patch.object(server_link, "enabled", return_value=True), \
             mock.patch.object(server_link, "ask", side_effect=down), \
             mock.patch.object(ai, "enabled", return_value=True), \
             mock.patch.object(ai, "chain", return_value=["gemini"]), \
             mock.patch.object(ai, "last_provider", return_value="gemini"), \
             mock.patch.object(ai, "resolve", return_value=(None, {}, "Запасной ответ.")):
            self.session.handle("сочини стихотворение про осень")
        self.assertIn("Запасной ответ.", self.voice.said)

    def test_server_down_without_ai_says_only_local_commands_work(self):
        with mock.patch.object(server_link, "enabled", return_value=True), \
             mock.patch.object(server_link, "ask", side_effect=server_link.ServerUnavailable("x")), \
             mock.patch.object(ai, "enabled", return_value=False):
            self.session.handle("сочини стихотворение про осень")
        self.assertTrue(any("Сервер недоступен" in phrase for phrase in self.voice.said))

    def test_without_ai_says_so(self):
        with mock.patch.object(ai, "enabled", return_value=False):
            self.session.handle("сочини стихотворение про осень")
        self.assertEqual(self.session.stats["unknown"], 1)
        self.assertTrue(any("Не понял" in phrase for phrase in self.voice.said))

    def test_dangerous_learned_skill_asks_first(self):
        memory.remember("вырубай машину", "shutdown", {})
        self.session.handle("вырубай машину")
        self.assertEqual(self.performed, [], "опасное не выполняется без подтверждения")
        self.assertTrue(any("Точно" in phrase for phrase in self.voice.said))
        self.session.handle("да")
        self.assertEqual(self.performed, [("shutdown", {})])


class ProviderChainTests(unittest.TestCase):
    def test_order_respects_settings_and_keys(self):
        with mock.patch.object(ai.cfg, "AI_MODE", "auto"), \
             mock.patch.object(ai.cfg, "AI_ORDER", "claude,gemini"), \
             mock.patch.object(ai.cfg, "CLAUDE_KEY", ""), \
             mock.patch.object(ai.cfg, "GEMINI_KEY", "abc"), \
             mock.patch.object(ai.cfg, "GROQ_KEY", ""):
            self.assertEqual(ai.chain(), ["gemini"])

        with mock.patch.object(ai.cfg, "AI_MODE", "auto"), \
             mock.patch.object(ai.cfg, "AI_ORDER", "claude,gemini"), \
             mock.patch.object(ai.cfg, "CLAUDE_KEY", "k"), \
             mock.patch.object(ai.cfg, "GEMINI_KEY", "abc"):
            self.assertEqual(ai.chain(), ["claude", "gemini"])

    def test_falls_through_to_next_provider(self):
        calls = []

        def fake_call(provider, system, user, history=()):
            calls.append(provider)
            if provider == "claude":
                raise ai.AiError("лимит запросов исчерпан")
            return "ответ"

        with mock.patch.object(ai.cfg, "AI_MODE", "auto"), \
             mock.patch.object(ai.cfg, "AI_ORDER", "claude,gemini"), \
             mock.patch.object(ai.cfg, "CLAUDE_KEY", "k"), \
             mock.patch.object(ai.cfg, "GEMINI_KEY", "g"), \
             mock.patch.object(ai, "_call", side_effect=fake_call):
            self.assertEqual(ai._try_chain("s", "u"), "ответ")
        self.assertEqual(calls, ["claude", "gemini"], "должен переключиться на запасной")

    def test_all_failed_reports_both(self):
        with mock.patch.object(ai.cfg, "AI_MODE", "auto"), \
             mock.patch.object(ai.cfg, "AI_ORDER", "claude,gemini"), \
             mock.patch.object(ai.cfg, "CLAUDE_KEY", "k"), \
             mock.patch.object(ai.cfg, "GEMINI_KEY", "g"), \
             mock.patch.object(ai, "_call", side_effect=ai.AiError("ключ не принят")):
            with self.assertRaises(ai.AiError) as caught:
                ai._try_chain("s", "u")
        self.assertIn("claude", str(caught.exception))
        self.assertIn("gemini", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
