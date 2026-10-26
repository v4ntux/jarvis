"""The eyes with a scripted model and a pretend screen: what is sent to the device, what comes back to the model,
and the rules around it (a failed step cancels the batch, limits, the owner's «да» before anything risky)."""
import asyncio
import json
import unittest

from jarvis4.config import Config
from jarvis4.core import Core
from jarvis4.eyes import NOT_RUN
from jarvis4.ports import Offline, Platform
from jarvis4.store import Store
from jarvis4.testing import ScriptedLLM, say, use, use_many
from jarvis4.tools import Ctx
from jarvis4.types import Interaction

PNG = b"\x89PNG\r\n\x1a\nfake"


class FakeOut:
    def __init__(self, answer=True):
        self.said, self.asked, self.answer = [], [], answer

    async def say(self, text, reply_ref=""):
        self.said.append(text)
        return "x"

    async def ask(self, question):
        self.asked.append(question)
        return self.answer

    async def progress(self, line):
        pass

    async def send_file(self, name, data):
        pass

    async def typing(self):
        pass

    async def finish(self):
        pass


class FakeScreen:
    name = "laptop"

    def __init__(self, online=True, fail_on=None):
        self.online, self.did, self.fail_on = online, [], fail_on

    async def do(self, name, args):
        self.did.append((name, args))
        if name == self.fail_on:
            raise RuntimeError("Windows не принял ввод")
        if name in ("screenshot", "zoom"):
            return "Скриншот", PNG
        return "OK", None

    async def screenshot(self):
        if not self.online:
            raise Offline("нет связи")
        return PNG


class Tg:  # the eyes never touch Telegram
    pass


def build(script, screen=None, **cfg):
    store = Store(":memory:")
    config = Config(owner_id=1, db_path=":memory:", anthropic_key="", **cfg)
    llm = ScriptedLLM(script, store)
    screens = {"laptop": screen or FakeScreen()}
    core = Core(config, store, Platform(tg=Tg(), server=None, web=None, extras={"screens": screens}), llm=llm,
                outs=lambda chat: FakeOut())
    return core, llm, screens["laptop"]


def interaction(core, answer=True):
    out = FakeOut(answer)
    return Interaction(kind="owner", chat="owner", sender="owner", text="сделай", msgs=[], owner=True, out=out), out


def run_eyes(core, itx, goal="открыть Claude", device="laptop"):
    return asyncio.run(core.eyes.run(itx, goal, device))


class EyesLoopTest(unittest.TestCase):
    def test_a_normal_task_looks_acts_looks_and_reports(self):
        core, llm, screen = build([
            use_many([("left_click", {"coordinate": [640, 700]}), ("screenshot", {})]),
            use_many([("key", {"text": "Return"}), ("screenshot", {})]),
            say("окно Claude открыто и на переднем плане")])
        itx, _ = interaction(core)
        result = run_eyes(core, itx)
        self.assertTrue(result.ok, result.text)
        self.assertIn("окно Claude открыто", result.text)
        self.assertEqual([n for n, _ in screen.did], ["left_click", "screenshot", "key", "screenshot"])
        # the model is offered Anthropic's toolset, and shown the first picture with the task before it
        first = llm.calls[0]
        self.assertEqual(first["tools"][0], "computer_toolset_20260801") if isinstance(first["tools"][0], str) else None
        content = first["messages"][0]["content"]
        self.assertEqual([c["type"] for c in content], ["text", "image"])
        # every answer carries the toolset name; a screenshot answers with a picture, a click with «OK»
        answers = llm.calls[1]["messages"][-1]["content"]
        self.assertTrue(all(a["toolset_name"] == "computer" for a in answers))
        self.assertEqual(answers[0]["content"][0]["text"], "OK")
        self.assertEqual(answers[1]["content"][0]["type"], "image")
        steps = [r["tool"] for r in core.store.journal(10, task=itx.id)]
        self.assertEqual(steps, ["screen:left_click", "screen:screenshot", "screen:key", "screen:screenshot"])
        self.assertGreater(itx.spent, 0)

    def test_the_first_failure_cancels_the_rest_of_the_batch(self):
        screen = FakeScreen(fail_on="left_click")
        core, llm, _ = build([
            use_many([("left_click", {"coordinate": [1, 1]}), ("type", {"text": "привет"}), ("screenshot", {})]),
            say("не смог нажать")], screen=screen)
        itx, _ = interaction(core)
        run_eyes(core, itx)
        answers = llm.calls[1]["messages"][-1]["content"]
        self.assertTrue(answers[0]["is_error"])
        self.assertIn("Windows не принял ввод", answers[0]["content"])
        self.assertEqual([a["content"] for a in answers[1:]], [NOT_RUN, NOT_RUN])
        self.assertTrue(all(a["is_error"] for a in answers[1:]))
        self.assertEqual([n for n, _ in screen.did], ["left_click"])  # the typing never happened

    def test_the_owner_says_no_and_nothing_risky_is_done(self):
        core, llm, screen = build([
            use("need_owner", question="Нажать «Оплатить 990 ₽»?"),
            say("Ван отказал, ничего не нажимал")])
        itx, out = interaction(core, answer=False)
        result = run_eyes(core, itx, goal="купить подписку")
        self.assertEqual(out.asked, ["Нажать «Оплатить 990 ₽»?"])
        self.assertTrue(itx.declined)
        answer = llm.calls[1]["messages"][-1]["content"][0]
        self.assertIn("Ван сказал: нет", answer["content"])
        self.assertEqual(screen.did, [])

    def test_the_owner_says_yes(self):
        core, llm, screen = build([use("need_owner", question="Удалить файл?"), say("удалил")])
        itx, _ = interaction(core, answer=True)
        run_eyes(core, itx)
        self.assertIn("Ван сказал: да", llm.calls[1]["messages"][-1]["content"][0]["content"])

    def test_it_stops_at_the_step_limit_and_says_what_it_managed(self):
        core, _, _ = build([use("left_click", coordinate=[1, 1]) for _ in range(10)], eyes_steps=3)
        itx, _ = interaction(core)
        result = run_eyes(core, itx)
        self.assertFalse(result.ok)
        self.assertIn("3 шагов мало", result.text)
        self.assertIn("3×left_click", result.text)

    def test_it_stops_when_the_task_costs_too_much(self):
        core, llm, _ = build([use("left_click", _cost=0.3, coordinate=[1, 1]) for _ in range(10)], eyes_task_limit=0.5)
        itx, _ = interaction(core)
        result = run_eyes(core, itx)
        self.assertFalse(result.ok)
        self.assertIn("лимит $0.50", result.text)
        self.assertEqual(len(llm.calls), 2)

    def test_a_device_that_is_not_there_is_named(self):
        core, _, _ = build([])
        itx, _ = interaction(core)
        result = run_eyes(core, itx, device="android")
        self.assertFalse(result.ok)
        self.assertIn("Есть: laptop", result.text)

    def test_an_offline_device_is_a_failure_not_a_crash(self):
        core, _, _ = build([], screen=FakeScreen(online=False))
        itx, _ = interaction(core)
        result = run_eyes(core, itx)
        self.assertFalse(result.ok)
        self.assertIn("не на связи", result.text)

    def test_an_action_the_toolset_does_not_have_is_an_error_for_the_model(self):
        core, llm, screen = build([use("format_c", drive="c"), say("ок")])
        itx, _ = interaction(core)
        run_eyes(core, itx)
        self.assertTrue(llm.calls[1]["messages"][-1]["content"][0]["is_error"])
        self.assertEqual(screen.did, [])


class ScreenTaskToolTest(unittest.TestCase):
    def test_paying_is_a_question_before_anything_starts(self):
        from jarvis4.tools import all_tools
        core, _, _ = build([])
        itx, _ = interaction(core)
        tool = all_tools()["screen_task"]
        verdict = asyncio.run(tool.guard(Ctx(core, itx), {"goal": "оплатить заказ на сайте"}))
        self.assertEqual(verdict.kind, "ask")
        verdict = asyncio.run(tool.guard(Ctx(core, itx), {"goal": "открыть Claude"}))
        self.assertEqual(verdict.kind, "allow")

    def test_only_the_brain_has_the_screen(self):
        from jarvis4.tools import all_tools, for_role
        registry = all_tools()
        mouth = {t.name for t in for_role(registry, "mouth", True)}
        brain = {t.name for t in for_role(registry, "brain", True)}
        others = {t.name for t in for_role(registry, "mouth", False)}
        self.assertIn("screen_task", brain)
        self.assertNotIn("screen_task", mouth | others)
        self.assertNotIn("laptop_see", mouth | others)


if __name__ == "__main__":
    unittest.main()
