"""The parts that wire the agent to the real world, with fakes in place of Telegram, the laptop and the phone."""
import asyncio
import base64
import functools
import json
import time
import unittest

from jarvis4 import alerts as alerts_mod
from jarvis4.app import App, CaptureOut
from jarvis4.config import Config
from jarvis4.nodes.hub import HandsOffline
from jarvis4.nodes.laptop import LaptopNode
from jarvis4.ports import CallResult, Offline
from jarvis4.store import Store
from jarvis4.tg.callapi import CallApi
from jarvis4.tg.calls import LiveTalk, Persona
from jarvis4.types import Interaction

OWNER_ID = 1000000001


def run(coro):
    return asyncio.run(coro)


class FakeOut:
    def __init__(self):
        self.said, self.asked, self.files = [], [], []

    async def say(self, text, reply_ref=""):
        self.said.append(text)
        return f"fake/{len(self.said)}"

    async def ask(self, question):
        self.asked.append(question)
        return True

    async def progress(self, line):
        pass

    async def send_file(self, name, data):
        self.files.append((name, len(data)))

    async def typing(self):
        pass

    async def finish(self):
        pass


class FakeHub:
    def __init__(self, replies=None, online=True):
        self.online, self.calls, self.replies = online, [], replies or {}

    async def call(self, tool, args, timeout=60):
        self.calls.append((tool, args))
        if not self.online:
            raise HandsOffline("Ноутбук не подключён.")
        reply = self.replies.get(tool, {"text": "Готово."})
        return reply(args) if callable(reply) else reply


class LaptopNodeTest(unittest.TestCase):
    def test_windows_come_with_their_kind(self):
        windows = [{"title": "Claude", "process": "claude.exe", "kind": "app"},
                   {"title": "✳ Claude Code", "process": "WindowsTerminal.exe", "kind": "terminal"}]
        node = LaptopNode(FakeHub({"windows_info": {"text": json.dumps(windows)}}))
        found = run(node.windows())
        self.assertEqual([(w.title, w.kind) for w in found], [("Claude", "app"), ("✳ Claude Code", "terminal")])

    def test_an_old_hands_without_windows_info_still_lists_titles(self):
        node = LaptopNode(FakeHub({"windows_info": {"text": "Ошибка: инструмент windows_info запрещён на ноутбуке."},
                                   "list_windows": {"text": "Видео — проводник\nClaude"}}))
        found = run(node.windows())
        self.assertEqual([w.title for w in found], ["Видео — проводник", "Claude"])

    def test_look_remembers_the_snapshot_for_the_next_click(self):
        hub = FakeHub({"look": {"text": "snapshot_id: abc123\nокно: Claude\ne1 button «Send»"},
                       "windows_info": {"text": "[]"}})
        node = LaptopNode(hub)
        run(node.look())
        run(node.click("e1"))
        self.assertEqual(hub.calls[-1], ("ui_action", {"action": "click_element", "params": {"element_id": "e1"},
                                                       "snapshot_id": "abc123"}))

    def test_offline_laptop_raises_offline(self):
        node = LaptopNode(FakeHub(online=False))
        with self.assertRaises(Offline):
            run(node.shell("dir"))

    def test_screen_do_returns_the_picture(self):
        png = base64.b64encode(b"\x89PNG....").decode()
        hub = FakeHub({"screen_do": lambda a: {"text": "Скриншот", "image": png} if a["name"] == "screenshot"
                       else {"text": "OK"}})
        node = LaptopNode(hub)
        text, image = run(node.screen_do("screenshot", {}))
        self.assertEqual(image, b"\x89PNG....")
        text, image = run(node.screen_do("left_click", {"coordinate": [10, 20]}))
        self.assertEqual((text, image), ("OK", None))
        self.assertEqual(hub.calls[-1][1], {"name": "left_click", "coordinate": [10, 20]})

    def test_media_asks_for_the_checked_version(self):
        hub = FakeHub({"media_checked": {"text": "Готово. Звук идёт: brave.exe (уровень 0.30)"}})
        self.assertIn("Звук идёт", run(LaptopNode(hub).media("play_pause")))
        self.assertEqual(hub.calls[0][0], "media_checked")


def make_app() -> App:
    cfg = Config(owner_id=OWNER_ID, db_path=":memory:", anthropic_key="", gemini_key="", hands_token="t")
    app = App(cfg, Store(":memory:"))
    out = FakeOut()
    app.core.outs = lambda chat: out
    app.out = out
    return app


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def say(self, text):
        return run(self.app.commands.run(text))

    def test_unknown_slash_is_not_a_command(self):
        self.assertIsNone(self.say("/нет-такой"))
        self.assertIsNone(self.say("привет"))

    def test_cost_shows_the_day(self):
        self.app.store.log_usage("claude-sonnet-5-5", 1000, 100, 0.123, "brain")
        text = self.say("/cost")
        self.assertIn("0.123", text)
        self.assertIn("Лимит $2.00", text)

    def test_reset_hides_the_old_talk_but_keeps_facts(self):
        store = self.app.store
        store.remember("Ван живёт в Москве")
        store.add_msg("owner/1", "owner", "owner", "старый разговор", ts=time.time() - 5)
        self.say("/reset")
        self.assertEqual(store.recent("owner", 10), [])
        self.assertEqual(len(store.facts()), 1)

    def test_lessons_can_be_forgotten(self):
        lesson = self.app.store.add_lesson("Просили позвонить — звони, не пиши", "звонок")
        self.assertIn("Просили позвонить", self.say("/lessons"))
        self.assertIn("забыл", self.say(f"/unlesson {lesson}"))
        self.assertIn("пока нет", self.say("/lessons"))

    def test_status_says_what_is_missing(self):
        text = self.say("/status")
        self.assertIn("Ноутбук: не подключён", text)
        self.assertIn("Свой Telegram: НЕ подключён", text)

    def test_group_modes(self):
        self.app.store.chat_seen("g1", "group", "Пацаны", 1)
        self.assertIn("Пацаны", self.say("/groups"))
        self.assertIn("тихо", self.say("/group пацаны тихо"))
        self.assertEqual(self.app.store.chat("g1")[2], "quiet")

    def test_watchers_listing_and_removal(self):
        self.assertIn("наблюдателей нет", self.say("/watchers"))
        wid = self.app.store.add_watcher({"source": "laptop_online", "what": "ноут", "notify": "", "task": "",
                                          "urgent": False, "auto": False, "by": "owner", "once": True}, 0)
        self.assertIn(f"#{wid}", self.say("/watchers"))
        self.assertIn("удалил", self.say(f"/unwatch {wid}"))

    def test_a_command_in_the_owners_chat_never_reaches_the_model(self):
        from jarvis4.core import Incoming
        run(self.app.core.on_message(Incoming(ref="owner/9", chat="owner", sender="owner", text="/memory")))
        self.assertTrue(self.app.out.said and "ничего не запомнил" in self.app.out.said[-1])
        self.assertFalse(self.app.core.queue)


class LimitTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.budget = self.app.core.budget

    def say(self, text):
        return run(self.app.commands.run(text))

    def test_over_the_limit_the_owner_is_refused_until_he_resets(self):
        from jarvis4.budget import OutOfMoney, Tier
        self.app.store.log_usage("claude-sonnet-5-5", 1, 1, 2.5, "legacy")  # what the old bot had spent today
        with self.assertRaises(OutOfMoney) as caught:
            self.budget.pick(Tier.MOUTH)
        self.assertIn("/limit reset", caught.exception.text)  # the refusal says how to get out of it
        self.assertIn("сбросил", self.say("/limit reset"))
        self.assertEqual(self.budget.pick(Tier.BRAIN), "claude-sonnet-5-5")
        self.assertAlmostEqual(self.budget.spent_today(), 0.0)

    def test_after_a_reset_new_spending_counts_again(self):
        self.app.store.log_usage("claude-sonnet-5-5", 1, 1, 2.5, "legacy")
        self.say("/limit reset")
        self.app.store.log_usage("claude-haiku-5-5", 1, 1, 0.3, "mouth")
        self.assertAlmostEqual(self.budget.spent_today(), 0.3)

    def test_a_limit_for_today_moves_the_whole_ladder(self):
        self.assertIn("$10.00", self.say("/limit 10"))
        self.assertEqual((self.budget.daily, self.budget.soft), (10.0, 7.5))
        self.app.store.log_usage("claude-sonnet-5-5", 1, 1, 5.0, "brain")
        self.assertEqual(self.budget.pick(__import__("jarvis4.budget", fromlist=["Tier"]).Tier.BRAIN), "claude-sonnet-5-5")

    def test_the_limit_for_today_is_gone_tomorrow(self):
        self.app.store.meta_set("limit_today", "2020-01-01:50")  # set long ago
        self.assertEqual(self.budget.daily, self.app.cfg.daily_limit)

    def test_nonsense_is_explained(self):
        self.assertIn("напиши так", self.say("/limit много"))
        self.assertIn("от $0.2 до $100", self.say("/limit 1000"))
        self.assertIn("потрачено", self.say("/limit"))


class AlertsTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.rings, self.voices = [], []

        class Api:
            async def ring_owner(inner, text):
                self.rings.append(text)
                return self.picked

        self.picked = False
        self.app.account.callapi = Api()

        async def send_voice(chat, text):
            self.voices.append(text)
        self.app.account.send_voice = send_voice

    def test_a_due_reminder_is_said(self):
        self.app.store.add_reminder(time.time() - 1, "выпить воды")
        run(self.app.alerts.tick())
        self.assertIn("⏰ выпить воды", self.app.out.said)
        self.assertEqual(self.app.store.due_reminders(), [])

    def test_a_calling_reminder_rings_and_falls_back_to_a_note(self):
        self.app.store.add_reminder(time.time() - 1, "созвон", call=True)
        run(self.app.alerts.tick())
        self.assertEqual(self.rings, ["созвон"])
        self.assertTrue(any("не взял" in s for s in self.app.out.said))

    def test_stop_freezes_reminders(self):
        self.app.store.add_reminder(time.time() - 1, "позже")
        self.app.core.frozen_until = time.time() + 60
        run(self.app.alerts.tick())
        self.assertEqual(len(self.app.store.due_reminders()), 1)

    def test_an_alarm_stops_ringing_when_the_owner_writes(self):
        async def scenario():
            fast = functools.partial(alerts_mod.ring_until_heard, every=0.05, escalate_after=0.2, repeats=40)
            alerts_mod.ring_until_heard = fast
            task = asyncio.create_task(self.app.alerts.alarm("кто-то вошёл на сервер"))
            await asyncio.sleep(0.3)
            before = len(self.rings)
            self.app.alerts.owner_seen()
            await asyncio.wait_for(task, 2)
            settled = len(self.rings)
            await asyncio.sleep(0.3)  # long enough for several more rings, were it still ringing
            return before, settled, len(self.rings)

        original = alerts_mod.ring_until_heard
        try:
            before, settled, final = run(scenario())
        finally:
            alerts_mod.ring_until_heard = original
        self.assertGreaterEqual(before, 2)
        self.assertLessEqual(settled, before + 1)  # a ring already on its way may finish
        self.assertEqual(final, settled)  # but none starts after he answered
        self.assertTrue(self.voices)  # while he did not pick up, a voice message went too

    def test_an_alarm_escalates_to_sms_and_a_robot_call(self):
        sent = []

        class Phone:
            ready = True

            async def sms(self, text):
                sent.append(("sms", text))
                return "SMS отправлено"

            async def call(self, text):
                sent.append(("call", text))
                return "Звоню"

        self.app.alerts.phone = Phone()

        async def scenario():
            alerts_mod.ring_until_heard = functools.partial(original, every=0.05, escalate_after=0.12, repeats=4)
            await self.app.alerts.alarm("диск заполнен")

        original = alerts_mod.ring_until_heard
        try:
            run(scenario())
        finally:
            alerts_mod.ring_until_heard = original
        self.assertEqual([k for k, _ in sent], ["sms", "call"])
        self.assertTrue(any("продублировал" in s for s in self.app.out.said))


class FakeCalls:
    def __init__(self, api, picks=True, transcript=None):
        self.api, self.picks, self.transcript, self.hung = api, picks, transcript or [], []

    async def call(self, peer, persona):
        self.persona = persona
        if self.picks:
            talk = LiveTalk(self, peer, persona)
            talk.transcript = self.transcript
            asyncio.get_running_loop().call_later(0.05, lambda: asyncio.create_task(self.api.ended(peer, talk)))
        return self.picks

    async def hang_up(self, peer):
        self.hung.append(peer)


class CallApiTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.told = []

        async def tell(text):
            self.told.append(text)
        self.app.account.tell_owner = tell
        self.api = CallApi(self.app.account, self.app.core, self.app.cfg, self.app.store)
        self.app.store.upsert_person("u77", "Сардор", "sardor_user", 77, dialog=True)

    def test_the_owner_may_start_work_by_voice_a_stranger_may_only_leave_a_note(self):
        owner = self.api.choose(OWNER_ID)
        self.assertEqual({t["name"] for t in owner.tools}, {"do_task", "hang_up"})
        stranger = self.api.choose(77)
        self.assertEqual({t["name"] for t in stranger.tools}, {"pass_to_owner", "hang_up"})
        self.assertIn("не знаешь и не обсуждаешь", stranger.system)

    def test_blocked_people_and_telegram_service_are_not_answered(self):
        self.app.store.upsert_person("u88", "Спамер", tg_id=88, blocked=True)
        self.assertIsNone(self.api.choose(88))
        self.assertIsNone(self.api.choose(777000))

    def test_a_chatty_stranger_is_limited(self):
        answers = [self.api.choose(77) for _ in range(5)]
        self.assertEqual([a is not None for a in answers], [True, True, True, False, False])

    def test_a_call_that_is_not_picked_up_says_so(self):
        self.api.calls = FakeCalls(self.api, picks=False)
        result = run(self.api.call("u77", "talk", "привет"))
        self.assertFalse(result.answered)
        self.assertIn("не взял трубку", result.text)

    def test_a_live_talk_returns_what_was_said_after_it_ends(self):
        self.api.calls = FakeCalls(self.api, transcript=[("jarvis", "привет, это Джарвис"), ("them", "о, здарова")])
        result = run(self.api.call("u77", "talk", "передать привет и пообщаться"))
        self.assertTrue(result.answered)
        self.assertIn("о, здарова", result.text)
        self.assertEqual(self.told, [])  # the task reports it itself: no extra «мне звонили» note
        stored = self.app.store.recent("u77", 5)
        self.assertTrue(any("[звонок" in m.text for m in stored))

    def test_say_mode_hangs_up_after_the_message(self):
        self.api.calls = FakeCalls(self.api, transcript=[("jarvis", "Ван передаёт привет")])
        result = run(self.api.call("u77", "say", "привет от Вана"))
        self.assertIn("сказал и положил", result.text)
        self.assertIn("привет от Вана", self.api.calls.persona.system)

    def test_calls_off_without_gemini(self):
        from jarvis4.ports import TGError
        with self.assertRaises(TGError):
            run(self.app.account.call("u77", "say", "привет"))


class AnnounceTest(unittest.TestCase):
    def app_with(self, llm):
        app = make_app()
        app.core.llm = llm
        return app

    class Broke:
        ready = True

        def __init__(self, kind):
            self.kind = kind

        async def complete(self, *a, **k):
            from jarvis4.llm import LLMError
            raise LLMError(self.kind, "x")

    def test_no_money_is_said_at_once(self):
        app = self.app_with(self.Broke("billing"))
        run(app.announce())
        self.assertTrue(any("кончились деньги" in s for s in app.out.said))
        self.assertTrue(any("на связи" in s for s in app.out.said))

    def test_a_restart_soon_after_is_not_announced_again(self):
        app = self.app_with(self.Broke("busy"))
        run(app.announce())
        self.assertEqual(len(app.out.said), 1)
        run(app.announce())
        self.assertEqual(len(app.out.said), 1)  # still no money? nothing new to say; and no «на связи» twice in 30 minutes

    def test_a_bad_key_is_said(self):
        app = self.app_with(self.Broke("auth"))
        run(app.announce())
        self.assertTrue(any("ключ Anthropic" in s for s in app.out.said))


class VoiceReplyTest(unittest.TestCase):
    def setUp(self):
        self.app = make_app()
        self.voiced = []

        async def voice(text):
            self.voiced.append(text)
        self.out = FakeOut()
        self.out.voice = voice
        self.itx = Interaction(kind="owner", chat="owner", sender="owner", text="x", msgs=[], owner=True, out=self.out,
                               spoken=True)

    def deliver(self, text):
        run(self.app.core.deliver(self.itx, text))

    def test_a_voice_message_gets_a_voice_answer_too(self):
        self.deliver("всё готово")
        self.assertEqual(self.out.said, ["всё готово"])
        self.assertEqual(self.voiced, ["всё готово"])

    def test_a_typed_message_does_not(self):
        self.itx.spoken = False
        self.deliver("всё готово")
        self.assertEqual(self.voiced, [])

    def test_the_command_switches_it_off_and_on(self):
        self.assertIn("только текстом", run(self.app.commands.run("/voice")))
        self.deliver("тихо")
        self.assertEqual(self.voiced, [])
        self.assertIn("и голосом", run(self.app.commands.run("/voice")))

    def test_a_long_answer_is_not_read_out(self):
        self.deliver("слово " * 100)
        self.assertEqual(self.voiced, [])

    def test_a_failing_voice_never_costs_the_answer(self):
        async def broken(text):
            raise RuntimeError("tts упал")
        self.out.voice = broken
        self.deliver("ответ")
        self.assertEqual(self.out.said, ["ответ"])


class CaptureOutTest(unittest.TestCase):
    def test_the_answer_is_kept_for_the_laptop_to_read_aloud(self):
        copy = FakeOut()
        out = CaptureOut(copy)
        run(out.say("первое"))
        run(out.say("второе"))
        self.assertEqual(out.lines, ["первое", "второе"])
        self.assertEqual(copy.said, [])


if __name__ == "__main__":
    unittest.main()
