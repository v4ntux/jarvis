"""Jarvis's own Telegram account (Telethon).

The owner talks to Jarvis here (free Gemini first, the Claude brain for real work); everyone
else talks to Gemini alone and never reaches the brain, the tools, the laptop or anything
Jarvis knows about the owner. Calls go through Gemini Live. Groups, channels and bots are ignored.
"""
import base64
import collections
import io
import logging
import os
import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from . import calls, voice
from .config import Config
from .gemini import Gemini, GeminiError, Strangers
from .transport import IMAGE_EXT, UserChat

log = logging.getLogger("jarvis.tg")

SERVICE = 777000  # Telegram's own notices, login codes among them: never answered or passed anywhere
MAX_FILE = 200 * 1024 * 1024
MAX_STRANGER_VOICE = 120  # seconds: longer voice messages from strangers are not worth the quota
MAX_ALBUM_IMAGES = 6  # the model looks at this many pictures of an album; all of them are saved
PHONE = re.compile(r"^\+?\d[\d\s()-]{8,}$")

STRANGER_CALL = """

Сейчас ты говоришь с этим человеком по телефону, голосом: 1–2 короткие фразы, без списков и смайликов.
Если он просит что-то передать владельцу или говорит что-то важное для него — pass_to_owner. Когда попрощались — hang_up."""
STRANGER_TOOLS = [
    {"name": "pass_to_owner", "description": "Передать владельцу короткую записку: кто звонил и что просил.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "hang_up", "description": "Положить трубку: когда попрощались или разговор закончен."},
]

# The owner's message: (chat, text, images, spoken, file path)
OwnerHandler = Callable[[UserChat, str, list[str] | None, bool, str | None], Awaitable[None]]


def make_client(cfg: Config) -> TelegramClient:
    # Jarvis lives here 24/7 with no bot to fall back on: keep reconnecting however long the outage is
    return TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash, device_model="Jarvis Server",
                          system_version="Ubuntu", app_version="3.0", lang_code="ru", system_lang_code="ru-RU",
                          connection_retries=-1, retry_delay=5)


TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                          "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                          "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                          "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya"})


def _name_words(text: str) -> list[str]:
    """Latin letters, endings cut: «Сардору», «Sardor» and «сардор» all become «sardor»."""
    latin = text.lower().translate(TRANSLIT).replace("kh", "h")
    return [re.sub(r"(?<=\w{3})[aeiouy]+$", "", w) for w in re.findall(r"[a-z0-9]+", latin)]


def name_matches(query: str, user) -> bool:
    """Every word asked for is the start of one of the person's names (first, last, username)."""
    wanted = _name_words(query)
    have = _name_words(" ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None),
                                            getattr(user, "username", None)) if p))
    # Short words must match whole: «Сар» should not quietly become Сардор
    return bool(wanted) and all(any(h == w or (len(w) >= 4 and h.startswith(w)) for h in have) for w in wanted)


def display(user) -> str:
    name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p)
    name = name or getattr(user, "title", None) or "без имени"
    if getattr(user, "username", None):
        name += f" (@{user.username})"
    return f"{name}, id {user.id}"


class UserAccount:
    display = staticmethod(display)

    def __init__(self, cfg: Config, strangers: Strangers, gemini: Gemini, inbox: str):
        self.cfg, self.strangers, self.gemini, self.inbox = cfg, strangers, gemini, inbox
        self.client: TelegramClient | None = None  # Telethon refuses an empty api_id, so it is made in start()
        self.calls: calls.Calls | None = None
        self.owner: UserChat | None = None
        self.ready = False
        # Set by the bot: the owner's messages, how Jarvis is in the owner's calls (given the first cue), notes
        self.on_owner: OwnerHandler | None = None
        self.owner_persona: Callable[[str], calls.Persona] | None = None
        self.tell_owner: Callable[[str], Awaitable[None]] | None = None
        # Set by the bot: any word from the owner here counts as «Принял» for a running alarm
        self.on_owner_seen: Callable[[], None] | None = None
        self.call_notes: dict[int, list[str]] = {}
        self._albums_seen: collections.deque = collections.deque(maxlen=200)

    async def start(self) -> bool:
        if not self.cfg.tg_api_id or not self.cfg.tg_api_hash:
            return False
        self.client = make_client(self.cfg)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("own Telegram account is not logged in: run python -m jarvis.tg_login")
            await self.client.disconnect()
            return False
        me = await self.client.get_me()
        self.owner = UserChat(self.client, self.cfg.owner_id)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_album, events.Album())
        if not await self._reach_owner():
            log.warning("cannot reach the owner yet: they should write to %s first", display(me))
        if self.gemini.ready:  # calls are Gemini Live: no key, no calls
            try:
                self.calls = calls.Calls(self.client, self.cfg.gemini_key, self.cfg.live_model, self.cfg.live_voice,
                                         self._choose, self._call_ended, self.cfg.call_minutes)
                await self.calls.start()
            except Exception:
                log.exception("calls are off")
                self.calls = None
        self.ready = True
        log.info("own account %s is up, calls %s, Gemini %s", display(me), "on" if self.calls else "off",
                 "on" if self.gemini.ready else "off")
        return True

    async def _reach_owner(self) -> bool:
        """A fresh account knows nobody: find the owner by phone number if they have not written yet."""
        try:
            await self.client.get_input_entity(self.cfg.owner_id)
            return True
        except ValueError:
            pass
        if not self.cfg.owner_phone:
            return False
        user = await self._import_phone(self.cfg.owner_phone, "Владелец")
        return bool(user and user.id == self.cfg.owner_id)

    async def _import_phone(self, phone: str, name: str):
        from telethon.tl.functions.contacts import ImportContactsRequest
        from telethon.tl.types import InputPhoneContact

        digits = re.sub(r"\D", "", phone)
        res = await self.client(ImportContactsRequest([InputPhoneContact(0, "+" + digits, name, "")]))
        return res.users[0] if res.users else None

    # Messages

    async def _on_message(self, event) -> None:
        # Who is who comes from Telegram's sender id, which nobody can fake; message text never decides it
        if not event.is_private:
            return
        try:
            if event.sender_id == self.cfg.owner_id:
                await self._from_owner(event)
            elif event.sender_id != SERVICE:
                sender = await event.get_sender()
                if sender is not None and not getattr(sender, "bot", False):  # two bots would talk forever
                    await self._from_stranger(event, sender)
        except Exception:
            log.exception("message from %s failed", event.sender_id)

    async def _from_owner(self, event) -> None:
        m = event.message
        text = m.raw_text or ""
        if self.on_owner_seen:
            self.on_owner_seen()
        if m.grouped_id:
            return  # part of an album: _on_album takes all of it at once
        if self.owner.answer(text):
            return
        if m.sticker or m.gif:
            return  # a reaction, not a request worth paying for
        if m.voice or m.audio or m.video_note:
            heard = await self._hear(m)
            if not heard:
                await self.owner.say("Не расслышал, повтори.")
                return
            await self.owner.say(f"🎙 {heard}", quiet=True)
            await self.on_owner(self.owner, heard + (f"\n{text}" if text else ""), None, True, None)
        elif m.photo:
            data = await m.download_media(file=bytes)
            name = f"photo_{m.id}.jpg"
            with open(os.path.join(self.inbox, name), "wb") as f:
                f.write(data)
            await self.on_owner(self.owner, (text or "Что на фото?") + f"\n[фото сохранено в inbox как {name}]",
                                [base64.b64encode(data).decode()], False, None)
        elif m.document:
            if (m.file.size or 0) > MAX_FILE:
                await self.owner.say("Файл больше 200 МБ, такие не принимаю.")
                return
            name = os.path.basename(m.file.name or f"file_{m.id}{m.file.ext or ''}")
            path = os.path.join(self.inbox, name)
            await m.download_media(file=path)
            await self.on_owner(self.owner, text, None, False, path)
        elif text:
            await self.on_owner(self.owner, text, None, False, None)

    async def _on_album(self, event) -> None:
        """Several photos or files sent at once by the owner: one request with all of them."""
        if not event.is_private or event.sender_id != self.cfg.owner_id:
            return
        try:
            names, images = [], []
            for m in event.messages:
                if m.photo:
                    name = f"photo_{m.id}.jpg"
                    data = await m.download_media(file=bytes)
                    with open(os.path.join(self.inbox, name), "wb") as f:
                        f.write(data)
                    images.append(base64.b64encode(data).decode())
                elif m.document and (m.file.size or 0) <= MAX_FILE:
                    name = os.path.basename(m.file.name or f"file_{m.id}{m.file.ext or ''}")
                    await m.download_media(file=os.path.join(self.inbox, name))
                else:
                    continue
                names.append(name)
            if names:
                text = (event.text or ("Что на фото?" if images else "Посмотри файлы.")) + \
                       f"\n[сохранено в inbox: {', '.join(names)}]"
                await self.on_owner(self.owner, text, images[:MAX_ALBUM_IMAGES] or None, False, None)
        except Exception:
            log.exception("album from the owner failed")

    async def _from_stranger(self, event, sender) -> None:
        m, peer, name = event.message, event.sender_id, display(sender)
        if not self.strangers.allowed(peer):
            return  # over the caps: silence, and no note for the owner either
        if m.grouped_id:
            if m.grouped_id in self._albums_seen:
                return  # one answer per album, not one per picture
            self._albums_seen.append(m.grouped_id)
        text, spoken = m.raw_text or "", False
        if m.voice or m.video_note:
            if (m.file.duration or 0) > MAX_STRANGER_VOICE:
                text = "[прислал длинное голосовое, больше двух минут]" + (f" {text}" if text else "")
            else:
                heard = await self._hear(m)
                if not heard:
                    return
                text, spoken = heard + (f"\n{text}" if text else ""), True
        elif m.media:
            text = "[прислал вложение]" + (f" {text}" if text else "")
        if not text.strip():
            return
        if self.strangers.is_new(peer) and self.tell_owner:
            await self.tell_owner(f"👤 Мне впервые написал {name}:\n{text[:500]}")
        await self.client.send_read_acknowledge(peer, m)
        try:
            async with self.client.action(peer, "record-audio" if spoken else "typing"):
                out = await self.strangers.reply(peer, name, text)
        except GeminiError as e:
            log.warning("Gemini failed for %s: %s", peer, e)
            await self.client.send_message(peer, "Секунду, я сейчас перегружен. Напишите ещё раз чуть позже.")
            return
        if out is None:
            return
        answer, note = out
        if spoken:
            await self._send_voice(peer, answer)
        else:
            await self.client.send_message(peer, answer, parse_mode=None, link_preview=False)
        if note and self.tell_owner:
            await self.tell_owner(f"📨 {name} просит передать:\n{note}")

    async def _hear(self, m) -> str:
        """Gemini understands speech best; Whisper on the server's CPU is the fallback."""
        data = await m.download_media(file=bytes)
        if self.gemini.ready:
            try:
                return await self.gemini.transcribe(data, m.file.mime_type or "audio/ogg")
            except GeminiError as e:
                log.warning("Gemini transcription failed, Whisper instead: %s", e)
        path = os.path.join(self.inbox, f"tg_voice_{m.chat_id}_{m.id}.ogg")
        with open(path, "wb") as f:
            f.write(data)
        try:
            return await voice.transcribe(path)
        finally:
            os.unlink(path)

    async def _send_voice(self, peer: int, text: str) -> None:
        path = await voice.synthesize(text)
        try:
            await self.client.send_file(peer, path, voice_note=True)
        finally:
            os.unlink(path)

    async def name_of(self, peer: int) -> str:
        try:
            return display(await self.client.get_entity(peer))
        except Exception:
            return f"id {peer}"

    async def find_user(self, to: str):
        """The person the owner named: @username or t.me link, +phone, numeric id, or a name from Jarvis's own
        chats and contacts. Returns the user, or what went wrong as text.

        A bare word is only ever a name: taking it for a username could reach a random stranger.
        """
        to = to.strip()
        if to.startswith("@") or "t.me/" in to:
            try:
                return await self.client.get_entity(to)
            except (ValueError, TypeError) as e:
                return f"Ошибка: не нашёл {to}: {e}"
        if to.isdigit():
            try:
                return await self.client.get_entity(int(to))
            except (ValueError, TypeError):
                pass  # not someone Jarvis has met: maybe a phone number
        if PHONE.match(to):
            user = await self._import_phone(to, to)
            return user or f"Ошибка: в Telegram нет пользователя с номером {to} (или он скрыл себя от поиска по номеру)."
        return await self._by_name(to)

    async def _by_name(self, name: str):
        from telethon.tl.functions.contacts import GetContactsRequest

        people = {}
        async for dialog in self.client.iter_dialogs(limit=300):
            user = dialog.entity
            if dialog.is_user and not getattr(user, "bot", False) and not getattr(user, "is_self", False):
                people[user.id] = user
        for user in getattr(await self.client(GetContactsRequest(hash=0)), "users", []):
            people.setdefault(user.id, user)
        found = [u for u in people.values() if name_matches(name, u)]
        if len(found) == 1:
            return found[0]
        if not found:
            return f"Ошибка: «{name}» нет среди моих чатов и контактов. Нужен @username или номер телефона."
        return f"Ошибка: под «{name}» подходят несколько: " + "; ".join(display(u) for u in found[:8]) + ". Уточни, кому."

    async def send_to(self, user, text: str, files: list[tuple[str, bytes]] | None = None) -> str:
        """A message, photo, album or file from Jarvis's account to a user found by find_user."""
        if files:
            sent = []
            for name, data in files:
                item = io.BytesIO(data)
                item.name = name
                sent.append(item)
            photos = all(os.path.splitext(n)[1].lower() in IMAGE_EXT for n, _ in files)
            # Several pictures go as one album; anything else as documents
            await self.client.send_file(user, sent if len(sent) > 1 else sent[0], caption=text or None,
                                        parse_mode=None, force_document=not photos)
            what = (("фото" if photos else "файл") + " " + ", ".join(n for n, _ in files))
        else:
            await self.client.send_message(user, text, parse_mode=None, link_preview=False)
            what = "сообщение"
        # Gemini sees what Jarvis sent first, so the reply lands in context
        self.strangers.memory.add_peer(user.id, display(user), "model",
                                       f"[{what}] {text}".strip() if files else text)
        return f"Отправил {display(user)}: {what}."

    async def set_profile(self, username: str = "", first_name: str = "", about: str = "") -> str:
        from telethon.errors import RPCError
        from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest

        done = []
        try:
            if username:
                await self.client(UpdateUsernameRequest(username.lstrip("@")))
                done.append(f"username @{username.lstrip('@')}")
            if first_name or about:
                await self.client(UpdateProfileRequest(first_name=first_name or None, about=about or None))
                done += [f"имя {first_name}"] * bool(first_name) + [f"описание «{about}»"] * bool(about)
        except RPCError as e:
            return "Ошибка: Telegram не дал: " + str(e)
        return "Поменял: " + ", ".join(done) if done else "Ошибка: нечего менять."

    # Calls

    async def call_owner(self, text: str) -> bool:
        """Rings the owner in Telegram and says text; True when they picked up."""
        if not (self.ready and self.calls and self.owner_persona):
            return False
        cue = f"Ты сам позвонил пользователю. Скажи ему: {text}"
        return await self.calls.call(self.cfg.owner_id, self.owner_persona(cue))

    def _choose(self, peer: int) -> calls.Persona | None:
        if peer == self.cfg.owner_id:
            if not self.owner_persona:
                return None
            return self.owner_persona("Пользователь позвонил тебе. Поздоровайся одним-двумя словами, например «Слушаю».")
        if peer == SERVICE or not self.strangers.ready or not self.strangers.allowed(peer):
            return None

        async def run_tool(name: str, args: dict) -> str:
            if name != "pass_to_owner":
                return "Ошибка: этого ты не можешь."
            self.call_notes.setdefault(peer, []).append(str(args.get("note", ""))[:500])
            return "Передам."

        return calls.Persona(self.strangers.persona() + STRANGER_CALL,
                             "Тебе позвонил человек. Поздоровайся: ты Джарвис, ИИ-ассистент; спроси, чем помочь.",
                             STRANGER_TOOLS, run_tool)

    async def _call_ended(self, peer: int, talk: calls.LiveTalk) -> None:
        lines = "\n".join(f"{'Ты' if peer == self.cfg.owner_id else 'Он'}: {t}" if who == "them" else f"Я: {t}"
                          for who, t in talk.transcript)
        minutes = max(1, round(talk.minutes))
        if not self.tell_owner:
            return
        if peer == self.cfg.owner_id:
            if lines:
                await self.owner.say(f"📞 Звонок, {minutes} мин:\n{lines}"[:4000], quiet=True)
            return
        name = await self.name_of(peer)
        # A call counts against the same caps as messages, and shows up in tg_chats
        said = " / ".join(t for who, t in talk.transcript if who == "them")
        self.strangers.memory.add_peer(peer, name, "user", f"[звонок] {said}"[:4000])
        self.strangers.memory.add_peer(peer, name, "model", "[звонок] " + " / ".join(
            t for who, t in talk.transcript if who == "jarvis")[:4000])
        notes = self.call_notes.pop(peer, [])
        text = f"📞 Мне звонил {name}, {minutes} мин."
        if lines:
            text += f"\n{lines[:1500]}"
        if notes:
            text += "\n📨 Просил передать: " + "; ".join(notes)
        await self.tell_owner(text)
