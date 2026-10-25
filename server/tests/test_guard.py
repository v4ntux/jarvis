"""Telegram's limits, the chat model in front of strangers and groups, monitoring and what still needs a yes,
all without Telegram, Claude or a network.

Run from server/: python -m unittest discover -s tests
"""
import asyncio
import dataclasses
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anthropic
import httpx
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl import functions, types

from jarvis import chat, config, monitor, pace
from jarvis.brain import Brain
from jarvis.gemini import GeminiError, Strangers
from jarvis.memory import Memory

ENV = {"OWNER_TG_ID": "1", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}
OWNER = 1


def make_cfg(**changes):
    with patch.dict(os.environ, ENV, clear=True):
        cfg = config.load()
    return dataclasses.replace(cfg, **{"gemini_key": "g", **changes})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


def send(peer):
    return functions.messages.SendMessageRequest(peer=peer, message="x")


USER = types.InputPeerUser(user_id=OWNER, access_hash=1)
STRANGER = types.InputPeerUser(user_id=7, access_hash=1)
GROUP = types.InputPeerChat(chat_id=55)


class PaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = pace.PacedClient(StringSession(), 1, "hash")
        self.client.pace = pace.Pace(OWNER, clock=self.clock)
        self.answers, self.sent_at = [], []

        async def call(sender, request, ordered=False, flood_sleep_threshold=None):
            self.sent_at.append((type(request).__name__, self.clock.now))
            answer = self.answers.pop(0) if self.answers else "ok"
            if isinstance(answer, Exception):
                raise answer
            return answer

        self.client._call = call
        self.client.pace.sleep = self.clock.sleep

    async def test_messages_to_one_chat_are_spaced_out(self):
        for peer in (USER, USER, GROUP, GROUP):
            await self.client(send(peer))
        times = [t for _, t in self.sent_at]
        self.assertGreaterEqual(times[1] - times[0], pace.GAP_PRIVATE)
        self.assertEqual(times[2], times[1])  # another chat does not wait for this one
        self.assertGreaterEqual(times[3] - times[2], pace.GAP_GROUP)

    async def test_no_more_than_the_minute_allows_in_total(self):
        for _ in range(pace.PER_MINUTE + 1):  # the owner's messages wait their turn however long it is
            await self.client(send(USER))
        first, last = self.sent_at[0][1], self.sent_at[-1][1]
        self.assertGreaterEqual(last - first, 60)

    async def test_a_flood_wait_is_waited_out_for_the_owner_and_nothing_is_lost(self):
        self.answers = [FloodWaitError(request=None, capture=200), "delivered"]
        self.assertEqual(await self.client(send(USER)), "delivered")
        self.assertGreaterEqual(self.sent_at[1][1] - self.sent_at[0][1], 200)
        self.assertEqual(self.client.pace.floods, 1)

    async def test_others_do_not_wait_minutes_and_everyone_respects_the_pause(self):
        self.answers = [FloodWaitError(request=None, capture=200)]
        with self.assertRaises(FloodWaitError):
            await self.client(send(GROUP))
        with self.assertRaises(pace.Throttled):  # Telegram is not asked again during the pause
            await self.client(send(STRANGER))
        self.assertEqual(len(self.sent_at), 1)
        self.assertIn("пауза", self.client.pace.status())

    async def test_typing_and_reactions_are_dropped_not_queued(self):
        self.client.pace.cool_until["extra"] = self.clock.now + 100
        typing = functions.messages.SetTypingRequest(peer=USER, action=types.SendMessageTypingAction())
        self.assertTrue(await self.client(typing))
        self.assertEqual(self.sent_at, [])

    async def test_edits_of_one_message_are_spaced(self):
        edit = functions.messages.EditMessageRequest(peer=USER, id=5, message="a")
        await self.client(edit)
        await self.client(edit)
        self.assertGreaterEqual(self.sent_at[1][1] - self.sent_at[0][1], pace.GAP_EDIT)

    async def test_other_requests_pass_untouched(self):
        await self.client(functions.users.GetFullUserRequest(id=types.InputUserSelf()))
        self.assertEqual(self.client.pace.windows["send"], type(self.client.pace.windows["send"])())


class FakeMessages:
    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def create(self, **kwargs):
        self.seen.append(kwargs)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def claude_says(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, stop_details=None,
                           usage=SimpleNamespace(input_tokens=1000, output_tokens=200, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=0))


class FakeGemini:
    ready, lite_first = True, ["lite"]

    def __init__(self, answer="от Gemini"):
        self.answer, self.seen = answer, []

    async def text(self, system, contents, max_tokens=2048, models=None, **_):
        self.seen.append(contents)
        return self.answer


class ChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def chat(self, *answers, **changes):
        c = chat.Chat(make_cfg(**changes), self.memory, FakeGemini())
        c.client = SimpleNamespace(messages=FakeMessages(*answers))
        return c

    async def test_claude_answers_thinks_and_is_counted_as_chat(self):
        c = self.chat(claude_says("Ну ты и клоун 🤡"))
        out = await c.text("persona", [{"role": "user", "parts": [{"text": "привет"}]}], effort="medium")
        self.assertEqual(out, "Ну ты и клоун 🤡")
        sent = c.client.messages.seen[0]
        self.assertEqual(sent["model"], "claude-haiku-5-5")
        self.assertEqual(sent["output_config"], {"effort": "medium"})
        self.assertGreater(sent["max_tokens"], 1024)  # room to think on top of the answer
        self.assertGreater(self.memory.spent_since(0, chat.USAGE_PREFIX), 0)
        self.assertEqual(c.gemini.seen, [])

    async def test_claude_down_gemini_answers(self):
        c = self.chat(anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
        self.assertEqual(await c.text("p", [{"role": "user", "parts": [{"text": "hi"}]}]), "от Gemini")

    async def test_video_goes_to_gemini_pictures_to_claude(self):
        c = self.chat(claude_says("годнота"))
        video = [{"role": "user", "parts": [{"text": "[видео]"}, {"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}}]}]
        self.assertEqual(await c.text("p", video), "от Gemini")
        jpeg = "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
        photo = [{"role": "user", "parts": [{"text": "[фото]"}, {"inlineData": {"mimeType": "image/jpeg", "data": jpeg}}]}]
        self.assertEqual(await c.text("p", photo), "годнота")
        blocks = c.client.messages.seen[0]["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image"])

    async def test_a_broken_picture_is_left_out_instead_of_failing(self):
        c = self.chat(claude_says("ок"))
        fake = [{"role": "user", "parts": [{"text": "[стикер]"}, {"inlineData": {"mimeType": "image/webp", "data": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}}]}]
        await c.text("p", fake)
        self.assertEqual([b["type"] for b in c.client.messages.seen[0]["messages"][0]["content"]], ["text"])

    async def test_a_refusal_is_silence_not_another_model(self):
        c = self.chat(claude_says("", stop="refusal"))
        self.assertEqual(await c.text("p", [{"role": "user", "parts": [{"text": "x"}]}]), "")
        self.assertEqual(c.gemini.seen, [])

    async def test_todays_chat_budget_spent_means_gemini(self):
        self.memory.log_usage(chat.USAGE_PREFIX + "claude-haiku-5-5", 1, 1, 5.0)
        c = self.chat(claude_says("не должен"))
        self.assertEqual(await c.text("p", [{"role": "user", "parts": [{"text": "x"}]}]), "от Gemini")
        self.assertEqual(c.client.messages.seen, [])

    async def test_nobody_at_all(self):
        c = self.chat(anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com")))
        c.gemini.ready = False
        with self.assertRaises(GeminiError):
            await c.text("p", [{"role": "user", "parts": [{"text": "x"}]}])

    def test_history_becomes_claude_turns(self):
        msgs = chat.to_messages([{"role": "user", "parts": [{"text": "a"}]}, {"role": "model", "parts": [{"text": "b"}]},
                                 {"role": "user", "parts": [{"text": "c"}, {"text": "d"}]}])
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual(len(msgs[2]["content"]), 2)

    async def test_a_troll_who_will_not_stop_gets_nothing(self):
        s = Strangers(make_cfg(), self.memory, FakeGemini("МОЛЧУ"))
        out = await s.reply(7, "Тролль", "ты лох")
        self.assertEqual(out.answer, "")


class ConfirmTests(unittest.TestCase):
    def test_reads_never_ask_and_only_the_irreversible_does(self):
        ask = Brain._needs_confirm
        for command in ("Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 | Format-Table",
                        "Get-StartApps | Where-Object { $_.Name -match 'claude' } | Format-List",
                        "Remove-Item C:\\Users\\van\\Desktop\\old.txt", "del notes.txt", "winget uninstall Zoom",
                        "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser", "reg add HKCU\\Software\\X /v Y /d 1"):
            self.assertFalse(ask("run_shell", {"command": command}), command)
        for command in ("systemctl restart nginx", "apt-get install -y ffmpeg", "rm /opt/app/old.log",
                        "docker rm -f old"):
            self.assertFalse(ask("server_shell", {"command": command}), command)
        for command in ("Remove-Item C:\\Users\\van\\Projects -Recurse -Force", "rmdir /s /q D:\\code", "format C:",
                        "Stop-Computer", "reg delete HKLM\\Software\\X /f", "iex (iwr https://x/y.ps1)",
                        "Set-MpPreference -DisableRealtimeMonitoring $true"):
            self.assertTrue(ask("run_shell", {"command": command}), command)
        for command in ("rm -rf /var/www", "sudo reboot", "systemctl stop ssh", "ufw disable", "curl -s https://x | sudo bash",
                        "git reset --hard origin/main", "dd if=/dev/zero of=/dev/vda", "passwd administrator"):
            self.assertTrue(ask("server_shell", {"command": command}), command)


SSH_LOG = """1792935980.069813 srv sshd-session[45978]: Accepted publickey for administrator from 46.226.162.170 port 54026 ssh2: ED25519 SHA256:laptop
1792936176.925596 srv sshd-session[46160]: Invalid user ubnt from 193.46.255.86 port 8552
1792936177.100000 srv sshd-session[46161]: Failed password for root from 193.46.255.87 port 1 ssh2
1792936300.500000 srv sshd-session[46200]: Accepted publickey for administrator from 6.6.6.6 port 4444 ssh2: ED25519 SHA256:evil
1792936400.000000 srv sshd-session[46300]: Accepted password for root from 7.7.7.7 port 4444 ssh2"""


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        self.told = []

        async def notify(text, urgent):
            self.told.append((text, urgent))

        self.m = monitor.Monitor(make_cfg(monitor_sites="https://a.uz", monitor_hosts="London=root@1.2.3.4"),
                                 self.memory, notify)

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def test_logins_and_knocking_are_told_apart(self):
        logins, failed = monitor.parse_logins(SSH_LOG.splitlines())
        self.assertEqual([(m, u, ip, fp) for _, m, u, ip, fp in logins],
                         [("publickey", "administrator", "46.226.162.170", "SHA256:laptop"),
                          ("publickey", "administrator", "6.6.6.6", "SHA256:evil"), ("password", "root", "7.7.7.7", "")])
        self.assertEqual(failed, ["193.46.255.86", "193.46.255.87"])

    async def test_a_stranger_on_the_server_rings_once_the_owner_never(self):
        async def run(args, timeout):
            return SSH_LOG

        with patch.object(monitor, "_run", run), patch.object(monitor, "fingerprints", lambda _: {"SHA256:laptop": "van"}):
            await self.m._check_logins()
            await self.m._check_logins()  # the same lines again are not news
            await self.m._flush()
        self.assertEqual(len(self.told), 1)
        text, urgent = self.told[0]
        self.assertTrue(urgent)
        self.assertIn("6.6.6.6", text)
        self.assertIn("7.7.7.7", text)
        self.assertNotIn("46.226.162.170", text)

    async def test_a_new_key_in_authorized_keys_is_a_call(self):
        with patch.object(monitor, "fingerprints", lambda _: {"SHA256:laptop": "van"}):
            self.m._known_keys()
        with patch.object(monitor, "fingerprints", lambda _: {"SHA256:laptop": "van", "SHA256:new": "x@evil"}):
            self.m._known_keys()
        await self.m._flush()
        self.assertTrue(self.told[0][1])
        self.assertIn("x@evil", self.told[0][0])

    async def test_a_site_is_down_after_two_fails_and_back_once(self):
        t = monitor.Target("https://a.uz")
        self.m._went(t, False, "Сайт a.uz", "ConnectTimeout")
        self.assertEqual(self.m.pending, [])  # one blip is not news
        self.m._went(t, False, "Сайт a.uz", "ConnectTimeout")
        self.m._went(t, False, "Сайт a.uz", "ConnectTimeout")
        self.m._went(t, True, "Сайт a.uz", "HTTP 200")
        await self.m._flush()
        self.assertEqual(len(self.told), 1)
        self.assertIn("🔴", self.told[0][0])
        self.assertIn("🟢", self.told[0][0])

    def test_sites_added_and_removed_in_words_survive(self):
        self.m.add_site("nvate.uz")
        self.assertEqual(self.m.site_list(), ["https://a.uz", "https://nvate.uz"])
        self.m.remove_site("a.uz")
        self.assertEqual(self.m.site_list(), ["https://nvate.uz"])
        self.assertTrue(self.m.remove_site("nothing.uz").startswith("Ошибка"))

    async def test_inside_the_other_vps(self):
        status = ("host london\nload 0.5 2\nmem 2000000 50000\ndisk / 95\ndisk /boot 20\nfailed nginx.service\n"
                  "docker bot exited\ndocker db running\npm2 karta online\nfails24 812\n"
                  "log 1792936400.000000 l sshd[1]: Accepted password for root from 7.7.7.7 port 1 ssh2\n")
        st = monitor.parse_status(status)
        self.assertEqual(st["disk"], {"/": 95, "/boot": 20})
        self.assertEqual(st["failed"], ["nginx.service"])
        self.assertEqual(st["down"], ["docker bot: exited"])
        self.assertEqual(st["fails24"], 812)

        async def run(args, timeout):
            return status

        with patch.object(monitor, "_run", run), patch.object(monitor, "fingerprints", lambda _: {}), \
                patch.object(monitor.os.path, "exists", lambda _: True):
            target = monitor.Target("London")
            await self.m._look_inside("London", "root@1.2.3.4", target)
            await self.m._flush()
        text = self.told[0][0]
        for piece in ("диск", "памят", "nginx.service", "docker bot", "7.7.7.7"):
            self.assertIn(piece, text)
        self.assertEqual(target.extra["access"], "есть")

    async def test_no_key_there_yet_shows_how_to_add_it(self):
        async def run(args, timeout):
            return "root@1.2.3.4: Permission denied (publickey,password)."

        with patch.object(monitor, "_run", run), patch.object(monitor.os.path, "exists", lambda _: True):
            target = self.m.hosts.setdefault("London", monitor.Target("London"))
            target.checked = time.time()
            await self.m._look_inside("London", "root@1.2.3.4", target)
        self.assertIn("london_setup.sh", self.m.status())
        self.assertEqual(self.told, [])


if __name__ == "__main__":
    unittest.main()


class MoreTests(unittest.IsolatedAsyncioTestCase):
    """What came out of the owner's second complaint: lag, repeats, a dumb «я не вижу фото»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def test_a_long_wait_for_one_message_does_not_hold_up_the_others(self):
        clock = Clock()
        p = pace.Pace(OWNER, clock=clock)
        p.cool_until["send"] = clock.now + 200
        self.assertGreater(p.delay("send", OWNER, False), 199)
        async with p.lock:
            p.book("send", OWNER, 200)
        self.assertFalse(p.lock.locked())  # the owner's message waits outside the lock

    async def test_a_status_line_is_skipped_when_it_would_be_late(self):
        client = pace.PacedClient(StringSession(), 1, "hash")
        client.pace = pace.Pace(OWNER)
        client.pace.cool_until["send"] = client.pace.clock() + 100
        with self.assertRaises(pace.Throttled), pace.at_most(3):
            await client(send(USER))

    async def test_a_stranger_can_start_a_new_chat(self):
        fake = FakeGemini("ок")
        s = Strangers(make_cfg(), self.memory, fake)
        await s.reply(7, "Ромбик", "порезы на руках")
        out = await s.reply(7, "Ромбик", "Жарвис, новый чат")
        self.assertIn("чистый лист", out.answer)
        await s.reply(7, "Ромбик", "йоу")
        texts = [p["text"] for c in fake.seen[-1] for p in c["parts"] if "text" in p]
        self.assertFalse(any("порезы" in t for t in texts))
        self.assertEqual(len(self.memory.peer_history(7, 50)), 6)  # the owner still sees everything

    def test_his_own_code_and_restart_ask_first(self):
        ask = Brain._needs_confirm
        for command in ("sudo systemctl restart jarvis.service", "cd /opt/jarvis && sed -i 's/a/b/' jarvis/tg_user.py",
                        "python3 - <<EOF\nopen('/opt/jarvis/jarvis/brain.py','w').write('x')\nEOF",
                        "cp /tmp/x.py /opt/jarvis/jarvis/tools.py", "pkill -f 'python -m jarvis'"):
            self.assertTrue(ask("server_shell", {"command": command}), command)
        for command in ("cat /opt/jarvis/jarvis/tools.py", "sed -n 1,40p /opt/jarvis/jarvis/brain.py",
                        "systemctl status jarvis", "journalctl -u jarvis -n 50", "grep -n def /opt/jarvis/jarvis/brain.py"):
            self.assertFalse(ask("server_shell", {"command": command}), command)

    def test_a_photo_from_a_moment_ago_is_seen_again(self):
        from jarvis import brain as brain_module

        with patch.object(brain_module, "INBOX", self.tmp.name):
            jpeg = b"\xff\xd8\xff" + b"x" * 30
            with open(os.path.join(self.tmp.name, "photo_5.jpg"), "wb") as f:
                f.write(jpeg)
            messages = [{"role": "user", "content": "[фото] мем\n[фото сохранено в inbox как photo_5.jpg]"},
                        {"role": "assistant", "content": "Классика"},
                        {"role": "user", "content": "ага, и ты не пались"}]
            brain_module._recall_photo(messages)
        self.assertEqual(messages[0]["content"][0]["type"], "image")
        self.assertEqual(messages[2]["content"], "ага, и ты не пались")

    def test_markdown_is_made_plain(self):
        from jarvis import style

        self.assertEqual(style.plain("**Гайд:** факт `#11`\n### Итог\nок"), "Гайд: факт #11\nИтог\nок")

    def test_questions_are_talk_requests_are_work(self):
        from jarvis import autonomy

        for text in ("Eto kogda nado skinut?", "зачем ты звонил?", "как открыть блокнот?"):
            self.assertFalse(autonomy.looks_like_task(text), text)
        for text in ("Скинь гифки", "Можешь скинуть решение?", "открой блокнот"):
            self.assertTrue(autonomy.looks_like_task(text), text)

    def test_a_burst_of_gifs_is_one_request(self):
        from jarvis.__main__ import LOOK_AT, together

        gif = [{"inlineData": {"mimeType": "video/mp4", "data": "AAAA"}}]
        batch = [{"chat": None, "text": "[гифка]", "images": [], "spoken": False, "media": gif} for _ in range(16)]
        batch.append({"chat": None, "text": "ору", "images": [], "spoken": True, "media": []})
        text, images, spoken, media = together(batch)
        self.assertTrue(text.startswith("[гифка] ×16\nору"))
        self.assertIn("первые", text)
        self.assertEqual(len(media), LOOK_AT)
        self.assertTrue(spoken)
        self.assertEqual(together([{"chat": None, "text": "привет", "images": [], "spoken": False, "media": []}]),
                         ("привет", [], False, []))


class TalkModelTests(unittest.IsolatedAsyncioTestCase):
    """Plain talk with the owner goes to the model that gets his jokes; work stays on the cheap one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def ask(self, text, **changes):
        from jarvis import brain as brain_module

        b = brain_module.Brain(make_cfg(**changes), self.memory, SimpleNamespace(online=False, routines=[]))
        b.client = SimpleNamespace(messages=FakeMessages(claude_says("ха, база")))

        async def nothing(*_):
            return True

        answer = await b.reply(text, brain_module.Session(nothing, nothing, nothing))
        return b.client.messages.seen[0]["model"], answer

    async def test_talk_on_sonnet_work_on_haiku(self):
        self.assertEqual((await self.ask("ага, ты тоже не палься"))[0], "claude-sonnet-5-5")
        self.assertEqual((await self.ask("открой блокнот"))[0], "claude-haiku-5-5")

    async def test_talk_budget_spent_means_the_cheap_one(self):
        from jarvis.brain import TALK_PREFIX

        self.memory.log_usage(TALK_PREFIX + "claude-sonnet-5-5", 1, 1, 1.5)  # over the talk budget, under the day
        self.assertEqual((await self.ask("как дела"))[0], "claude-haiku-5-5")

    async def test_no_price_under_a_joke(self):
        self.assertNotIn("$", (await self.ask("как дела", prices=((100.0, 100.0), (100.0, 100.0))))[1])
