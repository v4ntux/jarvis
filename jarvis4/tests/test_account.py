"""The Telegram account without Telegram: messages shaped like Telethon's go in, Incoming comes out for the core.

These paths only ever ran live before; a missing helper here would have silently dropped every sticker and photo."""
import asyncio
import datetime as dt
import os
import tempfile
import unittest

from telethon.tl.types import User

from jarvis4.config import Config
from jarvis4.ports import TGError
from jarvis4.store import Store
from jarvis4.tg import account as account_mod
from jarvis4.tg import media as media_mod
from jarvis4.tg.account import Account

OWNER = 1000000001


def run(coro):
    return asyncio.run(coro)


class File:
    def __init__(self, **kw):
        self.emoji, self.mime_type, self.size, self.name, self.ext, self.duration = "", "", 1000, None, "", 0
        self.__dict__.update(kw)


class Msg:
    """What the code reads of a Telethon message; unset kinds are None, as in Telethon."""

    def __init__(self, id=1, text="", sender=OWNER, chat=OWNER, out=False, **kw):
        self.id, self.raw_text, self.sender_id, self.chat_id, self.out = id, text, sender, chat, out
        self.date = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.timezone.utc)
        self.grouped_id, self.mentioned, self.is_reply, self.reply_to_msg_id = None, False, False, None
        self.sticker = self.photo = self.gif = self.video = self.video_note = self.voice = self.audio = None
        self.document = self.file = None
        self.replied, self.data = None, b"BYTES"
        self.__dict__.update(kw)

    async def download_media(self, file=None, thumb=None):
        if isinstance(file, str):
            with open(file, "wb") as f:
                f.write(self.data)
            return file
        return self.data

    async def get_reply_message(self):
        return self.replied


class Event:
    def __init__(self, message, sender=None, group=False, title="Пацаны"):
        self.message, self._sender, self.is_group, self.is_private = message, sender, group, not group
        self.sender_id, self.chat_id, self._title = message.sender_id, message.chat_id, title

    async def get_sender(self):
        return self._sender

    async def get_chat(self):
        return type("Chat", (), {"title": self._title})()


class Client:
    def __init__(self):
        self.read = []

    async def send_read_acknowledge(self, who, msg):
        self.read.append(msg.id)


class Core:
    def __init__(self):
        self.got = []

    async def on_message(self, inc):
        self.got.append(inc)


class Gem:
    ready = False

    async def transcribe(self, data, mime):
        return "привет, это голосом"

    async def describe(self, parts):
        return "злой котик в очках"


def user(id, first="Камрон", username="kamron_x", bot=False):
    return User(id=id, first_name=first, username=username, bot=bot)


class AccountTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._inbox = account_mod.INBOX
        account_mod.INBOX = self.tmp
        self.store = Store(":memory:")
        self.acc = Account(Config(owner_id=OWNER, db_path=":memory:"), self.store, Gem())
        self.acc.core, self.acc.client, self.acc.me_id = Core(), Client(), 999
        self.told = []

        async def tell(text):
            self.told.append(text)
        self.acc.tell_owner = tell

    def tearDown(self):
        account_mod.INBOX = self._inbox

    def feed(self, event):
        run(self.acc._on_message(event))
        return self.acc.core.got

    def test_the_owners_text_arrives_as_the_owners(self):
        got = self.feed(Event(Msg(7, "привет"), user(OWNER, "Ван")))
        self.assertEqual((got[0].chat, got[0].sender, got[0].text, got[0].ref), ("owner", "owner", "привет", "owner/7"))
        self.assertFalse(got[0].group)

    def test_a_sticker_is_a_sticker_with_its_emoji_and_is_remembered_for_the_chat(self):
        sticker = type("Doc", (), {"id": 4242})()
        m = Msg(8, sticker=sticker, file=File(emoji="😡️", mime_type="image/webp"))
        got = self.feed(Event(m, user(OWNER, "Ван")))
        self.assertEqual(got[0].media, {"kind": "sticker", "emoji": "😡"})
        self.assertEqual(self.store.top_stickers("owner"), [("😡", 1)])

    def test_with_gemini_the_sticker_gets_a_description(self):
        self.acc.gem.ready = True
        real = media_mod.look

        async def fake_look(m, limit):
            return "[стикер]", [{"inlineData": {}}]
        media_mod.look = fake_look
        try:
            sticker = type("Doc", (), {"id": 1})()
            got = self.feed(Event(Msg(9, sticker=sticker, file=File(emoji="😡")), user(OWNER)))
        finally:
            media_mod.look = real
        self.assertEqual(got[0].media["about"], "злой котик в очках")

    def test_every_kind_of_attachment_is_named(self):
        cases = [("photo", dict(photo=object(), file=File())), ("gif", dict(gif=object(), file=File())),
                 ("video", dict(video=object(), file=File(duration=5))), ("circle", dict(video_note=object(), file=File())),
                 ("voice", dict(voice=object(), file=File())), ("file", dict(document=object(), file=File(name="a.zip"))),
                 ("photo", dict(document=object(), file=File(name="x.png", mime_type="image/png")))]
        for kind, attrs in cases:
            self.assertEqual(media_mod.kind_of(Msg(**attrs))["kind"], kind)
        self.assertIsNone(media_mod.kind_of(Msg(text="просто текст")))

    def test_an_owners_photo_is_saved_and_shown_to_claude(self):
        m = Msg(10, "что это", photo=object(), file=File(), data=b"\xff\xd8\xff\xe0jpeg")
        got = self.feed(Event(m, user(OWNER)))
        self.assertEqual(len(got[0].images), 1)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "photo_10.jpg")))
        self.assertIn("[фото сохранено в inbox как photo_10.jpg]", got[0].text)

    def test_a_voice_message_is_heard(self):
        self.acc.gem.ready = True
        m = Msg(11, voice=object(), file=File(mime_type="audio/ogg", duration=3))
        got = self.feed(Event(m, user(OWNER)))
        self.assertEqual(got[0].text, "привет, это голосом")
        self.assertEqual(got[0].media, {"kind": "voice"})

    def test_a_voice_nobody_can_hear_is_dropped(self):
        class Deaf(Gem):
            ready = True

            async def transcribe(self, data, mime):
                from jarvis4.gem import GeminiError
                raise GeminiError("квота")
        self.acc.gem = Deaf()
        async def no_whisper(path):
            return ""
        from jarvis4.ops import voice
        real = voice.transcribe
        voice.transcribe = no_whisper
        try:
            got = self.feed(Event(Msg(12, voice=object(), file=File(mime_type="audio/ogg")), user(OWNER)))
        finally:
            voice.transcribe = real
        self.assertEqual(got, [])

    def test_a_small_text_file_is_read_into_the_message(self):
        m = Msg(13, "глянь", document=object(), file=File(name="notes.txt", mime_type="text/plain", ext=".txt"),
                data="строка один\nстрока два".encode())
        got = self.feed(Event(m, user(OWNER)))
        self.assertIn("строка два", got[0].text)
        self.assertEqual(got[0].media["kind"], "file")

    def test_a_huge_file_is_refused_silently(self):
        m = Msg(14, document=object(), file=File(name="big.iso", size=500 * 1024 * 1024))
        self.assertEqual(self.feed(Event(m, user(OWNER))), [])

    def test_a_stranger_is_a_person_with_a_key_and_the_chat_is_marked_read(self):
        got = self.feed(Event(Msg(1, "здарова", sender=777, chat=777), user(777, "Сардор", "sardor_user")))
        self.assertEqual((got[0].chat, got[0].sender), ("u777", "u777"))
        person = self.store.person("u777")
        self.assertEqual((person.name, person.username, person.dialog), ("Сардор", "sardor_user", True))
        self.assertEqual(self.acc.client.read, [1])

    def test_bots_and_telegram_service_messages_are_ignored(self):
        self.assertEqual(self.feed(Event(Msg(2, "код 12345", sender=777000, chat=777000), user(777000, "Telegram"))), [])
        self.assertEqual(self.feed(Event(Msg(3, "я бот", sender=55, chat=55), user(55, "Bot", "x_bot", bot=True))), [])

    def test_a_blocked_person_is_ignored(self):
        self.store.upsert_person("u777", "Спамер", tg_id=777, blocked=True)
        self.assertEqual(self.feed(Event(Msg(1, "купи", sender=777, chat=777), user(777, "Спамер"))), [])

    def test_a_group_message_gets_a_group_chat_and_the_owner_hears_of_a_new_group_once(self):
        m = Msg(5, "джарвис ты тут?", sender=42, chat=-100123, mentioned=True)
        got = self.feed(Event(m, user(42, "Зак", "zac_user"), group=True, title="Пацаны"))
        self.assertEqual((got[0].chat, got[0].sender, got[0].group, got[0].mentioned), ("g100123", "u42", True, True))
        self.assertEqual(self.store.chat("g100123"), ("group", "Пацаны", "on"))
        self.assertEqual(self.told, ["👥 Теперь я в группе «Пацаны»"])
        self.feed(Event(Msg(6, "ещё", sender=42, chat=-100123), user(42, "Зак", "zac_user"), group=True))
        self.assertEqual(len(self.told), 1)

    def test_the_owner_in_a_group_is_the_owner(self):
        got = self.feed(Event(Msg(5, "Ливай с группы", sender=OWNER, chat=-100123), user(OWNER, "Ван"), group=True))
        self.assertEqual(got[0].sender, "owner")

    def test_a_reply_brings_the_message_it_answers_into_the_store(self):
        original = Msg(100, "📨 zac просит передать", sender=999, chat=OWNER, out=True)
        m = Msg(101, "скинь этот стикер", is_reply=True, reply_to_msg_id=100)
        m.replied = original
        got = self.feed(Event(m, user(OWNER)))
        self.assertEqual(got[0].reply_ref, "owner/100")
        held = self.store.msg("owner/100")
        self.assertEqual((held.sender, held.text), ("jarvis", "📨 zac просит передать"))

    def test_history_never_overwrites_what_jarvis_already_holds(self):
        self.store.add_msg("owner/100", "owner", "jarvis", "моя записка", relay={"chat": "u1", "msgs": ["u1/5"]})
        run(self.acc.remember(Msg(100, "моя записка", out=True), "owner"))
        self.assertEqual(self.store.msg("owner/100").relay, {"chat": "u1", "msgs": ["u1/5"]})

    def test_an_album_of_photos_is_one_request(self):
        msgs = [Msg(20 + i, "подпись" if i == 0 else "", photo=object(), file=File(), data=b"\xff\xd8\xff") for i in range(3)]

        class AlbumEvent:
            is_private, sender_id, messages = True, OWNER, msgs
        run(self.acc._on_album(AlbumEvent()))
        got = self.acc.core.got
        self.assertEqual(len(got), 1)
        self.assertEqual(len(got[0].images), 3)
        self.assertIn("альбом: 3 шт.", got[0].text)

    def test_parts_of_an_album_are_left_to_the_album_handler(self):
        self.assertEqual(self.feed(Event(Msg(30, photo=object(), file=File(), grouped_id=777), user(OWNER))), [])

    def test_a_failure_on_the_owners_message_is_reported_to_him(self):
        async def broken(inc):
            raise RuntimeError("что-то сломалось")
        self.acc.core.on_message = broken
        self.feed(Event(Msg(40, "привет"), user(OWNER)))
        self.assertEqual(len(self.told), 1)
        self.assertIn("споткнулся на твоём сообщении", self.told[0])
        self.told.clear()
        self.feed(Event(Msg(41, "привет", sender=777, chat=777), user(777)))
        self.assertEqual(self.told, [])  # a stranger's lost message is not the owner's business

    def test_refs_round_trip(self):
        self.assertEqual(Account.parse(Account.ref("g123", 45)), ("g123", 45))
        self.assertEqual(self.acc.person_key(OWNER), "owner")
        self.assertEqual(self.acc.person_key(77), "u77")
        self.assertEqual(Account.group_key(-1001234), "g1001234")


class DryRunTest(unittest.TestCase):
    """The rehearsal mode must never reach Telegram: there is no client here, so any real call would crash."""

    def setUp(self):
        self.acc = Account(Config(owner_id=OWNER, db_path=":memory:", dry_run=True), Store(":memory:"), Gem())

    def test_nothing_is_sent_but_everything_answers(self):
        acc = self.acc
        self.assertTrue(run(acc.send("owner", "привет")).startswith("dry/"))
        self.assertTrue(run(acc.sticker("owner", "😂")).startswith("dry/"))
        self.assertTrue(run(acc.gif("owner", "cat")).startswith("dry/"))
        self.assertEqual(len(run(acc.forward(["u1/5", "u1/6"], "owner"))), 2)
        self.assertIsNone(run(acc.react("owner/1", "👍")))
        self.assertIsNone(run(acc.edit("owner/1", "x")))
        self.assertIsNone(run(acc.delete("owner/1")))
        self.assertIn("репетиция", run(acc.leave("g1")))
        result = run(acc.call("owner", "say", "привет"))
        self.assertTrue(result.answered)
        self.assertIn("репетиция", result.text)

    def test_without_calls_a_call_is_an_honest_error(self):
        acc = Account(Config(owner_id=OWNER, db_path=":memory:"), Store(":memory:"), Gem())
        with self.assertRaises(TGError):
            run(acc.call("owner", "say", "привет"))


if __name__ == "__main__":
    unittest.main()
