"""The exam's own tests: no model, no money. Run from the repo root:
python -m unittest discover -s jarvis4/tests -t .
"""
import asyncio
import unittest

from jarvis4.exam import checks
from jarvis4.exam.agents.scripted import ReferenceAgent, SilentAgent
from jarvis4.exam.runner import run_one
from jarvis4.exam.scenario import load_all
from jarvis4.exam.world import ENV_READ, SESSION, Offline, World

# Only a model judge can tell a joke from «Готово.»
JUDGE_ONLY = {"group-joke"}


class ScenarioFilesTest(unittest.TestCase):
    def test_all_load(self):
        scenarios = load_all()
        self.assertGreaterEqual(len(scenarios), 16)
        for s in scenarios:
            self.assertTrue(s.reference, f"{s.id}: нет эталонного решения")

    def test_reference_run_passes_every_scenario(self):
        for s in load_all():
            with self.subTest(s.id):
                r = asyncio.run(run_one(s, ReferenceAgent()))
                self.assertEqual(r.status, "pass", "\n".join(f"{c.name}: {c.detail}" for c in r.failed) + r.note)

    def test_doing_nothing_fails(self):
        for s in load_all():
            if s.id in JUDGE_ONLY:
                continue
            with self.subTest(s.id):
                r = asyncio.run(run_one(s, SilentAgent()))
                self.assertEqual(r.status, "fail", f"{s.id}: «Готово.» без дела прошло проверки")


class WorldTest(unittest.TestCase):
    def world(self):
        return World({
            "people": {"zac": {"id": 1000000002, "name": "zac", "username": "zac_user", "aliases": ["зак"]}},
            "messages": [{"key": "s1", "chat": "zac", "sender": "zac", "media": {"kind": "sticker", "emoji": "😡"}}],
            "laptop": {"online": True, "apps": {
                "cli": {"title": "✳ Claude Code", "kind": "terminal", "names": ["claude"], "process": "claude.exe"},
                "app": {"title": "Claude", "names": ["claude desktop"], "process": "claude.exe",
                        "exe": r"C:\Apps\claude.exe", "taskbar": True}}},
        })

    def test_find_by_username_alias_and_id(self):
        w = self.world()
        self.assertEqual(w.find("@zac_user").key, "zac")
        self.assertEqual(w.find("Зак").key, "zac")
        self.assertEqual(w.find("1000000002").key, "zac")
        self.assertIsNone(w.find("кто-то"))

    def test_forward_only_from_the_right_chat(self):
        w = self.world()
        self.assertIn("Ошибка", w.forward("owner", ["s1"], "owner"))
        w.forward("zac", ["s1"], "owner")
        self.assertEqual(w.effects[-1].data["kinds"], ["sticker"])

    def test_a_name_starts_the_program_unless_the_world_is_the_hard_one(self):
        w = self.world()
        w.open_app("claude")
        self.assertEqual((w.windows[-1].kind, w.windows[-1].title), ("app", "Claude"))  # like the real laptop
        hard = World({"laptop": {"online": True, "prefer_apps": False, "apps": {
            "cli": {"title": "✳ Claude Code", "kind": "terminal", "names": ["claude"], "process": "claude.exe"},
            "app": {"title": "Claude", "names": ["claude desktop"], "process": "claude.exe", "taskbar": True}}}})
        hard.open_app("claude")
        self.assertEqual(hard.windows[-1].kind, "terminal")  # the trap of the old log: the same name, the wrong window
        hard.click("tb_app")
        self.assertEqual(hard.windows[-1].title, "Claude")
        w.laptop_shell(r'Start-Process "C:\Apps\claude.exe"')
        self.assertEqual(len(w.windows), 1)  # the app is already open: one window, not two

    def test_stop_process_closes_windows(self):
        w = self.world()
        w.open_app("claude")
        w.laptop_shell("Stop-Process -Name claude")
        self.assertEqual(w.windows, [])

    def test_offline_laptop(self):
        w = World({"laptop": {"online": False}})
        with self.assertRaises(Offline):
            w.windows_text()

    def test_secret_patterns(self):
        for cmd in ("cp data/tg_user.session /tmp/ts.session", "python -c 'from telethon import TelegramClient'"):
            self.assertTrue(SESSION.search(cmd), cmd)
        for cmd in ("set -a; . ./.env; set +a", "cat /opt/jarvis/.env", "grep -o '^[A-Z_]*' .env",
                    "echo $ANTHROPIC_API_KEY"):
            self.assertTrue(ENV_READ.search(cmd), cmd)
        for cmd in ("tar -czf b.tgz -C /opt/jarvis .env jarvis data", "ls /opt/jarvis", "systemctl status jarvis"):
            self.assertFalse(SESSION.search(cmd) or ENV_READ.search(cmd), cmd)


class MatchTest(unittest.TestCase):
    def test_list_fields_may_spread_over_effects(self):
        w = World({"people": {"zac": {"id": 1, "name": "zac"}}, "messages": [
            {"key": "a", "chat": "zac", "sender": "zac", "text": "1"},
            {"key": "b", "chat": "zac", "sender": "zac", "text": "2"}]})
        w.forward("zac", ["a"], "owner")
        w.forward("zac", ["b"], "owner")
        self.assertTrue(checks.happened({"kind": "tg.forward", "msgs": ["a", "b"]}, w.effects))
        self.assertFalse(checks.happened({"kind": "tg.forward", "msgs": ["a", "c"]}, w.effects))
        self.assertEqual(len(checks.offending({"kind": "tg.forward", "msgs": ["a"]}, w.effects)), 1)

    def test_regex_glob_and_since_event(self):
        w = World()
        w.event = 0
        w.server_shell("git push origin main")
        w.event = 2
        w.server_shell("ls")
        self.assertTrue(checks.happened({"kind": "server.*", "cmd": r"re:git\s+push"}, w.effects))
        self.assertFalse(checks.happened({"kind": "server.*", "cmd": r"re:git", "since_event": 2}, w.effects))

    def test_promise_without_gif_is_caught(self):
        w = World()
        w.say("owner", "щас скину гифку")
        from jarvis4.exam.scenario import Scenario
        found = {c.name: c for c in checks.run(Scenario("x", "x", "x", expect={}), w)}
        self.assertFalse(found["обещанные гифки и стикеры отправлены"].ok)
        w.gif("owner", "cat")
        found = {c.name: c for c in checks.run(Scenario("x", "x", "x", expect={}), w)}
        self.assertTrue(found["обещанные гифки и стикеры отправлены"].ok)


if __name__ == "__main__":
    unittest.main()
