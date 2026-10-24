"""Learned routines without touching the real desktop: steps, storage and replay with fakes."""
import os
import tempfile
import unittest
from unittest import mock

os.environ["JARVIS_SERVER_BRAIN"] = "0"  # tests never talk to the real server brain
from core import actions, routines

NOTEPAD = {"process": "notepad.exe", "title": "Без имени — Блокнот"}
TELEGRAM = {"process": "telegram.exe", "title": "Telegram"}


def target(name="Файл", role="menuitem", window=NOTEPAD, rel=(0.1, 0.05)):
    return {"window": window, "role": role, "name": name, "rel": list(rel), "at": [0.5, 0.5], "point": list(rel)}


class StepBuilderTests(unittest.TestCase):
    def setUp(self):
        self.b = routines.StepBuilder()

    def test_typing_becomes_one_text_step_and_backspace_edits_it(self):
        for i, ch in enumerate("мамв"):
            self.b.char(1 + i * 0.1, ch, TELEGRAM, False)
        self.b.key(2.0, "backspace", TELEGRAM)
        self.b.char(2.1, "а", TELEGRAM, False)
        self.b.key(2.5, "enter", TELEGRAM)
        self.assertEqual([(s["type"], s.get("text") or s.get("keys")) for s in self.b.steps],
                         [("text", "мама"), ("key", "enter")])

    def test_backspacing_everything_removes_the_step(self):
        self.b.char(1, "x", NOTEPAD, False)
        self.b.key(1.1, "backspace", NOTEPAD)
        self.assertEqual(self.b.steps, [])

    def test_two_quick_clicks_on_the_same_element_are_a_double_click(self):
        self.b.click(1.0, target(), "left")
        self.b.click(1.3, target(), "left")
        self.b.click(3.0, target(), "left")
        self.assertEqual([s["count"] for s in self.b.steps], [2, 1])
        self.assertEqual(self.b.steps[1]["gap"], 1.7)

    def test_password_typing_is_never_kept(self):
        for i, ch in enumerate("hunter2"):
            self.b.char(1 + i * 0.1, ch, TELEGRAM, True)
        self.b.key(2, "backspace", TELEGRAM)
        self.assertEqual(self.b.steps, [{"type": "secret", "window": TELEGRAM, "gap": 0.0}])
        self.assertNotIn("hunter", str(self.b.steps))

    def test_text_in_another_window_starts_a_new_step(self):
        self.b.char(1, "a", NOTEPAD, False)
        self.b.char(2, "b", TELEGRAM, False)
        self.assertEqual([s["text"] for s in self.b.steps], ["a", "b"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(routines, "PATH", os.path.join(self.tmp.name, "routines.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        routines._save({routines._norm(n): {"name": n, "created": 0, "steps": [{"type": "key", "keys": "enter",
                                                                              "window": NOTEPAD, "gap": 0}]}
                        for n in ("Почта", "отчёт в телеграм")})

    def test_find_forgives_case_yo_quotes_and_endings(self):
        self.assertEqual(routines.find("почту")["name"], "Почта")
        self.assertEqual(routines.find("«Отчет в телеграме»")["name"], "отчёт в телеграм")
        self.assertIsNone(routines.find("погода"))

    def test_commands_only_match_saved_routines(self):
        self.assertEqual(routines.match_command("Сделай почту"), "Почта")
        self.assertEqual(routines.match_command("запусти приём отчёт в телеграм!"), "отчёт в телеграм")
        self.assertEqual(routines.match_command("приём почта"), "Почта")
        self.assertIsNone(routines.match_command("сделай скриншот"))
        self.assertIsNone(routines.match_command("почта"))

    def test_forget(self):
        self.assertTrue(routines.forget("почта"))
        self.assertEqual(routines.names(), ["отчёт в телеграм"])
        self.assertFalse(routines.forget("почта"))


class FakeUIA:
    def __init__(self, found=True):
        self.found = found
        self.clicked = []

    def call(self, fn, *args, timeout=15):
        assert fn is routines._click_step
        self.clicked.append(args[0]["name"])
        return self.found


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log = []
        for patcher in (
            mock.patch.object(routines, "PATH", os.path.join(self.tmp.name, "routines.json")),
            mock.patch.object(routines.time, "sleep", lambda s: None),
            mock.patch.object(routines, "FIND_TIMEOUT", 0.05),
            mock.patch.object(routines, "_bring", lambda window, stop: self.log.append(("focus", window["process"])) or 1),
            mock.patch.object(actions, "type_text", lambda text: self.log.append(("type", text))),
            mock.patch.object(actions, "hotkey", lambda keys: self.log.append(("keys", keys))),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def save(self, steps):
        routines._save({"тест": {"name": "тест", "created": 0, "steps": steps}})

    def test_steps_run_in_order_in_the_right_programs(self):
        self.save([{"type": "click", "button": "left", "count": 1, "gap": 0, **target("Поиск", "edit", TELEGRAM)},
                   {"type": "text", "text": "мама", "window": TELEGRAM, "gap": 0.4},
                   {"type": "key", "keys": "enter", "window": TELEGRAM, "gap": 9}])
        uia = FakeUIA()
        self.assertEqual(routines.replay("тест", uia=uia), "Приём «тест» выполнен: 3 шага.")
        self.assertEqual(uia.clicked, ["Поиск"])
        self.assertEqual(self.log, [("focus", "telegram.exe"), ("focus", "telegram.exe"), ("type", "мама"),
                                    ("focus", "telegram.exe"), ("keys", "enter")])

    def test_missing_element_stops_and_says_which_step(self):
        self.save([{"type": "key", "keys": "ctrl+n", "window": NOTEPAD, "gap": 0},
                   {"type": "click", "button": "left", "count": 1, "gap": 0, **target("Сохранить", "button")},
                   {"type": "text", "text": "не дойдёт", "window": NOTEPAD, "gap": 0}])
        out = routines.replay("тест", uia=FakeUIA(found=False))
        self.assertTrue(out.startswith("Ошибка: приём «тест» остановился на шаге 2 из 3"), out)
        self.assertIn("«Сохранить»", out)
        self.assertNotIn(("type", "не дойдёт"), self.log)

    def test_password_step_hands_over_to_the_owner(self):
        self.save([{"type": "secret", "window": TELEGRAM, "gap": 0},
                   {"type": "key", "keys": "enter", "window": TELEGRAM, "gap": 0}])
        out = routines.replay("тест", uia=FakeUIA())
        self.assertIn("пароль", out)
        self.assertEqual(self.log, [])

    def test_stop_flag_interrupts(self):
        self.save([{"type": "key", "keys": "enter", "window": NOTEPAD, "gap": 0}])
        out = routines.replay("тест", stop_flag=lambda: True, uia=FakeUIA())
        self.assertIn("остановлено", out)
        self.assertEqual(self.log, [])

    def test_unknown_routine_lists_known_ones(self):
        self.save([])
        with self.assertRaisesRegex(routines.RoutineError, "Есть: тест"):
            routines.replay("почта", uia=FakeUIA())


class PickTests(unittest.TestCase):
    def test_named_element_takes_the_nearest_look_alike(self):
        step = target("Удалить", "button", rel=(0.9, 0.5))
        far, near = {"rel": [0.1, 0.5]}, {"rel": [0.85, 0.5]}
        self.assertIs(routines._pick([far, near], step), near)

    def test_nameless_element_must_be_close_to_where_it_was(self):
        step = target("", "image", rel=(0.5, 0.5))
        self.assertIsNone(routines._pick([{"rel": [0.9, 0.9]}], step))
        self.assertIsNotNone(routines._pick([{"rel": [0.52, 0.5]}], step))


class DescribeTests(unittest.TestCase):
    def test_summary_is_readable_and_hides_passwords(self):
        text = routines.describe([
            {"type": "click", "button": "left", "count": 2, **target("Документы", "listitem")},
            {"type": "text", "text": "привет", "window": NOTEPAD},
            {"type": "secret", "window": TELEGRAM}])
        self.assertEqual(text.splitlines(), [
            "1. notepad: дважды нажал «Документы» (listitem)",
            "2. notepad: напечатал «привет»",
            "3. telegram: ввод пароля (не записан, при повторе спрошу)"])


if __name__ == "__main__":
    unittest.main()
