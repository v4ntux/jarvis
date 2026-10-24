"""Watchers and server jobs without Telegram, Anthropic or a real laptop.

Run from server/: python -m unittest discover -s tests
"""
import asyncio
import datetime as dt
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from jarvis import shell, watchers
from jarvis.config import TZ
from jarvis.memory import Memory


class FakeHub:
    def __init__(self, outputs=(), online=True):
        self.outputs = list(outputs)
        self.online = online
        self.since = time.time()
        self.calls = []

    async def call(self, tool, args, timeout=60):
        self.calls.append((tool, args))
        return {"text": self.outputs.pop(0)}


def at(year, month, day, hour, minute):
    return dt.datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp()


class NormalizeTests(unittest.TestCase):
    def test_laptop_check_gets_defaults(self):
        spec = watchers.normalize({"what": "загрузка", "source": "laptop", "command": "1",
                                   "condition": "equals", "value": 0})
        self.assertEqual((spec["value"], spec["every_min"], spec["once"]), ("0", 5, True))

    def test_rejects_broken_specs(self):
        bad = [
            {"what": "x", "source": "mail"},
            {"what": "", "source": "laptop", "command": "1"},
            {"what": "x", "source": "laptop"},
            {"what": "x", "source": "url", "url": "https://a", "condition": "below"},
            {"what": "x", "source": "url", "url": "https://a", "condition": "below", "value": "много"},
            {"what": "x", "source": "time", "at": "25:00"},
            {"what": "x", "source": "time", "at": "08:00", "days": "someday"},
        ]
        for args in bad:
            with self.subTest(args=args), self.assertRaises(ValueError):
                watchers.normalize(args)

    def test_offline_repeats_online_fires_once_by_default(self):
        self.assertFalse(watchers.normalize({"what": "x", "source": "laptop_offline"})["once"])
        self.assertTrue(watchers.normalize({"what": "x", "source": "laptop_online"})["once"])


class ScheduleTests(unittest.TestCase):
    def test_weekdays_skip_the_weekend(self):
        friday_evening = at(2026, 10, 23, 20, 0)
        self.assertEqual(watchers.next_time("08:00", "weekdays", friday_evening), at(2026, 10, 26, 8, 0))

    def test_same_day_when_still_ahead_and_never_now(self):
        morning = at(2026, 10, 24, 7, 0)
        self.assertEqual(watchers.next_time("08:00", "daily", morning), at(2026, 10, 24, 8, 0))
        self.assertEqual(watchers.next_time("08:00", "daily", at(2026, 10, 24, 8, 0)), at(2026, 10, 25, 8, 0))

    def test_day_list(self):
        saturday = at(2026, 10, 24, 12, 0)
        self.assertEqual(watchers.next_time("09:30", "mon,wed", saturday), at(2026, 10, 26, 9, 30))


class EvaluateTests(unittest.TestCase):
    def spec(self, condition, value=""):
        return {"condition": condition, "value": value}

    def test_changed_needs_a_baseline_and_fires_on_every_change(self):
        state = {}
        spec = self.spec("changed")
        self.assertEqual([watchers.evaluate(spec, out, state) for out in ("a", "a", "b", "c", "c")],
                         [False, False, True, True, False])

    def test_threshold_fires_on_crossing_only(self):
        state = {}
        spec = self.spec("below", "20")
        results = [watchers.evaluate(spec, out, state) for out in ("35", "19", "15", "40", "18,5")]
        self.assertEqual(results, [False, True, False, False, True])

    def test_contains_ignores_case_and_missing_inverts(self):
        self.assertTrue(watchers.evaluate(self.spec("contains", "в наличии"), "Товар В НАЛИЧИИ", {}))
        self.assertTrue(watchers.evaluate(self.spec("missing", "нет"), "есть", {}))

    def test_no_number_is_an_error_not_a_fire(self):
        with self.assertRaises(ValueError):
            watchers.evaluate(self.spec("above", "5"), "пусто", {})


class WatcherLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.fired = []

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def fire(self, spec, detail):
        self.fired.append((spec["what"], detail))

    def add(self, **args):
        spec = watchers.normalize(args)
        return self.memory.add_watcher(spec, watchers.first_check(spec, time.time()))

    async def check_all(self, w):
        for wid, spec, state, _ in self.memory.watchers():
            await w._check(wid, spec, state)
        await asyncio.gather(*w.running)

    async def test_download_watcher_fires_once_then_disappears(self):
        hub = FakeHub(["2", "1", "0"])
        w = watchers.Watchers(self.memory, hub, self.fire)
        self.add(what="загрузка", source="laptop", command="count", condition="equals", value="0")
        for _ in range(3):
            await self.check_all(w)
        self.assertEqual(self.fired, [("загрузка", "0")])
        self.assertEqual(self.memory.watchers(), [])
        self.assertEqual(hub.calls[0], ("run_shell", {"command": "count", "timeout": watchers.CHECK_TIMEOUT}))

    async def test_laptop_errors_and_absence_never_fire(self):
        hub = FakeHub(["Ошибка (код 1):\nнет такого"])
        w = watchers.Watchers(self.memory, hub, self.fire)
        wid = self.add(what="x", source="laptop", command="c", condition="missing", value="ok")
        await self.check_all(w)
        hub.online = False
        await self.check_all(w)
        self.assertEqual(self.fired, [])
        [(_, _, state, due)] = self.memory.watchers()
        self.assertIn("не подключён", state["error"])
        self.assertLess(due, time.time() + 120)  # an absent laptop is retried soon

    async def test_offline_alert_waits_for_the_grace_period_and_repeats_per_outage(self):
        hub = FakeHub(online=False)
        w = watchers.Watchers(self.memory, hub, self.fire)
        self.add(what="ноутбук пропал", source="laptop_offline", minutes=10)
        hub.since = time.time() - 60
        await self.check_all(w)
        self.assertEqual(self.fired, [])
        hub.since = time.time() - 700
        await self.check_all(w)
        await self.check_all(w)
        hub.online = True
        await self.check_all(w)
        hub.online, hub.since = False, time.time() - 700
        await self.check_all(w)
        self.assertEqual(len(self.fired), 2)

    async def test_online_watcher_ignores_the_laptop_already_being_there(self):
        hub = FakeHub(online=True)
        w = watchers.Watchers(self.memory, hub, self.fire)
        self.add(what="вернулся", source="laptop_online")
        await self.check_all(w)
        hub.online = False
        await self.check_all(w)
        hub.online = True
        await self.check_all(w)
        self.assertEqual(self.fired, [("вернулся", "")])
        self.assertEqual(self.memory.watchers(), [])

    async def test_schedule_fires_and_moves_to_the_next_day(self):
        w = watchers.Watchers(self.memory, FakeHub(), self.fire)
        wid = self.add(what="бриф", source="time", at="08:00", task="погода и новости")
        await self.check_all(w)
        [(_, _, _, due)] = self.memory.watchers()
        self.assertEqual(self.fired, [("бриф", "")])
        self.assertGreater(due, time.time())
        self.assertLessEqual(due, time.time() + 86400)

    async def test_server_check_runs_without_the_laptop(self):
        hub = FakeHub(online=False)
        w = watchers.Watchers(self.memory, hub, self.fire)
        with patch.object(shell, "WORK", self.tmp.name):
            self.add(what="число", source="server", command="echo 42", condition="above", value="40")
            await self.check_all(w)
        self.assertEqual(self.fired, [("число", "42")])
        self.assertEqual(hub.calls, [])


class ShellTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(shell, "WORK", self.tmp.name),
                        patch.object(shell, "JOBS", os.path.join(self.tmp.name, "jobs"))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    async def test_scripts_do_not_see_the_bot_secrets(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-secret", "HANDS_TOKEN": "t"}):
            out = await shell.run("echo key=${ANTHROPIC_API_KEY:-none} token=${HANDS_TOKEN:-none}; pwd", 10)
        self.assertEqual(out.splitlines(), ["key=none token=none", self.tmp.name])

    async def test_timeout_kills_the_whole_pipeline(self):
        marker = os.path.join(self.tmp.name, "late")
        started = time.monotonic()
        out = await shell.run(f"(sleep 3; touch {marker}) & sleep 30", 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(out.startswith("Ошибка"))
        await asyncio.sleep(3.5)
        self.assertFalse(os.path.exists(marker))

    async def test_background_job_reports_exit_code_and_tail(self):
        finished = asyncio.get_running_loop().create_future()

        async def done(job_id, code, tail):
            finished.set_result((job_id, code, tail))

        job_id = await shell.start_job("echo начал; sleep 0.5; echo конец; exit 3", "тест", done)
        self.assertIn((job_id, "тест"), shell.jobs())
        got = await asyncio.wait_for(finished, 10)
        self.assertEqual(got, (job_id, 3, "начал\nконец"))
        self.assertEqual(shell.jobs(), [])

    async def test_stop_job(self):
        finished = asyncio.get_running_loop().create_future()

        async def done(job_id, code, tail):
            finished.set_result(code)

        job_id = await shell.start_job("sleep 60", "долго", done)
        self.assertTrue(shell.stop_job(job_id))
        self.assertNotEqual(await asyncio.wait_for(finished, 10), 0)
        self.assertFalse(shell.stop_job(job_id))


if __name__ == "__main__":
    unittest.main()
