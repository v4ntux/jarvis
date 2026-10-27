"""Jarvis 4.2: media downloads for everybody, the whole Telegram account, stickers checked for fit, his own dictionary."""
import asyncio
import json
import os
import random
import tempfile
import time
import unittest
from unittest import mock

from jarvis4 import context, learn
from jarvis4.config import Config
from jarvis4.core import Core, Incoming
from jarvis4.ops import grab
from jarvis4.ports import Platform
from jarvis4.store import Store
from jarvis4.testing import ScriptedLLM, say
from jarvis4.tg.taste import Taste
from jarvis4.tools import Ctx, all_tools
from jarvis4.types import Interaction, Msg


class Out:
    def __init__(self, yes=True):
        self.said, self.asked, self.yes = [], [], yes

    async def say(self, text, reply_ref=""):
        self.said.append(text)
        return f"o/{len(self.said)}"

    async def ask(self, q):
        self.asked.append(q)
        return self.yes

    async def progress(self, line):
        pass

    async def send_file(self, name, data):
        pass

    async def typing(self):
        pass

    async def finish(self):
        pass


class Tg:
    """The Telegram port as the tools see it, remembering what was done."""

    def __init__(self):
        self.sent, self.reacted, self.done, self.admins = [], [], [], set()

    async def send(self, chat, text, files=None, reply_ref=""):
        self.sent.append((chat, text, [n for n, _ in files or []], reply_ref))
        return f"{chat}/{100 + len(self.sent)}"

    async def react(self, ref, emoji):
        self.reacted.append((ref, emoji))

    async def lookup(self, query):
        return f"Ошибка: «{query}» не нашёл."

    async def join(self, link):
        self.done.append(("join", link))
        return "g77", "Мемы", "channel"

    async def block(self, person, on=True):
        self.done.append(("block" if on else "unblock", person))

    async def create_chat(self, title, about="", channel=True, members=None, username=""):
        self.done.append(("create", title, channel, members))
        return "g88", "https://t.me/+abc"

    async def delete_chat(self, chat):
        self.done.append(("delete", chat))

    async def is_admin(self, chat, person):
        return person in self.admins

    async def moderate(self, chat, action, person="", msg_id=0, text="", minutes=0):
        self.done.append((action, chat, person, msg_id, text, minutes))
        return "https://t.me/+link" if action == "link" else "сделал"

    async def set_profile(self, first_name="", last_name=None, bio=None, username="", photo=""):
        self.done.append(("profile", first_name, bio))
        return "обновил: имя/о себе"

    async def leave(self, chat):
        self.done.append(("leave", chat))
        return "вышел"


def build(script=None, yes=True):
    store = Store(":memory:")
    cfg = Config(owner_id=1, db_path=":memory:", anthropic_key="")
    tg, out = Tg(), Out(yes)
    core = Core(cfg, store, Platform(tg=tg, server=None, web=None), llm=ScriptedLLM(script or [], store),
                outs=lambda chat: out)
    store.upsert_person("u5", "Камрон", "kamron_x", 5, dialog=True)
    store.upsert_person("u6", "Спамер", "spammer", 6)
    store.chat_seen("g1", "group", "Пацаны", -1001)
    store.chat_seen("g2", "channel", "Мой канал", -1002)
    return core, store, tg, out


def itx(text="сделай", owner=True, kind="owner", chat="owner", sender="owner", msgs=None, out=None):
    return Interaction(kind=kind, chat=chat, sender=sender, text=text, msgs=msgs or [], owner=owner, out=out or Out())


class Call:
    def __init__(self, name, args):
        self.name, self.input = name, args


# ---- downloads ---------------------------------------------------------------------------------------------------------

class LinksTest(unittest.TestCase):
    def test_links_of_known_sites_are_found(self):
        self.assertEqual(grab.link_in("глянь https://vm.tiktok.com/ZMabc/ ахах"), "https://vm.tiktok.com/ZMabc/")
        self.assertTrue(grab.link_in("https://www.instagram.com/reel/C1x2y3/?igsh=1"))
        self.assertTrue(grab.link_in("https://pin.it/4abcDEF"))
        self.assertTrue(grab.link_in("https://youtu.be/dQw4w9WgXcQ"))
        self.assertEqual(grab.link_in("https://example.com/video.mp4"), "")

    def test_only_a_bare_link_is_downloaded_without_asking(self):
        self.assertTrue(grab.bare_link("https://www.tiktok.com/@a/video/1"))
        self.assertTrue(grab.bare_link("смотри https://www.tiktok.com/@a/video/1"))
        self.assertEqual(grab.bare_link("а что думаешь про это видео https://youtu.be/x, норм?"), "")
        self.assertEqual(grab.bare_link("просто текст"), "")


class DeliverTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patch = mock.patch.object(grab, "WORK", self.tmp)
        self.patch.start()
        self.store, self.tg = Store(":memory:"), Tg()

    def tearDown(self):
        self.patch.stop()

    async def fake_fetch(self, url, max_mb=0, audio=False, laptop=None):
        folder = os.path.join(self.tmp, "x")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "clip.mp4"), "wb") as f:
            f.write(b"0" * 2048)
        self.max_mb = max_mb
        return folder, [os.path.join(folder, "clip.mp4")], "смешной кот"

    def test_the_file_goes_into_the_chat_and_the_folder_is_cleaned(self):
        with mock.patch.object(grab, "fetch", self.fake_fetch):
            said = asyncio.run(grab.deliver(self.tg, self.store, "g1", "https://youtu.be/x", False, "g1/5"))
        self.assertEqual(self.tg.sent, [("g1", "", ["clip.mp4"], "g1/5")])
        self.assertIn("смешной кот", said)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "x")))
        self.assertEqual(self.max_mb, grab.OTHERS_MB)

    def test_others_get_a_daily_share_and_the_owner_does_not(self):
        with mock.patch.object(grab, "fetch", self.fake_fetch):
            for _ in range(grab.OTHERS_DAILY):
                asyncio.run(grab.deliver(self.tg, self.store, "u5", "https://youtu.be/x", False))
            with self.assertRaises(grab.GrabError):
                asyncio.run(grab.deliver(self.tg, self.store, "u5", "https://youtu.be/x", False))
            for _ in range(grab.OTHERS_DAILY + 2):
                asyncio.run(grab.deliver(self.tg, self.store, "owner", "https://youtu.be/x", True))

    def test_a_site_that_shuts_the_server_out_is_tried_from_the_laptop(self):
        class Laptop:
            online = True

            def __init__(self):
                self.commands = []

            async def shell(self, command, timeout=60):
                self.commands.append(command)
                return "C:\\Users\\van\\AppData\\Local\\Temp\\jarvis-grab\\x\\Funny_cat.mp4"

            async def pull(self, path):
                return "Funny_cat.mp4", b"0" * 1000

        async def blocked(url, folder, max_mb, audio, quality=""):
            return "", "[TikTok] 1: Your IP address is blocked from accessing this post"

        laptop = Laptop()
        with mock.patch.object(grab, "_yt_dlp", blocked):
            folder, files, title = asyncio.run(grab.fetch("https://www.tiktok.com/@a/video/1", 50, laptop=laptop))
            self.assertEqual([os.path.basename(f) for f in files], ["Funny_cat.mp4"])
            self.assertEqual(title, "Funny cat")
            self.assertIn("--max-filesize 19M", laptop.commands[0])
            self.assertIn("Remove-Item", laptop.commands[-1])  # the laptop's copy is cleaned up
            grab.cleanup(folder)
            laptop.online = False
            with self.assertRaises(grab.GrabError) as e:
                asyncio.run(grab.fetch("https://www.tiktok.com/@a/video/1", 50, laptop=laptop))
            self.assertIn("не на связи", str(e.exception))

    def test_yt_dlp_is_asked_for_a_playable_720p_or_mp3(self):
        args = grab._args("https://youtu.be/x", "/tmp/f", 50, False)
        self.assertIn("res:720,ext:mp4:m4a", args)
        self.assertIn("50M", args)
        self.assertIn("mp3", grab._args("https://youtu.be/x", "/tmp/f", 50, True))


class AutoGrabTest(unittest.TestCase):
    def test_a_bare_link_from_anybody_comes_back_as_a_video_without_the_model(self):
        core, store, tg, out = build()  # an empty script: any model call would fail the test
        got = []

        async def fake_deliver(tg_, store_, chat, url, owner, reply="", audio=False, laptop=None):
            got.append((chat, url, owner, reply))
            return "Скачал"

        async def go():
            with mock.patch.object(grab, "deliver", fake_deliver):
                await core.on_message(Incoming(ref="g1/7", chat="g1", sender="u5", text="https://vm.tiktok.com/ZMabc/",
                                               group=True))
                await asyncio.gather(*core.background)

        asyncio.run(go())
        self.assertEqual(got, [("g1", "https://vm.tiktok.com/ZMabc/", False, "g1/7")])
        self.assertEqual(core.llm.calls, [])

    def test_a_failed_download_in_a_group_is_a_shrug_not_a_message(self):
        core, store, tg, out = build()

        async def broken(*a, **k):
            raise grab.GrabError("приватное видео")

        async def go():
            with mock.patch.object(grab, "deliver", broken):
                await core.on_message(Incoming(ref="g1/8", chat="g1", sender="u5", text="https://youtu.be/x", group=True))
                await asyncio.gather(*core.background)

        asyncio.run(go())
        self.assertEqual(tg.reacted, [("g1/8", "🤷")])
        self.assertEqual(out.said, [])

    def test_an_ignored_person_is_not_answered(self):
        core, store, tg, out = build()
        store.set_ignored("u6")

        async def go():
            await core.on_message(Incoming(ref="u6/1", chat="u6", sender="u6", text="привет, ответь"))
            await asyncio.gather(*core.background)

        asyncio.run(go())
        self.assertEqual(out.said, [])
        self.assertIsNotNone(store.msg("u6/1"))  # still kept


# ---- the account -------------------------------------------------------------------------------------------------------

class AccountToolsTest(unittest.TestCase):
    def setUp(self):
        self.core, self.store, self.tg, self.out = build()
        self.tools = all_tools()

    def use(self, tool, it, **args):
        """Through the core: guard, run, journal, the way the model's calls go."""
        return asyncio.run(self.core.run_tool(self.tools[tool], Call(tool, args), it))

    def test_join_block_and_ignore(self):
        self.assertTrue(self.use("tg_join", itx("вступи"), link="https://t.me/+abc").ok)
        result = self.use("tg_block", itx("заблокируй спамера"), who="Спамер")
        self.assertTrue(result.ok, result.text)
        self.assertIn(("block", "u6"), self.tg.done)
        self.assertIn("u6", self.store.ignored())
        self.use("tg_block", itx("разблокируй спамера"), who="Спамер", off=True)
        self.assertNotIn("u6", self.store.ignored())
        self.assertTrue(self.use("tg_ignore", itx("игнорь камрона"), who="Камрон").ok)
        self.assertIn("u5", self.store.ignored())
        self.assertFalse(self.use("tg_block", itx("заблокируй себя"), who="Ван").ok)

    def test_deleting_a_channel_asks_the_owner_once(self):
        it = itx("удали мой канал")
        result = self.use("tg_delete_chat", it, chat="Мой канал")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(len(it.out.asked), 1)
        self.assertIn(("delete", "g2"), self.tg.done)
        no = itx("удали мой канал", out=Out(yes=False))
        self.assertFalse(self.use("tg_delete_chat", no, chat="Мой канал").ok)

    def test_a_new_group_with_somebody_the_owner_did_not_name_is_asked_about(self):
        it = itx("сделай группу Тест")
        self.use("tg_create", it, kind="group", title="Тест", members=["Камрон"])
        self.assertEqual(len(it.out.asked), 1)
        named = itx("сделай группу Тест с Камроном")
        self.assertTrue(self.use("tg_create", named, kind="group", title="Тест", members=["Камрон"]).ok)
        self.assertEqual(named.out.asked, [])
        self.assertIn(("create", "Тест", False, ["u5"]), self.tg.done)

    def test_posting_into_a_channel(self):
        result = self.use("tg_post", itx("запости в канал привет"), chat="Мой канал", text="привет")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.tg.sent[-1][:2], ("g2", "привет"))

    def group_itx(self, sender, text, reply_ref=""):
        self.store.add_msg("g1/50", "g1", "u6", "купи крипту", ts=time.time() - 5)
        msg = Msg("g1/51", "g1", sender, time.time(), text, reply_ref=reply_ref)
        return itx(text, owner=sender == "owner", kind="group", chat="g1", sender=sender, msgs=[msg])

    def test_the_owner_bans_the_author_of_the_message_he_answered(self):
        result = self.use("tg_admin", self.group_itx("owner", "забань его", "g1/50"), action="ban")
        self.assertTrue(result.ok, result.text)
        self.assertEqual(self.tg.done[-1][:4], ("ban", "g1", "u6", 0))

    def test_a_group_admin_may_moderate_and_a_member_may_not(self):
        it = self.group_itx("u5", "джарвис, кикни спамера", "g1/50")
        self.assertIn("админов", self.use("tg_admin", it, action="kick").text)
        self.tg.admins.add("u5")
        self.assertTrue(self.use("tg_admin", it, action="kick").ok)
        self.assertIn("Ван", self.use("tg_admin", it, action="title", text="новое").text)  # not for admins

    def test_nobody_moves_against_the_owner(self):
        self.tg.admins.add("u5")
        self.store.add_msg("g1/60", "g1", "owner", "я тут", ts=time.time())
        it = self.group_itx("u5", "забань его", "g1/60")
        result = self.use("tg_admin", it, action="ban")
        self.assertFalse(result.ok)
        self.assertNotIn("ban", [d[0] for d in self.tg.done])

    def test_pin_needs_a_message_of_that_chat(self):
        it = self.group_itx("owner", "закрепи")
        self.assertFalse(self.use("tg_admin", it, action="pin").ok)
        self.assertTrue(self.use("tg_admin", it, action="pin", ref="g1/50").ok)
        self.assertEqual(self.tg.done[-1][:4], ("pin", "g1", "", 50))

    def test_profile_photo_only_from_jarvis_folders(self):
        self.assertFalse(self.use("tg_profile", itx("смени аватарку"), photo="/etc/passwd").ok)
        self.assertTrue(self.use("tg_profile", itx("смени био"), bio="живу на сервере").ok)

    def test_others_may_download_and_teach_words_but_not_run_the_account(self):
        names = {t.name for t in self.tools.values() if t.others}
        self.assertTrue({"media_download", "slang_learn", "tg_admin", "tg_sticker"} <= names)
        self.assertFalse(names & {"tg_join", "tg_block", "tg_create", "tg_delete_chat", "tg_post", "tg_profile"})


# ---- stickers and GIFs -------------------------------------------------------------------------------------------------

class Doc:
    def __init__(self, i):
        self.id, self.mime_type, self.size = i, "image/webp", 100


class Client:
    async def download_media(self, doc, file=None, thumb=None):
        return b"RIFF0000WEBPframe" + bytes([doc.id])


class Gem:
    ready = True

    def __init__(self, answer):
        self.answer, self.seen = answer, []

    async def fitting(self, frames, about):
        self.seen.append((len(frames), about))
        return self.answer


class FitTest(unittest.TestCase):
    def taste(self, gem):
        return Taste(Client(), Store(":memory:"), rng=random.Random(1), gem=gem)

    def test_gemini_picks_one_of_the_fitting(self):
        gem = Gem([2, 0])
        i = asyncio.run(self.taste(gem)._pick([Doc(1), Doc(2), Doc(3)], "ржу с шутки", 3))
        self.assertIn(i, (2, 0))
        self.assertEqual(gem.seen, [(3, "ржу с шутки")])

    def test_nothing_is_sent_when_nothing_fits(self):
        self.assertIsNone(asyncio.run(self.taste(Gem([]))._pick([Doc(1), Doc(2)], "грусть", 2)))

    def test_without_eyes_a_random_one_of_the_first(self):
        i = asyncio.run(self.taste(None)._pick([Doc(1), Doc(2), Doc(3)], "ржу", 2))
        self.assertIn(i, (0, 1))

    def test_frames_are_downloaded_once(self):
        taste = self.taste(Gem([0]))
        asyncio.run(taste._pick([Doc(1)], "ржу", 1))
        taste.client = None  # a second download would fail
        self.assertEqual(asyncio.run(taste._pick([Doc(1)], "ржу", 1)), 0)

    def test_the_prompt_nudges_a_sticker_after_a_dry_spell(self):
        store = Store(":memory:")
        for i in range(3):
            store.add_msg(f"g1/{i}", "g1", "jarvis", "текст", ts=time.time() - 10 + i)
        self.assertFalse(context.media_lately(store, "g1"))
        store.add_msg("g1/9", "g1", "jarvis", "", {"kind": "gif"}, ts=time.time())
        self.assertTrue(context.media_lately(store, "g1"))


# ---- his own dictionary ------------------------------------------------------------------------------------------------

class SlangTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_known_words_are_in_the_prompt_the_ones_of_the_moment_first(self):
        self.store.slang_learn("имба", "очень сильная штука", "это имба")
        self.store.slang_learn("краш", "тот, кто нравится")
        block = learn.dictionary(self.store, "g1", "ну краш же")
        self.assertIn("имба — очень сильная штука", block)
        self.assertLess(block.index("краш"), block.index("имба"))

    def test_an_unclear_word_is_asked_once_then_quiet_until_answered(self):
        for _ in range(3):
            self.store.count_words("g1", ["шкибиди"])
        self.store.slang_mark("шкибиди", "ask")
        self.assertIn("шкибиди", learn.dictionary(self.store, "g1"))
        self.assertNotIn("шкибиди", learn.dictionary(self.store, "g1", chatty=False))  # not in work
        learn.said_myself(self.store, "g1", "а чё такое шкибиди?")
        self.assertEqual(self.store.slang_to_ask("g1"), [])
        self.store.slang_learn("шкибиди", "мем из тиктока, бессмыслица")
        self.assertEqual(self.store.slang("шкибиди")["status"], "known")

    def test_his_own_use_is_counted(self):
        self.store.slang_learn("имба", "очень сильная штука")
        learn.said_myself(self.store, "g1", "это имба, имба полная")
        self.assertEqual(self.store.slang("имба")["uses"], 1)

    def test_a_plain_word_never_comes_back_as_a_candidate(self):
        for _ in range(4):
            self.store.count_words("g1", ["смотри", "вайб"])
        self.assertEqual(set(self.store.slang_candidates()), {"смотри", "вайб"})
        self.store.slang_mark("смотри", "plain")
        self.assertEqual(self.store.slang_candidates(), ["вайб"])
        self.store.slang_mark("смотри", "plain")
        self.store.slang_learn("вайб", "атмосфера")
        self.store.slang_mark("вайб", "ask")  # a word he knows keeps its meaning
        self.assertEqual(self.store.slang("вайб")["status"], "known")

    def test_the_night_review_sorts_new_words(self):
        core, store, tg, out = build([say(json.dumps({
            "summary": "", "lessons": [], "profiles": [],
            "slang": [{"word": "вайб", "meaning": "атмосфера, настроение", "example": "вайб тут топ"},
                      {"word": "левое", "meaning": "не из списка", "example": ""}],
            "plain": ["смотри"], "unclear": ["шкибиди"]}))])
        for i, text in enumerate(["вайб тут топ", "смотри какой вайб", "шкибиди доп доп", "смотри шкибиди",
                                  "вайб смотри шкибиди"]):
            core.ingest(Incoming(ref=f"g1/{i}", chat="g1", sender="u5", text=text, group=True, ts=time.time()))
        from jarvis4.alerts import Alerts
        review = learn.Review(core, Alerts(core, type("A", (), {"dry": lambda s, *a: ""})(), None, store))
        note = asyncio.run(review.run())
        self.assertIn("вайб", note)
        self.assertEqual(store.slang("вайб")["meaning"], "атмосфера, настроение")
        self.assertIsNone(store.slang("левое"))  # not one of the words it was asked about
        self.assertEqual(store.slang("смотри")["status"], "plain")
        self.assertEqual(store.slang_to_ask("g1"), ["шкибиди"])
        prompt = core.llm.calls[0]["messages"][0]["content"]
        self.assertIn("шкибиди доп доп", prompt)  # how the word is used

    def test_people_teach_words_with_the_tool(self):
        core, store, tg, out = build()
        it = itx("кринж это когда стыдно", owner=False, kind="person", chat="u5", sender="u5")
        result = asyncio.run(all_tools()["slang_learn"].run(Ctx(core, it), {"word": "Кринж", "meaning": "испанский стыд"}))
        self.assertTrue(result.ok)
        self.assertEqual(store.slang("кринж")["source"], "Камрон")


# ---- the account's Telegram calls, Telethon-shaped -------------------------------------------------------------------

class TLClient:
    """Records what the account asks of Telethon."""

    def __init__(self):
        self.calls = []

    async def __call__(self, request):
        self.calls.append(type(request).__name__)
        from telethon.tl.types import Channel
        chat = Channel(id=555, title="Новый", photo=None, date=None, broadcast=True, access_hash=1)
        return type("Res", (), {"chats": [chat], "link": "https://t.me/+new"})()

    async def get_input_entity(self, peer):
        return ("peer", peer)

    async def get_entity(self, name):
        from telethon.tl.types import Channel
        return Channel(id=556, title="Мемы", photo=None, date=None, megagroup=True, access_hash=2)

    async def edit_permissions(self, entity, user, until_date=None, **rights):
        self.calls.append(("perm", user, until_date is not None, rights))

    async def kick_participant(self, entity, user):
        self.calls.append(("kick", user))


class ManageTest(unittest.TestCase):
    def setUp(self):
        from jarvis4.tg.account import Account
        self.store = Store(":memory:")
        self.acc = Account(Config(owner_id=1, db_path=":memory:"), self.store, None)
        self.acc.client = TLClient()
        self.store.chat_seen("g1", "group", "Пацаны", -1001)

    def test_an_invite_link_is_imported_and_the_chat_remembered(self):
        key, title, kind = asyncio.run(self.acc.join("https://t.me/+AbCdEf"))
        self.assertIn("ImportChatInviteRequest", self.acc.client.calls)
        self.assertEqual((title, kind), ("Новый", "channel"))
        self.assertEqual(self.store.chat(key)[:2], ("channel", "Новый"))

    def test_a_public_group_is_joined_by_name(self):
        key, title, kind = asyncio.run(self.acc.join("@memes_chat"))
        self.assertEqual(self.acc.client.calls, ["JoinChannelRequest"])
        self.assertIsNotNone(self.store.chat(key))

    def test_a_ban_for_an_hour_and_a_mute_forever(self):
        asyncio.run(self.acc.moderate("g1", "ban", "u6", minutes=60))
        asyncio.run(self.acc.moderate("g1", "mute", "u6"))
        ban, mute = self.acc.client.calls
        self.assertEqual(ban, ("perm", ("peer", 6), True, {"view_messages": False}))
        self.assertEqual(mute, ("perm", ("peer", 6), False, {"send_messages": False}))

    def test_a_new_channel_gets_an_invite_link(self):
        key, link = asyncio.run(self.acc.create_chat("Новый", "про мемы"))
        self.assertEqual(link, "https://t.me/+new")
        self.assertEqual(self.acc.client.calls, ["CreateChannelRequest", "ExportChatInviteRequest"])
        self.assertEqual(self.store.chat(key)[0], "channel")

    def test_block_goes_to_telegram(self):
        asyncio.run(self.acc.block("u6"))
        asyncio.run(self.acc.block("u6", on=False))
        self.assertEqual(self.acc.client.calls, ["BlockRequest", "UnblockRequest"])


if __name__ == "__main__":
    unittest.main()
