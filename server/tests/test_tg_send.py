"""Sending messages, photos and files to people from Jarvis's own account, without Telegram.

Run from server/: python -m unittest discover -s tests
"""
import dataclasses
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from jarvis import brain as brain_module
from jarvis import config, shell, tg_user
from jarvis.brain import Brain
from jarvis.memory import Memory

ENV = {"OWNER_TG_ID": "1", "ANTHROPIC_API_KEY": "k", "HANDS_TOKEN": "h"}
SARDOR = SimpleNamespace(id=1000000003, first_name="Сардор", last_name=None, username="frozen_user")
KAMRON = SimpleNamespace(id=1000000004, first_name="Kamron", last_name="Karimov", username="kamron_user")


class NameTests(unittest.TestCase):
    def test_names_match_across_cases_and_alphabets(self):
        for query in ("Сардор", "Сардору", "сардора", "Sardor", "sardoru", "frozen"):
            self.assertTrue(tg_user.name_matches(query, SARDOR), query)
        for query in ("Камрон", "Камрону", "Kamron Karimov", "Каримов", "kamron_user"):
            self.assertTrue(tg_user.name_matches(query, KAMRON), query)
        for query in ("Сардор Каримов", "Сар", "Ваня", ""):
            self.assertFalse(tg_user.name_matches(query, SARDOR), query)


class FakeHub:
    online, routines = True, []

    def __init__(self):
        self.calls = []

    async def call(self, tool, args, timeout=60):
        self.calls.append((tool, args))
        if tool == "send_screen":
            return {"text": "ok", "file": "c2NyZWVu", "name": "screen.png"}  # b"screen"
        if args["path"].endswith("missing.jpg"):
            return {"text": "Ошибка: Файла нет."}
        return {"text": "ok", "file": "cGhvdG8=", "name": os.path.basename(args["path"].replace("\\", "/"))}


class FakeAccount:
    ready = True
    display = staticmethod(tg_user.display)

    def __init__(self, found=SARDOR):
        self.found, self.sent = found, []

    async def find_user(self, to):
        return self.found

    async def send_to(self, user, text, files=None):
        self.sent.append((user, text, files))
        return "Отправил."


class Session:
    def __init__(self, yes=True):
        self.yes, self.asked = yes, []

    async def progress(self, line):
        pass

    async def confirm(self, question):
        self.asked.append(question)
        return self.yes

    async def send_file(self, name, data):
        pass


class SendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.inbox, self.work = os.path.join(root, "inbox"), os.path.join(root, "work")
        os.makedirs(self.inbox)
        os.makedirs(self.work)
        with open(os.path.join(self.inbox, "photo_5.jpg"), "wb") as f:
            f.write(b"jpeg")
        with open(os.path.join(self.inbox, "photo_6.jpg"), "wb") as f:
            f.write(b"jpeg2")
        with open(os.path.join(self.work, "report.pdf"), "wb") as f:
            f.write(b"pdf")
        with open(os.path.join(root, ".env"), "w") as f:
            f.write("ANTHROPIC_API_KEY=secret")
        os.symlink(os.path.join(root, ".env"), os.path.join(self.work, "innocent.jpg"))
        for target, name, value in ((brain_module, "INBOX", self.inbox), (shell, "WORK", self.work)):
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        with patch.dict(os.environ, ENV, clear=True):
            cfg = config.load()
        self.memory = Memory(os.path.join(root, "j.db"))
        self.hub = FakeHub()
        self.brain = Brain(dataclasses.replace(cfg, db_path=os.path.join(root, "j.db")), self.memory, self.hub)
        self.account = self.brain.tg = FakeAccount()

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def send(self, session=None, **args):
        return (await self.brain._run_tool("tg_send", args, session or Session()))["text"]

    async def test_photo_from_inbox_names_the_exact_person(self):
        session = Session()
        self.assertEqual(await self.send(session, to="Сардору", text="Смотри", file="photo_5.jpg"), "Отправил.")
        self.assertIn("Сардор (@frozen_user), id 1000000003", session.asked[0])
        self.assertIn("фото photo_5.jpg", session.asked[0])
        user, text, files = self.account.sent[0]
        self.assertEqual((user, text, files), (SARDOR, "Смотри", [("photo_5.jpg", b"jpeg")]))

    async def test_a_name_the_owner_said_needs_no_question(self):
        session = Session()
        session.request = "отправь Сардору это фото"
        self.assertEqual(await self.send(session, to="Сардор", file="photo_5.jpg"), "Отправил.")
        self.assertEqual(session.asked, [])
        self.assertEqual(len(self.account.sent), 1)

    async def test_a_name_from_earlier_in_the_talk_counts_too(self):
        self.memory.add("user", "Сардор просил фотки с вчера")
        session = Session()
        await self.send(session, to="Сардор", file="photo_5.jpg")
        self.assertEqual(session.asked, [])

    async def test_a_name_nobody_said_still_asks(self):
        session = Session()
        session.request = "отправь это фото"
        await self.send(session, to="Сардор", file="photo_5.jpg")
        self.assertIn("Это имя ты не называл", session.asked[0])

    async def test_album_of_several_photos(self):
        await self.send(to="Сардор", file="photo_5.jpg, photo_6.jpg")
        self.assertEqual([n for n, _ in self.account.sent[0][2]], ["photo_5.jpg", "photo_6.jpg"])

    async def test_inbox_name_cannot_climb_out(self):
        self.assertTrue((await self.send(to="Сардор", file="../.env")).startswith("Ошибка"))
        self.assertEqual(self.account.sent, [])

    async def test_server_secrets_never_go_to_people(self):
        for path in ("../.env", os.path.join(self.tmp.name, ".env"), "/etc/passwd", "innocent.jpg"):
            result = await self.send(to="Сардор", file=path, file_from="server")
            self.assertTrue(result.startswith("Ошибка"), path)
        self.assertEqual(self.account.sent, [])

    async def test_server_work_file(self):
        await self.send(to="Сардор", file="report.pdf", file_from="server")
        self.assertEqual(self.account.sent[0][2], [("report.pdf", b"pdf")])

    async def test_laptop_file_and_screen(self):
        await self.send(to="Сардор", file="C:\\Users\\van\\Pictures\\cat.jpg", file_from="laptop")
        self.assertEqual(self.account.sent[0][2], [("cat.jpg", b"photo")])
        await self.send(to="Сардор", file_from="laptop_screen", text="вот экран")
        self.assertEqual(self.account.sent[1][2], [("screen.png", b"screen")])
        self.assertEqual(self.hub.calls[-1], ("send_screen", {}))
        self.assertTrue((await self.send(to="Сардор", file="D:\\missing.jpg", file_from="laptop")).startswith("Ошибка"))

    async def test_declined_or_unknown_person_or_nothing_sends_nothing(self):
        self.assertTrue((await self.send(Session(yes=False), to="Сардор", text="привет")).startswith("Ошибка"))
        self.account.found = "Ошибка: «Боря» нет среди моих чатов и контактов."
        self.assertIn("Боря", await self.send(to="Боря", text="привет"))
        self.account.found = SARDOR
        self.assertTrue((await self.send(to="Сардор")).startswith("Ошибка"))
        self.assertEqual(self.account.sent, [])

    async def test_plain_message_still_works(self):
        session = Session()
        await self.send(session, to="Сардор", text="Буду в 18:00")
        self.assertEqual(self.account.sent[0], (SARDOR, "Буду в 18:00", []))
        self.assertIn("сообщение", session.asked[0])


class FakeClient:
    def __init__(self):
        self.files, self.messages = [], []

    async def send_file(self, user, file, **kwargs):
        self.files.append((file, kwargs))

    async def send_message(self, user, text, **kwargs):
        self.messages.append(text)


class AccountSendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.memory = Memory(os.path.join(self.tmp.name, "j.db"))
        with patch.dict(os.environ, ENV, clear=True):
            cfg = config.load()
        self.account = tg_user.UserAccount(cfg, SimpleNamespace(memory=self.memory), None, self.tmp.name)
        self.account.client = self.client = FakeClient()

    async def asyncTearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    async def test_album_photo_document_and_history(self):
        await self.account.send_to(SARDOR, "смотри", [("a.jpg", b"1"), ("b.png", b"2")])
        album, kwargs = self.client.files[0]
        self.assertEqual([f.name for f in album], ["a.jpg", "b.png"])
        self.assertEqual((kwargs["caption"], kwargs["force_document"]), ("смотри", False))
        await self.account.send_to(SARDOR, "", [("report.pdf", b"3")])
        single, kwargs = self.client.files[1]
        self.assertEqual((single.name, kwargs["force_document"], kwargs["caption"]), ("report.pdf", True, None))
        self.assertEqual(self.memory.peer_history(SARDOR.id, 5),
                         [("model", "[фото a.jpg, b.png] смотри"), ("model", "[файл report.pdf]")])


if __name__ == "__main__":
    unittest.main()
