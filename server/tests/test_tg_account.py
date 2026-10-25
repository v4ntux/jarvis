"""Jarvis's own Telegram account without Telegram, Gemini, Claude or real calls: the Gemini
chain, the free chat in front of the brain, stranger rules, Live call audio and yes/no in words.

Run from server/: python -m unittest discover -s tests
"""
import asyncio
import dataclasses
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import anthropic
import httpx
import numpy as np

from jarvis import calls, config, gemini, style, tg_user, voice
from jarvis.front import FREE_TOOLS, Front
from jarvis.memory import Memory
from jarvis.transport import UserChat

ENV = {"OWNER_TG_ID": "1", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}


def make_cfg(**changes):
    with patch.dict(os.environ, ENV, clear=True):
        cfg = config.load()
    return dataclasses.replace(cfg, **{"gemini_key": "g", **changes})


class FakeGemini:
    """Answers with scripted parts, one list per call."""

    lite_first = ["lite", "best"]

    def __init__(self, *answers, ready=True):
        self.answers, self.ready, self.seen, self.models_used = list(answers), ready, [], []

    async def generate(self, system, contents, tools=None, max_tokens=2048, models=None):
        self.seen.append((system, contents, tools))
        self.models_used.append(models)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def text(self, system, contents, max_tokens=2048, models=None, **_):
        return gemini.text_of(await self.generate(system, contents, models=models))


class GeminiChainTests(unittest.IsolatedAsyncioTestCase):
    def client(self, replies, models="a,b"):
        g = gemini.Gemini(make_cfg(gemini_model=models))
        self.asked = []

        async def post(model, body):
            self.asked.append((model, body))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        g._post = post
        return g

    async def test_overloaded_model_hands_over_to_the_next(self):
        g = self.client([(503, {"error": "high demand"}),
                         (200, {"candidates": [{"content": {"parts": [{"text": "Привет"}]}}]})])
        self.assertEqual(await g.text("s", []), "Привет")
        self.assertEqual([m for m, _ in self.asked], ["a", "b"])
        self.assertEqual(self.asked[0][1]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "minimal"})

    async def test_timeout_hands_over_too(self):
        import httpx

        g = self.client([httpx.ReadTimeout("slow"), (200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})])
        self.assertEqual(await g.text("s", []), "ok")

    async def test_spent_daily_quota_is_skipped_until_it_resets(self):
        quota = {"error": {"details": [{"retryDelay": "73532s"}]}}
        ok = (200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
        g = self.client([(429, quota), ok, ok])
        await g.text("s", [])
        await g.text("s", [])
        self.assertEqual([m for m, _ in self.asked], ["a", "b", "b"])  # «a» is not asked again today
        self.assertGreater(g.resting["a"] - time.time(), 73000)
        self.assertEqual(gemini.rest_for(503, {}), 20)

    def test_lite_models_first_for_strangers(self):
        g = gemini.Gemini(make_cfg(gemini_model="gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"))
        self.assertEqual(g.models[0], "gemini-3.6-flash")
        self.assertEqual(g.lite_first, ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.6-flash"])

    async def test_all_down(self):
        g = self.client([(503, {}), (429, {})])
        with self.assertRaises(gemini.GeminiError):
            await g.text("s", [])

    async def test_function_calls_and_thoughts(self):
        g = self.client([(200, {"candidates": [{"content": {"parts": [
            {"text": "размышляю", "thought": True}, {"functionCall": {"name": "web_search", "args": {"query": "q"}}}]}}]})])
        parts = await g.generate("s", [], tools=[{"name": "web_search"}])
        self.assertEqual(parts[1]["functionCall"]["name"], "web_search")
        self.assertEqual(gemini.text_of(parts), "")
        self.assertEqual(self.asked[0][1]["tools"], [{"functionDeclarations": [{"name": "web_search"}]}])


class FakeHub:
    online = True
    routines = []


class FakeBrain:
    """Stands in for the Claude brain: records what it was asked to do."""

    def __init__(self, memory):
        self.memory, self.cfg, self.hub = memory, make_cfg(), FakeHub()
        self.replies, self.tools, self.hints = [], [], []
        self.down = 0  # how many of the next requests fail as if Anthropic were unreachable
        self.spent = 0.0

    async def reply(self, text, session, images=None, task=False, hint=""):
        if self.down:
            self.down -= 1
            raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))
        self.replies.append(text)
        self.hints.append(hint)
        self.task = task
        return "сделал на ноутбуке ($0.02)"

    def spent_today(self):
        return self.spent

    async def _run_tool(self, name, args, session):
        self.tools.append((name, args))
        return {"text": "Напоминание #1 на 25.10 12:00."}

    def _routine_command(self, text):
        return "почта" if text == "сделай почту" else None


class Dice:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class FakeSession:
    def __init__(self, yes=True):
        self.lines, self.yes, self.asked = [], yes, []

    async def progress(self, line):
        self.lines.append(line)

    async def confirm(self, question):
        self.asked.append(question)
        return self.yes


class FrontTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.brain = FakeBrain(self.memory)

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def front(self, *answers, ready=True):
        f = Front(self.brain, FakeGemini(*answers, ready=ready))
        return f

    async def test_the_owner_talks_to_claude(self):
        f = self.front()
        self.assertEqual(await f.reply("привет", FakeSession()), "сделал на ноутбуке ($0.02)")
        self.assertEqual(self.brain.replies, ["привет"])
        self.assertEqual(f.gemini.seen, [])

    async def test_small_talk_now_and_then_carries_the_sticker_urge(self):
        f = self.front()
        f.rng = Dice(0.0)
        await f.reply("как дела", FakeSession())
        await f.reply("открой блокнот", FakeSession())  # work never gets it
        self.assertEqual(self.brain.hints, [style.URGE, ""])

    async def test_claude_down_gemini_answers_and_the_request_is_stored_once(self):
        self.memory.add("user", "привет")  # what the brain had stored before it failed
        self.brain.down = 1
        f = self.front([{"text": "Привет, Ван."}])
        self.assertEqual(await f.reply("привет", FakeSession()), "Привет, Ван.")
        self.assertEqual(self.brain.replies, [])
        self.assertEqual([m["content"] for m in self.memory.recent(5)], ["привет", "Привет, Ван."])

    async def test_todays_money_spent_chat_goes_to_gemini_but_work_still_to_claude(self):
        self.brain.spent = 100.0
        f = self.front([{"text": "Норм."}])
        self.assertEqual(await f.reply("как дела", FakeSession()), "Норм.")
        await f.reply("открой блокнот", FakeSession())
        self.assertEqual(self.brain.replies, ["открой блокнот"])  # the brain itself says the limit is reached

    async def test_free_tool_then_answer(self):
        self.brain.down = 1
        f = self.front([{"functionCall": {"name": "set_reminder", "args": {"text": "чайник", "in_minutes": 10}}}],
                       [{"text": "Напомню через 10 минут."}])
        self.assertEqual(await f.reply("напомни про чайник", FakeSession()), "Напомню через 10 минут.")
        self.assertEqual(self.brain.tools, [("set_reminder", {"text": "чайник", "in_minutes": 10})])
        self.assertEqual(self.brain.replies, [])
        answer = f.gemini.seen[1][1][-1]["parts"][0]["functionResponse"]
        self.assertEqual(answer["name"], "set_reminder")
        self.assertIn("[действия: set_reminder]", self.memory.recent(5)[-1]["content"])

    async def test_real_work_goes_to_the_brain(self):
        self.brain.down = 1  # Gemini stands in, and still hands the work over
        f = self.front([{"functionCall": {"name": "delegate", "args": {"task": "открыть блокнот"}}}])
        self.assertEqual(await f.reply("открой блокнот", FakeSession()), "сделал на ноутбуке ($0.02)")
        self.assertEqual(self.brain.replies, ["открой блокнот"])  # his own words, not Gemini's paraphrase
        self.assertTrue(self.brain.task)
        self.assertEqual(self.memory.recent(5), [])  # the brain keeps its own history

    async def test_saying_it_hands_over_without_calling_still_hands_over(self):
        self.brain.down = 1
        f = self.front([{"text": "Это работа для сервера. Передаю задачу старшему мозгу."}])
        await f.reply("скачай видео", FakeSession())
        self.assertEqual(self.brain.replies, ["скачай видео"])

    async def test_unknown_tool_is_real_work_too(self):
        self.brain.down = 1
        f = self.front([{"functionCall": {"name": "run_shell", "args": {"command": "dir"}}}])
        await f.reply("покажи файлы", FakeSession())
        self.assertEqual(len(self.brain.replies), 1)
        self.assertEqual(self.brain.tools, [])

    async def test_gemini_down_brain_answers_with_what_was_done(self):
        self.brain.down = 1
        f = self.front([{"functionCall": {"name": "remember", "args": {"text": "кот Барсик"}}}],
                       gemini.GeminiError("503"))
        await f.reply("запомни кота", FakeSession())
        self.assertEqual(self.brain.replies, ["запомни кота\n[Уже сделано: remember]"])

    async def test_think_word_routine_and_no_key_skip_gemini(self):
        for text in ("подумай хорошенько над планом", "сделай почту"):
            f = self.front()
            await f.reply(text, FakeSession())
        await self.front(ready=False).reply("привет", FakeSession())
        self.assertEqual(len(self.brain.replies), 3)

    async def test_watchers_are_set_without_questions(self):
        # Dangerous commands are still caught where every command passes: the brain's confirmation
        f = self.front()
        session = FakeSession(yes=False)
        await f.run_tool("add_watcher", {"source": "laptop", "command": "Get-Content x"}, session)
        await f.run_tool("add_watcher", {"source": "time", "at": "08:00"}, session)
        self.assertEqual(len(self.brain.tools), 2)
        self.assertEqual(session.asked, [])

    async def test_a_task_answered_with_a_question_goes_to_the_brain(self):
        self.brain.down = 1
        f = self.front([{"text": "Кому именно отправить? Скажи ник."}])
        await f.reply("отправь 20 рилсов Заке", FakeSession())
        self.assertEqual(len(self.brain.replies), 1)
        self.assertIn("без вопросов", self.brain.replies[0])

    async def test_a_chat_question_is_just_answered(self):
        self.brain.down = 1
        f = self.front([{"text": "Нормально! А у тебя как?"}])
        self.assertEqual(await f.reply("как дела", FakeSession()), "Нормально! А у тебя как?")
        self.assertEqual(self.brain.replies, [])

    async def test_reaction_is_kept_out_of_the_history(self):
        self.brain.down = 1
        f = self.front([{"text": "РЕАКЦИЯ: 🔥\nБаян, но зашёл."}])
        answer = await f.reply("[гифка]", FakeSession())
        self.assertTrue(answer.startswith("РЕАКЦИЯ: 🔥"))  # the chat strips it and puts the emoji under the message
        self.assertEqual(self.memory.recent(5)[-1]["content"], "Баян, но зашёл.")

    async def test_stickers_and_videos_reach_gemini_not_claude(self):
        f = self.front([{"text": "ору"}])
        media = [{"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}}]
        await f.reply("[видео 5 сек]", FakeSession(), media=media)
        sent = f.gemini.seen[0][1][-1]["parts"]
        self.assertEqual(sent[-1], media[0])
        self.assertEqual(self.brain.replies, [])

    def test_only_server_side_tools_are_free(self):
        self.assertFalse({"run_shell", "server_shell", "server_job", "browse", "look", "put_file"} & set(FREE_TOOLS))
        names = {t["name"] for t in Front(self.brain, FakeGemini()).tools}
        self.assertEqual(names, set(FREE_TOOLS) | {"delegate"})


class StrangerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def strangers(self, answer="Привет!", **changes):
        fake = FakeGemini(*([[{"text": answer}]] * 10))
        return gemini.Strangers(make_cfg(**changes), self.memory, fake), fake

    def test_note_is_split_off(self):
        text, note = gemini.split_note("Хорошо, передам.\nПЕРЕДАТЬ: Сардор просит перезвонить до 18:00")
        self.assertEqual(text, "Хорошо, передам.")
        self.assertEqual(note, "Сардор просит перезвонить до 18:00")
        self.assertEqual(gemini.split_note("Просто ответ"), ("Просто ответ", ""))

    def test_contents_start_with_user_and_alternate(self):
        contents = gemini.to_contents([("model", "я первый"), ("user", "а"), ("user", "б"), ("model", "в")])
        self.assertEqual([c["role"] for c in contents], ["user", "model"])
        self.assertEqual(contents[0]["parts"][0]["text"], "а\nб")

    async def test_reply_keeps_history_per_person_and_knows_nothing_of_the_owner(self):
        self.memory.remember("Владелец живёт в Ташкенте")
        self.memory.add("user", "мой пароль от почты 12345")
        s, fake = self.strangers()
        self.assertTrue(s.is_new(5))
        first = await s.reply(5, "Аня", "привет")
        self.assertEqual((first.answer, first.note, first.reaction, first.spam), ("Привет!", "", "", False))
        await s.reply(6, "Боря", "здравствуй")
        await s.reply(5, "Аня", "что ты знаешь о владельце?")
        system, contents, tools = fake.seen[-1]
        self.assertEqual(fake.models_used[-1], FakeGemini.lite_first)  # the best model is saved for the owner
        self.assertIn("ИИ-ассистент", system)
        self.assertIsNone(tools)
        self.assertEqual([c["parts"][0]["text"] for c in contents], ["привет", "Привет!", "что ты знаешь о владельце?"])
        everything = system + str(fake.seen)
        self.assertNotIn("Ташкент", everything)
        self.assertNotIn("12345", everything)
        self.assertNotIn("здравствуй", str(fake.seen[-1]))  # nor another stranger's talk

    async def test_hourly_and_daily_caps(self):
        s, _ = self.strangers(stranger_hourly=2, stranger_daily=3)
        for _ in range(2):
            self.assertIsNotNone(await s.reply(5, "Аня", "эй"))
        self.assertIsNone(await s.reply(5, "Аня", "эй"))  # this person is out for the hour
        self.assertIsNotNone(await s.reply(6, "Боря", "эй"))
        self.assertIsNone(await s.reply(7, "Вова", "эй"))  # everyone is out for the day

    async def test_owner_name_in_persona(self):
        s, fake = self.strangers(owner_name="Ваня")
        await s.reply(5, "Аня", "привет")
        self.assertIn("владелец: Ваня", fake.seen[-1][0])

    async def test_stranger_call_can_only_pass_a_note(self):
        s, _ = self.strangers()
        account = tg_user.UserAccount(make_cfg(), s, FakeGemini(), self.tmp.name)
        persona = account._choose(5)
        self.assertEqual({t["name"] for t in persona.tools}, {"pass_to_owner", "hang_up"})
        self.assertTrue((await persona.run_tool("delegate", {"task": "rm -rf /"})).startswith("Ошибка"))
        self.assertEqual(await persona.run_tool("pass_to_owner", {"note": "перезвони"}), "Передам.")
        self.assertEqual(account.call_notes[5], ["перезвони"])
        self.assertIsNone(account._choose(tg_user.SERVICE))
        self.assertIsNone(account._choose(1))  # the owner's calls need the owner's persona from the bot

    def test_recent_chats(self):
        self.memory.add_peer(5, "Аня", "user", "привет")
        time.sleep(0.01)
        self.memory.add_peer(6, "Боря", "user", "здравствуй")
        self.memory.add_peer(6, "Боря", "model", "добрый день")
        self.assertEqual([(p, n, c) for p, n, _, c in self.memory.peer_chats(10)], [(6, "Боря", 2), (5, "Аня", 1)])


class FakeCalls:
    voice, key, models = "Charon", "k", ["m"]

    def __init__(self):
        self.frames = []

    async def send_frame(self, peer, frame):
        self.frames.append(frame)


class LiveTests(unittest.IsolatedAsyncioTestCase):
    def test_resampling(self):
        tone = (np.sin(np.arange(4800) / 10) * 8000).astype(np.int16).tobytes()
        self.assertEqual(len(calls.down(tone)), len(tone) // 3)
        self.assertEqual(len(calls.up(tone)), len(tone) * 2)
        self.assertEqual(calls.up(b""), b"")
        self.assertLess(np.abs(np.frombuffer(calls.up(calls.down(tone)), np.int16)).max(), 8200)

    def talk(self, run_tool=None):
        return calls.LiveTalk(FakeCalls(), 1, calls.Persona("sys", "Поздоровайся", [{"name": "x"}], run_tool))

    def test_setup_has_voice_tools_and_transcripts(self):
        setup = self.talk().setup("gemini-live")["setup"]
        self.assertEqual(setup["model"], "models/gemini-live")
        self.assertEqual(setup["tools"], [{"functionDeclarations": [{"name": "x"}]}])
        self.assertIn("inputAudioTranscription", setup)
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])

    def test_audio_transcripts_and_interruption(self):
        import base64

        talk = self.talk()
        chunk = base64.b64encode(b"\x01\x00" * 240).decode()
        self.assertIsNone(talk.on_server({"serverContent": {"inputTranscription": {"text": "Какая "}}}))
        talk.on_server({"serverContent": {"inputTranscription": {"text": "погода?"}}})
        talk.on_server({"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": chunk}}]},
                                          "outputTranscription": {"text": "Солнечно."}}})
        self.assertEqual(len(talk.speaker), 240 * 2 * 2)
        talk.on_server({"serverContent": {"interrupted": True}})
        self.assertEqual(len(talk.speaker), 0)  # talked over: the rest is dropped
        self.assertEqual(talk.on_server({"serverContent": {"turnComplete": True}}), "turn")
        self.assertEqual(talk.transcript, [("them", "Какая погода?"), ("jarvis", "Солнечно.")])

    async def test_tool_calls_answer_back(self):
        sent = []

        async def run_tool(name, args):
            return f"сделал {name} {args}"

        talk = self.talk(run_tool)

        async def send(message):
            sent.append(message)

        talk._send = send
        talk.on_server({"toolCall": {"functionCalls": [{"id": "7", "name": "set_reminder", "args": {"a": 1}}]}})
        await asyncio.sleep(0.01)
        answer = sent[0]["toolResponse"]["functionResponses"][0]
        self.assertEqual((answer["id"], answer["name"]), ("7", "set_reminder"))
        self.assertIn("сделал set_reminder", answer["response"]["result"])

    async def test_hang_up_ends_the_talk(self):
        talk = self.talk()
        sent = []

        async def send(message):
            sent.append(message)

        talk._send = send
        talk.task = asyncio.create_task(asyncio.sleep(10))
        with patch.object(calls, "HANG_UP_AFTER", 0.01):
            talk.on_server({"toolCall": {"functionCalls": [{"id": "1", "name": "hang_up"}]}})
            await asyncio.sleep(0.05)
        self.assertTrue(talk.ending)
        self.assertTrue(talk.task.cancelled())

    async def test_player_paces_frames(self):
        talk = self.talk()
        talk.speaker += b"\0" * calls.FRAME * 10
        player = asyncio.create_task(talk._player())
        await asyncio.sleep(0.05)
        player.cancel()
        self.assertTrue(4 <= len(talk.calls.frames) <= 10)
        self.assertTrue(all(len(f) == calls.FRAME for f in talk.calls.frames))


class VoiceTests(unittest.TestCase):
    def test_cost_footer_not_spoken(self):
        self.assertEqual(voice._speakable("Готово.\n\n($0.02)"), "Готово.")

    def test_sentences_for_tts(self):
        self.assertEqual(voice.sentences("Привет, Джарвис. Какая погода?\n- Солнце!\n\n($0.01)"),
                         ["Привет, Джарвис.", "Какая погода?", "- Солнце!"])
        self.assertEqual(voice.sentences("```код```"), [])


class FakeTelethon:
    def __init__(self):
        self.sent = []

    async def send_message(self, peer, text, **kwargs):
        self.sent.append(text)


class UserChatTests(unittest.IsolatedAsyncioTestCase):
    async def test_yes_no_in_words(self):
        chat = UserChat(FakeTelethon(), 1)
        ask = asyncio.create_task(chat.confirm("Удалить файл?"))
        await asyncio.sleep(0)
        self.assertFalse(chat.answer("а что за файл?"))  # not an answer: goes on as a normal message
        self.assertTrue(chat.answer("Да!"))
        self.assertTrue(await ask)
        self.assertFalse(chat.answer("да"))  # nothing pending any more

    async def test_no_and_refuse(self):
        chat = UserChat(FakeTelethon(), 1)
        ask = asyncio.create_task(chat.confirm("Удалить?"))
        await asyncio.sleep(0)
        self.assertTrue(chat.answer("нет"))
        self.assertFalse(await ask)
        ask = asyncio.create_task(chat.confirm("Удалить?"))
        await asyncio.sleep(0)
        chat.refuse_all()
        self.assertFalse(await ask)


if __name__ == "__main__":
    unittest.main()
