"""Jarvis's own Telegram account without Telegram, Gemini or real calls: phrase cutting,
the stranger conversation rules and the yes/no answers in words.

Run from server/: python -m unittest discover -s tests
"""
import asyncio
import dataclasses
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from jarvis import calls, config, gemini, voice
from jarvis.memory import Memory
from jarvis.transport import UserChat

ENV = {"OWNER_TG_ID": "1", "TG_BOT_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}


def make_cfg(**changes):
    with patch.dict(os.environ, ENV, clear=True):
        cfg = config.load()
    return dataclasses.replace(cfg, **{"gemini_key": "g", **changes})


def tone(ms, amplitude=3000, rate=calls.RATE):
    t = np.arange(rate * ms // 1000) / rate
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype(np.int16).tobytes()


def quiet(ms, rate=calls.RATE):
    return (np.random.default_rng(0).normal(0, 20, rate * ms // 1000)).astype(np.int16).tobytes()


class EndpointerTests(unittest.TestCase):
    def feed(self, ep, pcm, chunk=1234):
        out = []
        for i in range(0, len(pcm), chunk):  # odd chunk sizes, like the network gives
            out += ep.feed(pcm[i:i + chunk])
        return out

    def test_phrase_between_silences(self):
        ep = calls.Endpointer()
        phrases = self.feed(ep, quiet(500) + tone(1000) + quiet(1000))
        self.assertEqual(len(phrases), 1)
        seconds = len(phrases[0]) / 2 / calls.RATE
        self.assertGreater(seconds, 1.0)  # the start is kept thanks to the pre-roll
        self.assertLess(seconds, 1.6)

    def test_two_phrases(self):
        ep = calls.Endpointer()
        self.assertEqual(len(self.feed(ep, tone(600) + quiet(1000) + tone(600) + quiet(1000))), 2)

    def test_short_pause_keeps_one_phrase(self):
        ep = calls.Endpointer()
        self.assertEqual(len(self.feed(ep, tone(500) + quiet(300) + tone(500) + quiet(1000))), 1)

    def test_clicks_and_silence_are_not_phrases(self):
        ep = calls.Endpointer()
        self.assertEqual(self.feed(ep, quiet(2000) + tone(80) + quiet(1000) + tone(100) + quiet(1500)), [])

    def test_unfinished_phrase_waits(self):
        ep = calls.Endpointer()
        self.assertEqual(self.feed(ep, tone(700) + quiet(300)), [])
        self.assertEqual(len(self.feed(ep, quiet(800))), 1)

    def test_talking_flag_for_interrupting(self):
        ep = calls.Endpointer()
        self.feed(ep, quiet(300) + tone(200))
        self.assertFalse(ep.talking)
        self.feed(ep, tone(400))
        self.assertTrue(ep.talking)

    def test_steady_noise_is_not_endless_speech(self):
        ep = calls.Endpointer()
        hum = tone(12_000, amplitude=600)
        phrases = self.feed(ep, hum + quiet(1000))
        # The bar rises with steady noise: no 30-second «phrase» of hum
        self.assertTrue(all(len(p) / 2 / calls.RATE < 12 for p in phrases))

    def test_bye_words(self):
        for text in ("Ладно, пока!", "Отбой", "всё, до связи.", "Клади трубку"):
            self.assertTrue(calls.BYE.search(text), text)
        for text in ("пока не знаю", "Сделай отчёт", "отбойный молоток нужен"):
            self.assertFalse(calls.BYE.search(text), text)


class FakeCalls:
    def __init__(self, seconds=0.0):
        self.seconds, self.played = seconds, 0

    async def play_pcm(self, peer, pcm):
        self.played += 1
        await asyncio.sleep(self.seconds)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.said, self.heard = [], []

        async def synth(text, rate):
            self.said.append(text)
            return b"\0" * calls.FRAME

        async def recognize(pcm, rate):
            return self.heard.pop(0)

        for name, fake in (("synthesize_pcm", synth), ("transcribe_pcm", recognize)):
            p = patch.object(voice, name, fake)
            p.start()
            self.addCleanup(p.stop)

    async def wait_said(self, count):
        for _ in range(200):
            if len(self.said) >= count:
                return
            await asyncio.sleep(0.01)
        self.fail(f"said only {self.said}")

    async def test_talk_until_bye(self):
        async def reply(text):
            return f"ответ на «{text}»"

        talk = calls.Conversation(FakeCalls(), 1, reply, "Слушаю.", minutes=5)
        task = asyncio.create_task(talk.run())
        self.heard += ["какая погода", "ну ладно, пока"]
        talk.phrases.put_nowait(b"x")
        await self.wait_said(2)
        talk.phrases.put_nowait(b"y")
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.said, ["Слушаю.", "ответ на «какая погода»", "До связи."])
        self.assertEqual(talk.transcript[1], ("them", "какая погода"))

    async def test_slow_answer_gets_a_filler(self):
        async def reply(text):
            await asyncio.sleep(0.2)
            return "готово"

        with patch.object(calls, "FILLER_AFTER", 0.05):
            talk = calls.Conversation(FakeCalls(), 1, reply, "", minutes=5)
            task = asyncio.create_task(talk.run())
            self.heard += ["сделай отчёт"]
            talk.phrases.put_nowait(b"x")
            await self.wait_said(2)
            task.cancel()
        self.assertEqual(self.said, ["Секунду.", "готово"])

    async def test_empty_recognition_is_skipped(self):
        async def reply(text):
            return "ответ"

        talk = calls.Conversation(FakeCalls(), 1, reply, "", minutes=5)
        task = asyncio.create_task(talk.run())
        self.heard += ["", "пока"]
        talk.phrases.put_nowait(b"x")
        await asyncio.sleep(0.05)
        talk.phrases.put_nowait(b"y")
        await asyncio.wait_for(task, 2)
        self.assertEqual(self.said, ["До связи."])

    async def test_interrupting_stops_jarvis(self):
        async def reply(text):
            return "ответ"

        talk = calls.Conversation(FakeCalls(seconds=5), 1, reply, "", minutes=5)
        speaking = asyncio.create_task(talk.say("очень длинная речь"))
        await asyncio.sleep(0.05)
        talk.hear(tone(600))  # the owner starts talking over Jarvis
        await asyncio.wait_for(speaking, 1)

    async def test_out_of_time(self):
        async def reply(text):
            return "ответ"

        talk = calls.Conversation(FakeCalls(), 1, reply, "Слушаю.", minutes=0)
        await asyncio.wait_for(talk.run(), 1)
        self.assertEqual(self.said[-1], "Время звонка вышло, продолжим в переписке.")


class VoiceTests(unittest.TestCase):
    def test_48k_to_16k(self):
        audio = voice.to_16k(tone(1000), 48000)
        self.assertEqual(len(audio), 16000)
        self.assertLess(abs(audio).max(), 1.0)

    def test_cost_footer_not_spoken(self):
        self.assertEqual(voice._speakable("Готово.\n\n($0.02)"), "Готово.")

    def test_sentences_for_tts(self):
        self.assertEqual(voice.sentences("Привет, Джарвис. Какая погода?\n- Солнце!\n\n($0.01)"),
                         ["Привет, Джарвис.", "Какая погода?", "- Солнце!"])
        self.assertEqual(voice.sentences("```код```"), [])


class StrangerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.sent = []

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def strangers(self, answer="Привет!", **changes):
        s = gemini.Strangers(make_cfg(**changes), self.memory)

        async def generate(system, contents):
            self.sent.append((system, contents))
            return answer

        s.generate = generate
        return s

    def test_note_is_split_off(self):
        text, note = gemini.split_note("Хорошо, передам.\nПЕРЕДАТЬ: Сардор просит перезвонить до 18:00")
        self.assertEqual(text, "Хорошо, передам.")
        self.assertEqual(note, "Сардор просит перезвонить до 18:00")
        self.assertEqual(gemini.split_note("Просто ответ"), ("Просто ответ", ""))

    def test_contents_start_with_user_and_alternate(self):
        contents = gemini.to_contents([("model", "я первый"), ("user", "а"), ("user", "б"), ("model", "в")])
        self.assertEqual([c["role"] for c in contents], ["user", "model"])
        self.assertEqual(contents[0]["parts"][0]["text"], "а\nб")

    async def test_reply_keeps_history_per_person(self):
        s = self.strangers()
        self.assertTrue(s.is_new(5))
        self.assertEqual(await s.reply(5, "Аня", "привет"), ("Привет!", ""))
        await s.reply(6, "Боря", "здравствуй")
        await s.reply(5, "Аня", "как дела?")
        system, contents = self.sent[-1]
        self.assertIn("ИИ-ассистент", system)
        self.assertEqual([c["parts"][0]["text"] for c in contents], ["привет", "Привет!", "как дела?"])
        self.assertFalse(s.is_new(5))

    async def test_call_phrases_are_marked(self):
        s = self.strangers()
        await s.reply(5, "Аня", "алло", spoken=True)
        self.assertTrue(self.sent[-1][1][-1]["parts"][0]["text"].startswith(gemini.CALL_MARK))

    async def test_hourly_and_daily_caps(self):
        s = self.strangers(stranger_hourly=2, stranger_daily=3)
        for _ in range(2):
            self.assertIsNotNone(await s.reply(5, "Аня", "эй"))
        self.assertIsNone(await s.reply(5, "Аня", "эй"))  # this person is out for the hour
        self.assertIsNotNone(await s.reply(6, "Боря", "эй"))
        self.assertIsNone(await s.reply(7, "Вова", "эй"))  # everyone is out for the day

    async def test_no_key_no_reply(self):
        s = self.strangers(gemini_key="")
        self.assertIsNone(await s.reply(5, "Аня", "привет"))
        self.assertEqual(self.sent, [])

    async def test_owner_name_in_persona(self):
        s = self.strangers(owner_name="Ваня")
        await s.reply(5, "Аня", "привет")
        self.assertIn("владелец: Ваня", self.sent[-1][0])

    def test_recent_chats(self):
        self.memory.add_peer(5, "Аня", "user", "привет")
        time.sleep(0.01)
        self.memory.add_peer(6, "Боря", "user", "здравствуй")
        self.memory.add_peer(6, "Боря", "model", "добрый день")
        self.assertEqual([(p, n, c) for p, n, _, c in self.memory.peer_chats(10)], [(6, "Боря", 2), (5, "Аня", 1)])


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
        self.assertFalse(chat.answer("а что за файл?"))  # not an answer: goes to the brain as usual
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
