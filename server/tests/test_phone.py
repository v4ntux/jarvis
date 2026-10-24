"""SMS, robot calls and alarm escalation with SMSC mocked out: nothing is sent or paid for."""
import asyncio
import dataclasses
import os
import tempfile
import unittest
from unittest.mock import patch

from jarvis import config, phone
from jarvis.memory import Memory

ENV = {"OWNER_TG_ID": "1", "TG_BOT_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}


def make_cfg(**changes):
    with patch.dict(os.environ, ENV, clear=True):
        cfg = config.load()
    base = dict(owner_phone="79990001122", smsc_login="jarvis", smsc_password="pw", phone_daily_limit=3)
    return dataclasses.replace(cfg, **{**base, **changes})


class PhoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.sent = []

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def phone(self, reply=None, **changes):
        p = phone.Phone(make_cfg(**changes), self.memory)

        async def post(data):
            self.sent.append(data)
            return reply or {"id": 1, "cnt": 1, "cost": "3.5", "balance": "96.5"}

        p._post = post
        return p

    async def test_not_configured_says_what_is_missing_and_sends_nothing(self):
        p = self.phone(owner_phone="")
        self.assertFalse(p.ready)
        with self.assertRaisesRegex(phone.PhoneError, "OWNER_PHONE"):
            await p.sms("x")
        self.assertEqual(self.sent, [])

    async def test_sms_goes_only_to_the_owner_and_is_counted(self):
        out = await self.phone().sms("привет" * 100)
        [data] = self.sent
        self.assertEqual((data["phones"], data["login"], data["psw"]), ("79990001122", "jarvis", "pw"))
        self.assertEqual((data["charset"], data["fmt"], data["cost"]), ("utf-8", 3, 3))
        self.assertEqual(len(data["mes"]), phone.MAX_SMS)
        self.assertNotIn("call", data)
        self.assertIn("96.5", out)
        self.assertEqual(self.memory.phone_since(0), (1, 3.5))

    async def test_call_uses_the_robot_voice_and_api_key_beats_password(self):
        await self.phone(smsc_api_key="key123", smsc_voice="m2").call("пожар")
        [data] = self.sent
        self.assertEqual((data["call"], data["voice"], data["apikey"]), (1, "m2", "key123"))
        self.assertNotIn("psw", data)

    async def test_smsc_error_is_reported_and_not_counted(self):
        p = self.phone(reply={"error": "insufficient funds", "error_code": 3})
        with self.assertRaisesRegex(phone.PhoneError, "код 3"):
            await p.sms("x")
        self.assertEqual(self.memory.phone_since(0), (0, 0))

    async def test_daily_cap_stops_runaway_watchers(self):
        p = self.phone()
        for _ in range(3):
            await p.sms("x")
        with self.assertRaisesRegex(phone.PhoneError, "лимит"):
            await p.call("x")
        self.assertEqual(len(self.sent), 3)


class RingTests(unittest.IsolatedAsyncioTestCase):
    async def run_ring(self, answer_after):
        heard = asyncio.Event()
        log = []

        async def remind():
            log.append("voice")

        async def escalate():
            log.append("phone")

        if answer_after is not None:
            asyncio.get_running_loop().call_later(answer_after, heard.set)
        answered = await phone.ring_until_heard(heard, remind, escalate, repeats=6, every=0.1,
                                                escalate_after=0.25)
        return answered, log

    async def test_answer_in_time_means_no_phone(self):
        answered, log = await self.run_ring(0.15)
        self.assertTrue(answered)
        self.assertEqual(log, ["voice", "voice"])

    async def test_silence_escalates_once_and_keeps_reminding(self):
        answered, log = await self.run_ring(None)
        self.assertFalse(answered)
        self.assertEqual(log.count("phone"), 1)
        self.assertEqual(log.count("voice"), 6)
        self.assertEqual(log.index("phone"), 3)  # after the third unanswered reminder


if __name__ == "__main__":
    unittest.main()
