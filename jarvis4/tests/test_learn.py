"""Learning: the words and stickers of a chat, skills that climb or break, the group's spontaneous lane, the night review."""
import asyncio
import datetime as dt
import json
import time
import unittest

from jarvis4 import clock, learn
from jarvis4.config import Config
from jarvis4.core import Core, Incoming
from jarvis4.ports import Platform
from jarvis4.store import Store
from jarvis4.testing import ScriptedLLM, say
from jarvis4.tools import Ctx, all_tools
from jarvis4.types import Interaction


class Out:
    def __init__(self):
        self.said = []

    async def say(self, text, reply_ref=""):
        self.said.append(text)
        return f"o/{len(self.said)}"

    async def ask(self, q):
        return True

    async def progress(self, line):
        pass

    async def send_file(self, name, data):
        pass

    async def typing(self):
        pass

    async def finish(self):
        pass


class Tg:
    def __init__(self):
        self.reacted = []

    async def react(self, ref, emoji):
        self.reacted.append((ref, emoji))


def build(script=None):
    store = Store(":memory:")
    cfg = Config(owner_id=1, db_path=":memory:", anthropic_key="")
    tg = Tg()
    out = Out()
    llm = ScriptedLLM(script or [], store)
    core = Core(cfg, store, Platform(tg=tg, server=None, web=None), llm=llm, outs=lambda chat: out)
    return core, store, tg, out, llm


def itx(core, text="сделай", out=None):
    return Interaction(kind="owner", chat="owner", sender="owner", text=text, msgs=[], owner=True, out=out or Out())


class WordsTest(unittest.TestCase):
    def test_filler_urls_and_short_words_are_not_counted(self):
        found = learn.tokens("Короче это вообще кринж, смотри https://x.com/кринж @bot и ну да")
        self.assertEqual(found, ["кринж", "смотри"])

    def test_a_chat_style_block_is_made_of_what_the_chat_says_often(self):
        store = Store(":memory:")
        for _ in range(4):
            store.count_words("g1", learn.tokens("это полный кринж, братан"))
        store.count_words("g1", ["редкое"])
        store.sticker_seen("g1", 11, "🗿", "", "ну и ну")
        store.sticker_seen("g1", 11, "🗿")
        store.sticker_seen("g1", 12, "😂")
        text = learn.chat_style(store, "g1")
        self.assertIn("кринж", text)
        self.assertIn("братан", text)
        self.assertNotIn("редкое", text)  # said once: not a habit yet
        self.assertIn("🗿×2", text)
        self.assertEqual(learn.chat_style(store, "g2"), "")

    def test_messages_teach_the_words_but_jarvis_own_words_do_not(self):
        core, store, *_ = build()
        for i in range(3):
            core.ingest(Incoming(ref=f"g1/{i}", chat="g1", sender="u5", text="опять этот кринж", group=True))
        core.ingest(Incoming(ref="g1/9", chat="g1", sender="jarvis", text="годнота годнота годнота годнота"))
        words = dict(store.top_words("g1", 10))
        self.assertEqual(words.get("кринж"), 3)
        self.assertNotIn("годнота", words)

    def test_the_prompt_carries_the_chat_style(self):
        from jarvis4 import context
        core, store, *_ = build()
        for _ in range(3):
            store.count_words("owner", ["брат"] * 1 + ["епта"])
        system = context.system(core, itx(core), "mouth")
        self.assertIn("епта", system[0]["text"])


class SkillsTest(unittest.TestCase):
    def setUp(self):
        self.core, self.store, *_ = build()
        self.tools = all_tools()

    def run_tool(self, tool, itx_, **args):
        return asyncio.run(self.tools[tool].run(Ctx(self.core, itx_), args))

    STEPS = "1) laptop_open('claude') 2) проверить laptop_windows: окно «Claude», вид «приложение», не терминал"

    def test_a_skill_is_saved_only_after_real_work_succeeded(self):
        it = itx(self.core)
        result = self.run_tool("skill_save", it, name="открыть claude", description="когда просят открыть клауд",
                               steps=self.STEPS)
        self.assertFalse(result.ok)
        self.assertIn("Сначала сделай", result.text)
        self.store.journal_add(it.id, "laptop_open", {"name": "claude"}, True, "Запускаю")
        result = self.run_tool("skill_save", it, name="открыть claude", description="когда просят открыть клауд",
                               steps=self.STEPS)
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.store.skill("открыть claude")["status"], "draft")

    def test_reading_alone_is_not_work(self):
        it = itx(self.core)
        self.store.journal_add(it.id, "laptop_windows", {}, True, "окна")
        result = self.run_tool("skill_save", it, name="что-то", description="описание на много слов", steps=self.STEPS)
        self.assertFalse(result.ok)

    def test_skills_climb_with_successes_and_break_with_failures(self):
        self.store.skill_save("a", "для проверки статусов", "шаги")
        self.assertEqual([self.store.skill_outcome("a", True) for _ in range(3)], ["verified", "verified", "trusted"])
        self.store.skill_save("b", "для проверки статусов", "шаги")
        self.assertEqual([self.store.skill_outcome("b", False) for _ in range(2)], ["draft", "broken"])
        self.assertNotIn("b", self.core.skills_text("x"))  # a broken skill is not offered
        self.assertIn("a [trusted]", self.core.skills_text("x"))

    def test_rewriting_a_skill_makes_it_a_draft_again(self):
        self.store.skill_save("a", "для проверки статусов", "шаги")
        self.store.skill_outcome("a", True)
        self.store.skill_save("a", "для проверки статусов", "другие шаги")
        self.assertEqual(self.store.skill("a")["status"], "draft")

    def test_following_a_skill_that_works_counts_for_it(self):
        self.store.skill_save("открыть claude", "когда просят открыть клауд", self.STEPS)
        it = itx(self.core)
        result = self.run_tool("skill_get", it, name="открыть claude")
        self.assertIn("открыть клауд", result.text)
        self.assertEqual(it.skills_used, ["открыть claude"])
        self.store.journal_add(it.id, "skill_get", {}, True, "")
        self.store.journal_add(it.id, "laptop_open", {}, True, "ok")
        self.assertTrue(self.core._skills_ok(it, "done"))
        self.assertFalse(self.core._skills_ok(it, "stopped"))
        self.store.journal_add(it.id, "laptop_click", {}, False, "нет кнопки")
        self.assertFalse(self.core._skills_ok(it, "done"))

    def test_an_unknown_skill_lists_the_known_ones(self):
        self.store.skill_save("a", "для проверки статусов", "шаги")
        result = self.run_tool("skill_get", itx(self.core), name="нет такого")
        self.assertFalse(result.ok)
        self.assertIn("Есть: a", result.text)

    def test_a_recorded_show_becomes_a_draft_skill(self):
        recorded = []

        class Lap:
            online = True

            async def record(self, name):
                recorded.append(name)
                return "Запись идёт."

        self.core.platform.laptop = Lap()
        result = self.run_tool("show_me_start", itx(self.core), name="музыка", when="когда просят включить мою музыку")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(recorded, ["музыка"])
        skill = self.store.skill("музыка")
        self.assertEqual((skill["status"], skill["body"]), ("draft", "routine:музыка"))
        self.assertIn("laptop_routine", self.run_tool("skill_get", itx(self.core), name="музыка").text)


class GroupLaneTest(unittest.TestCase):
    def setUp(self):
        self.core, self.store, self.tg, self.out, _ = build()
        self.store.chat_seen("g1", "group", "Пацаны", 1)

    def message(self, text="", sender="u5", media=None, ref=None, mentioned=False):
        self.n = getattr(self, "n", 0) + 1
        return Incoming(ref=ref or f"g1/{self.n}", chat="g1", sender=sender, text=text, media=media, group=True,
                        mentioned=mentioned)

    def test_the_owner_can_mute_and_unmute_him_in_the_group(self):
        asyncio.run(self.core._from_group(self.message("Джарвис, тихо", sender="owner")))
        self.assertEqual(self.store.chat("g1")[2], "quiet")
        self.assertEqual(self.tg.reacted[-1][1], "🫡")
        asyncio.run(self.core._from_group(self.message("Джарвис, говори", sender="owner")))
        self.assertEqual(self.store.chat("g1")[2], "on")

    def test_a_member_cannot_mute_him(self):
        core, store, tg, out, llm = build([say("не, я тут")])
        store.chat_seen("g1", "group", "Пацаны", 1)
        asyncio.run(core._from_group(self.message("Джарвис, тихо", sender="u5")))
        self.assertEqual(store.chat("g1")[2], "on")  # only the owner's word switches him
        self.assertEqual(out.said, ["не, я тут"])  # a member's «тихо» is just talk he answers

    def test_spontaneous_remarks_are_rare_and_capped(self):
        core, store = self.core, self.store
        core.rng.random = lambda: 0.0  # the dice always say yes: only the caps can stop him
        self.assertTrue(core.group_wants_to_speak(self.message("смешно")))
        core.group_last_spoke["g1"] = (time.time(), "u5")
        self.assertFalse(core.group_wants_to_speak(self.message("ещё смешнее")))  # he just spoke
        core.group_last_spoke.pop("g1")
        for i in range(8):
            store.add_msg(f"g1/m{i}", "g1", "jarvis", "реплика")
        self.assertFalse(core.group_wants_to_speak(self.message("и ещё")))  # eight an hour is enough
        core.rng.random = lambda: 0.99
        self.assertFalse(Core.group_wants_to_speak(core, self.message("тишина")))

    def test_pictures_get_more_attention_than_text(self):
        self.core.rng.random = lambda: 0.1
        self.assertTrue(self.core.group_wants_to_speak(self.message("", media={"kind": "photo"})))
        self.assertFalse(self.core.group_wants_to_speak(self.message("просто текст")))


class ReviewTest(unittest.TestCase):
    VERDICT = {"summary": "Дважды путал звонок и сообщение, научился звонить, когда просят позвонить.",
               "lessons": [{"text": "Просят позвонить — звони, не пиши сообщение, даже если не дозвонился.", "when": "звонок"}],
               "profiles": [{"key": "u5", "profile": "Пишет коротко, любит мемы про котов."}]}

    def setUp(self):
        self.core, self.store, _, self.out, self.llm = build([say(json.dumps(self.VERDICT))])
        from jarvis4.alerts import Alerts
        self.alerts = Alerts(self.core, type("A", (), {"dry": lambda s, *a: ""})(), None, self.store)
        self.review = learn.Review(self.core, self.alerts)
        self.store.upsert_person("u5", "Камрон", tg_id=5, dialog=True)

    def a_bad_day(self):
        self.store.journal_add("t1", "tg_send", {"to": "abubakr"}, False, "заблокировал")
        self.store.add_msg("owner/1", "owner", "jarvis", "написал ему", ts=time.time() - 60)
        self.store.add_msg("owner/2", "owner", "owner", "я же сказал звонить а не писать", ts=time.time() - 30)
        for i in range(7):
            self.store.add_msg(f"u5/{i}", "u5", "u5", f"сообщение номер {i} про котов", ts=time.time() - 20 + i)

    def test_a_quiet_day_costs_nothing(self):
        self.assertEqual(asyncio.run(self.review.run()), "")
        self.assertEqual(self.llm.calls, [])

    def test_a_bad_day_becomes_lessons_profiles_and_a_morning_note(self):
        self.a_bad_day()
        note = asyncio.run(self.review.run())
        self.assertIn("выучил", note)
        lessons = self.store.lessons()
        self.assertEqual(len(lessons), 1)
        self.assertIn("звони, не пиши", lessons[0][1])
        self.assertIn("коротко", self.store.person("u5").profile)
        prompt = self.llm.calls[0]["messages"][0]["content"]
        self.assertIn("заблокировал", prompt)  # the failure
        self.assertIn("я же сказал звонить", prompt)  # the correction, with what it answered
        self.assertIn("написал ему", prompt)

    def test_a_lesson_that_repeats_an_old_one_is_not_added_again(self):
        self.a_bad_day()
        self.store.add_lesson("Просят позвонить — звони, не пиши сообщение, даже если не дозвонился.", "звонок")
        asyncio.run(self.review.run())
        self.assertEqual(len(self.store.lessons()), 1)

    def test_the_note_arrives_in_the_morning_once(self):
        self.store.meta_set("morning_note", "🌙 разобрал день")
        night = dt.datetime(2026, 10, 27, 5, 0, tzinfo=clock.TZ)
        morning = dt.datetime(2026, 10, 27, 9, 30, tzinfo=clock.TZ)
        self.store.meta_set("review_day", "2026-10-27")
        asyncio.run(self.review.tick(night))
        self.assertEqual(self.out.said, [])
        asyncio.run(self.review.tick(morning))
        self.assertEqual(self.out.said, ["🌙 разобрал день"])
        asyncio.run(self.review.tick(morning))
        self.assertEqual(len(self.out.said), 1)

    def test_the_review_runs_once_a_day_after_four(self):
        self.a_bad_day()
        early = dt.datetime(2026, 10, 27, 3, 0, tzinfo=clock.TZ)
        asyncio.run(self.review.tick(early))
        self.assertEqual(self.llm.calls, [])
        late = dt.datetime(2026, 10, 27, 4, 30, tzinfo=clock.TZ)
        asyncio.run(self.review.tick(late))
        self.assertEqual(len(self.llm.calls), 1)
        asyncio.run(self.review.tick(late))
        self.assertEqual(len(self.llm.calls), 1)


if __name__ == "__main__":
    unittest.main()
