"""«сделай почту» runs the learned routine with no Anthropic call; other words go to the model."""
import os
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config
from jarvis.brain import Brain, Session
from jarvis.memory import Memory

ENV = {"OWNER_TG_ID": "1", "TG_BOT_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}


class FakeHub:
    online = True

    def __init__(self, routines):
        self.routines = routines
        self.calls = []

    async def call(self, tool, args, timeout=60):
        self.calls.append((tool, args, timeout))
        return {"text": "Приём «%s» выполнен: 3 шага." % args["name"]}


async def nothing(*args):
    return None


class RoutineShortcutTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, ENV, clear=True):
            cfg = config.load()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.hub = FakeHub(["Почта", "отчёт в телеграм"])
        self.brain = Brain(cfg, self.memory, self.hub)
        self.session = Session(nothing, nothing, nothing)

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def test_routine_runs_without_the_model(self):
        with patch.object(self.brain.client.messages, "create", side_effect=AssertionError("paid call")):
            answer = await self.brain.reply("Сделай почту", self.session)
            spoken = await self.brain.reply("[голосом у ноутбука] запусти приём «Отчет в телеграме»", self.session)
        self.assertEqual(answer, "Приём «Почта» выполнен: 3 шага.")
        self.assertIn("отчёт в телеграм", spoken)
        self.assertEqual([(t, a) for t, a, _ in self.hub.calls],
                         [("run_routine", {"name": "Почта"}), ("run_routine", {"name": "отчёт в телеграм"})])
        self.assertEqual(self.hub.calls[0][2], 600)  # long routines are not cut off at 90 s
        self.assertEqual(self.memory.spent_since(0), 0)

    def test_other_requests_are_not_routines(self):
        for text in ("сделай скриншот", "почта", "сделай почту и отчёт", "открой почту"):
            with self.subTest(text=text):
                self.assertIsNone(self.brain._routine_command(text))

    def test_offline_laptop_or_no_routines_means_no_shortcut(self):
        self.hub.online = False
        self.assertIsNone(self.brain._routine_command("сделай почту"))
        self.hub.online, self.hub.routines = True, []
        self.assertIsNone(self.brain._routine_command("сделай почту"))

    def test_prompt_lists_routines_so_the_model_can_pick_them(self):
        live = self.brain._system()[1]["text"]
        self.assertIn("Выученные приёмы (run_routine, бесплатно): Почта, отчёт в телеграм.", live)


if __name__ == "__main__":
    unittest.main()
