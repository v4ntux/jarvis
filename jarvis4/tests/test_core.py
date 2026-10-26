"""The core against exam scenarios, with a scripted model: no money. What is tested here is everything around the
model: refs and replies reach the prompt, the guard, the journal, stop, hand-off, the answer check, authorization.
"""
import asyncio
import json
import unittest

from jarvis4.exam.agents.jarvis4 import Jarvis4
from jarvis4.exam.runner import run_one
from jarvis4.exam.scenario import load_all
from jarvis4.testing import ScriptedLLM, say, use, use_many

SCENARIOS = {s.id: s for s in load_all()}
OK = say(json.dumps({"ok": True, "fixed": "", "why": ""}))  # the answer check's verdict


def play(scenario_id, script, timeout=20.0):
    holder = {}

    def factory(store):
        holder["llm"] = ScriptedLLM(script, store)
        return holder["llm"]

    agent = Jarvis4(factory)
    result = asyncio.run(run_one(SCENARIOS[scenario_id], agent, None, timeout))
    return result, holder["llm"], agent


def failed(result):
    return "\n".join(f"{c.name}: {c.detail}" for c in result.failed) + result.note


class ForwardTest(unittest.TestCase):
    def test_reply_to_a_relay_forwards_the_right_sticker(self):
        result, llm, _ = play("forward-sticker", [
            use("tg_forward", refs=["zac_sticker"], to="owner"), say("держи, стикер от зака"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        prompt = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
        self.assertIn("в ответ на [relay]", prompt)  # the model reads which note was answered
        self.assertIn("[zac_sticker] [стикер 😡]", prompt)  # and which messages the note stands for
        self.assertIn("tg_forward", llm.calls[0]["tools"])
        self.assertNotIn("server_shell", llm.calls[0]["tools"])  # the mouth has no shell to improvise with

    def test_all_stickers_via_history_not_a_shell(self):
        result, llm, _ = play("forward-all-stickers", [
            use("tg_history", chat="zac", kind="sticker"),
            use("tg_forward", refs=["zac_sticker1", "zac_sticker2"]), say("лови оба"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        history = llm.calls[1]["messages"][-1]["content"][0]["content"][0]["text"]
        self.assertIn("[zac_sticker1]", history)
        self.assertNotIn("zac_old_text", history)

    def test_forwarding_to_a_stranger_is_a_question(self):
        # the model forwards to Sardor, whom nobody named: the guard asks, the owner (scenario) says yes by default
        result, llm, agent = play("forward-sticker", [
            use("tg_forward", refs=["zac_sticker"], to="owner"), say("ок"), OK])
        self.assertEqual(result.status, "pass", failed(result))


class HandoffTest(unittest.TestCase):
    def test_open_claude_app_from_the_taskbar(self):
        result, llm, _ = play("open-claude-app", [
            use("do_task", goal="Открой клауд", ack="щас", effort="normal"),
            use("laptop_look"), use("laptop_click", element="tb_claude_app"),
            say("открыл Claude из панели задач"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        models = [c["model"] for c in llm.calls]
        self.assertEqual(models[0], "claude-haiku-5-5")  # the mouth hands over...
        self.assertEqual(models[1], "claude-sonnet-5-5")  # ...and the brain takes a new laptop task
        self.assertNotIn("do_task", llm.calls[1]["tools"])
        self.assertIn("laptop_open", llm.calls[1]["tools"])
        self.assertIn("ЗАДАЧА", llm.calls[1]["system"][1]["text"])

    def test_a_terminal_that_opens_is_called_a_terminal(self):
        seen = []
        result, llm, _ = play("open-claude-app", [
            use("do_task", goal="Открой клауд"),
            use("laptop_open", name="claude"),
            lambda m, s, msgs, t: (seen.append(json.dumps(msgs[-1], ensure_ascii=False)), use("laptop_close", title="Claude Code"))[1],
            use("laptop_click", element="tb_claude_app"), say("открыл приложение Claude"), OK])
        # opening the terminal at all fails the scenario, but the result named it, so the model could undo it
        self.assertEqual(result.status, "fail")
        self.assertIn("терминал", seen[0])
        self.assertIn("не приложение", seen[0])

    def test_the_mouth_that_claims_without_acting_is_overruled(self):
        # Haiku says «открыл» with no tool call: the core hands the owner's words to the brain, which really does it
        result, llm, _ = play("open-claude-app", [
            say("открыл клауд"),
            use("laptop_click", element="tb_claude_app"), say("открыл Claude из панели задач"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        self.assertEqual(llm.calls[1]["model"], "claude-sonnet-5-5")


class GuardTest(unittest.TestCase):
    def test_env_and_session_are_refused_before_they_run(self):
        result, llm, _ = play("backup-no-questions", [
            use("do_task", goal="Сделай бекап", effort="simple"),
            use("server_shell", command="cd /opt/jarvis && set -a && . ./.env && set +a && tar -czf b.tgz data"),
            use("server_shell", command="cd /opt/jarvis && tar --exclude=.venv -czf backup/jarvis_$(date +%Y%m%d_%H%M).tar.gz "
                                         ".env jarvis data && tar -tzf backup/jarvis_$(date +%Y%m%d_%H%M).tar.gz | wc -l"),
            say("бекап готов: /opt/jarvis/backup/jarvis_20261026_1400.tar.gz, 12M, проверил"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        refused = llm.calls[2]["messages"][-1]["content"][0]["content"][0]["text"]
        self.assertTrue(refused.startswith("Отказ охраны"), refused)

    def test_mass_hello_skips_the_blocked_and_the_stranger(self):
        result, llm, _ = play("hello-everyone", [
            use_many([("tg_send", {"to": "kamron", "text": "здарова)"}), ("tg_send", {"to": "zac", "text": "йо"}),
                      ("tg_send", {"to": "sardor", "text": "привет"}), ("tg_send", {"to": "abubakr", "text": "эй"})]),
            say("написал камрону, заку и сардору. абубакр в блоке, ему не пишу"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        answers = [b["content"][0]["text"] for b in llm.calls[1]["messages"][-1]["content"]]
        self.assertTrue(any("заблокировал" in a for a in answers))

    def test_a_named_person_needs_no_question_an_unnamed_one_does(self):
        result, _, _ = play("call-and-talk", [
            use("tg_call", to="abubakr", mode="talk", goal="привет, пообщаться"),
            say("набрал, абубакр не взял трубку"), OK])
        self.assertEqual(result.status, "pass", failed(result))


class StopTest(unittest.TestCase):
    def test_stop_cancels_the_task_and_empties_the_queue(self):
        def later(reply, delay=0.15):  # the model "thinks" a moment, so the owner's next messages arrive mid-task
            async def step(model, system, messages, tools):
                await asyncio.sleep(delay)
                return reply
            return step

        async def forever(model, system, messages, tools):
            await asyncio.sleep(30)  # the brain is still "thinking" when the owner says stop
            return say("не дойдёт")

        result, llm, agent = play("stop-freezes-queue", [
            use("do_task", goal="Глянь что жрёт место на диске C и почисти временные файлы"),
            later(use("laptop_shell", command="Get-PSDrive C")), later(use("laptop_shell", command="dir C:/Users")),
            later(use("laptop_shell", command="dir C:/Temp")), forever])
        self.assertEqual(result.status, "pass", failed(result))
        said = [r["text"] for r in agent.world.replies if r["kind"] == "text"]
        self.assertEqual(said, ["стоп, всё бросил"])  # one word, and the queued «залей на гитхаб» never ran
        self.assertLessEqual(len(llm.calls), 5)  # nothing was asked of the model after the stop


class LessonTest(unittest.TestCase):
    def test_a_lesson_from_a_correction_is_in_the_next_prompt(self):
        result, llm, _ = play("lesson-call-not-write", [
            use("learn_lesson", text="Когда Ван просит позвонить, звонишь, а не пишешь сообщение. Не взял трубку — скажи Вану.",
                when="позвонить"),
            say("понял, косяк"),
            use("tg_call", to="@sardor_user", mode="say", goal="привет от Вана"), say("сардор не взял трубку, писать не стал"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        later = llm.calls[2]["system"][0]["text"]  # the system prompt of the call after the lesson
        self.assertIn("звонишь, а не пишешь", later)
        self.assertIn("Уроки", later)
        first = llm.calls[0]
        self.assertIn("поправляет", first["system"][1]["text"])  # the correction is flagged to the model


class ModeTest(unittest.TestCase):
    def test_an_angry_owner_gets_the_no_jokes_mode(self):
        result, llm, _ = play("angry-owner-no-jokes", [
            use("do_task", goal="Закрой терминал и открой приложение Claude", effort="simple"),
            use("laptop_close", title="Claude Code"), use("laptop_click", element="tb_claude_app"),
            say("косяк. терминал закрыл, открыл приложение Claude из панели задач"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        self.assertIn("Ван злится", llm.calls[0]["system"][1]["text"])
        self.assertEqual(llm.calls[1]["model"], "claude-haiku-5-5")  # «simple» stays on the cheap model


class SpeakTest(unittest.TestCase):
    def test_a_failed_call_followed_by_a_bare_reaction_still_gets_words(self):
        # exam run: the call failed, the model sent 😢 and said nothing at all
        result, llm, agent = play("call-and-talk", [
            use("tg_call", to="abubakr", mode="talk", goal="привет, пообщаться"),
            use("tg_react", emoji="😢"),
            say("набрал, абубакр не взял трубку. писать не стал"), OK])
        self.assertEqual(result.status, "pass", failed(result))
        nudge = llm.calls[2]["messages"][-1]["content"][-1]["text"]
        self.assertIn("не сказал ни слова", nudge)
        said = [r["text"] for r in agent.world.replies if r["kind"] == "text"]
        self.assertEqual(said, ["набрал, абубакр не взял трубку. писать не стал"])

    def test_a_reaction_alone_to_a_meme_needs_no_words(self):
        result, llm, agent = play("group-joke", [use("tg_react", emoji="🔥")])
        self.assertEqual(len(llm.calls), 1)  # nothing was done, so nobody is asked to say anything


class HonestyTest(unittest.TestCase):
    def test_an_answer_the_journal_does_not_support_is_rewritten(self):
        fixed = say(json.dumps({"ok": False, "fixed": "не вышло: сардор не взял трубку", "why": "заявил о разговоре"}))
        result, llm, agent = play("call-and-talk", [
            use("tg_call", to="abubakr", mode="talk", goal="привет"), say("поговорили, всё ок"), fixed])
        said = [r["text"] for r in agent.world.replies if r["kind"] == "text"]
        self.assertEqual(said, ["не вышло: сардор не взял трубку"])


if __name__ == "__main__":
    unittest.main()
