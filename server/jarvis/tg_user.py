"""Jarvis's own Telegram account (Telethon).

The owner talks to Jarvis here (free Gemini first, the Claude brain for real work); everyone
else talks to Gemini alone and never reaches the brain, the tools, the laptop or anything
Jarvis knows about the owner. Calls go through Gemini Live. Groups, channels and bots are ignored.
"""
import asyncio
import base64
import collections
import io
import logging
import os
import re
import time
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from . import autonomy, calls, media, style, voice
from .config import Config
from .gemini import Gemini, GeminiError, Strangers
from .groups import RECENT_MEDIA_SECONDS, Groups
from .names import name_matches
from .taste import Taste
from .transport import IMAGE_EXT, UserChat, react

log = logging.getLogger("jarvis.tg")

SERVICE = 777000  # Telegram's own notices, login codes among them: never answered or passed anywhere
MAX_FILE = 200 * 1024 * 1024
MAX_STRANGER_VOICE = 120  # seconds: longer voice messages from strangers are not worth the quota
MAX_ALBUM_IMAGES = 6  # the model looks at this many pictures of an album; all of them are saved
SPAM_BLOCK_AFTER = 3  # junk messages from one stranger before Telegram's block
PHONE = re.compile(r"^\+?\d[\d\s()-]{8,}$")

STRANGER_CALL = """

Сейчас ты говоришь с этим человеком по телефону, голосом: 1–2 короткие фразы, без списков и смайликов.
Если он просит что-то передать владельцу или говорит что-то важное для него — pass_to_owner. Когда попрощались — hang_up."""
STRANGER_TOOLS = [
    {"name": "pass_to_owner", "description": "Передать владельцу короткую записку: кто звонил и что просил.",
     "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "hang_up", "description": "Положить трубку: когда попрощались или разговор закончен."},
]

# The owner's message: (chat, text, images, spoken, file path, parts of video and stickers for Gemini)
OwnerHandler = Callable[..., Awaitable[None]]


def make_client(cfg: Config) -> TelegramClient:
    # Jarvis lives here 24/7 with no bot to fall back on: keep reconnecting however long the outage is
    return TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash, device_model="Jarvis Server",
                          system_version="Ubuntu", app_version="3.0", lang_code="ru", system_lang_code="ru-RU",
                          connection_retries=-1, retry_delay=5)


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
        self.groups = Groups(cfg, strangers.memory, gemini)  # how he behaves in group chats
        self.taste: Taste | None = None  # his own stickers and GIFs, once the account is up
        self.me_id = 0
        self._background: set[asyncio.Task] = set()

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
        self.me_id = self.groups.me_id = me.id
        self.owner = UserChat(self.client, self.cfg.owner_id)
        self.taste = self.owner.taste = Taste(self.client, self.strangers.memory)
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
        task = asyncio.create_task(self.taste.warm())  # sticker packs are looked up in the background
        self._background.add(task)
        task.add_done_callback(self._background.discard)
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
        try:
            if event.is_group:
                await self._from_group(event)
            elif not event.is_private:
                return  # channels
            elif event.sender_id == self.cfg.owner_id:
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
        self.owner.last = m  # a reaction goes under this message
        if m.voice or m.audio or m.video_note:
            heard = await self._hear(m)
            if heard:
                await self.owner.say(f"🎙 {heard}", quiet=True)
                await self.on_owner(self.owner, heard + (f"\n{text}" if text else ""), None, True, None)
                return
            if not m.video_note:
                await self.owner.say("Не расслышал, повтори.")
                return
        if m.sticker or m.gif or m.video or m.video_note or m.photo or \
                (m.document and (m.file.mime_type or "").startswith("image/")):
            await self._look_at(m, text)
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

    async def _look_at(self, m, text: str) -> None:
        """A meme, sticker, GIF, video, circle or photo: Gemini looks, and answers like a person (or just reacts)."""
        label, parts = await media.look(m, media.OWNER_MAX)
        images, saved = None, ""
        if m.photo and parts:  # photos also reach Claude's eyes and the inbox, for tasks like «отправь это Заке»
            data = parts[0]["inlineData"]["data"]
            name = f"photo_{m.id}.jpg"
            with open(os.path.join(self.inbox, name), "wb") as f:
                f.write(base64.b64decode(data))
            images, parts, saved = [data], [], f"\n[фото сохранено в inbox как {name}]"
        elif text and autonomy.looks_like_task(text) and (m.video or m.gif or m.video_note):
            name = os.path.basename(m.file.name or f"video_{m.id}{m.file.ext or '.mp4'}")
            await m.download_media(file=os.path.join(self.inbox, name))
            saved = f"\n[видео сохранено в inbox как {name}]"
        await self.on_owner(self.owner, f"{label} {text}".strip() + saved, images, False, None, parts or None)

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
                text = (event.text or (f"[альбом: {len(images)} фото]" if images else "Посмотри файлы.")) + \
                       f"\n[сохранено в inbox: {', '.join(names)}]"
                await self.on_owner(self.owner, text, images[:MAX_ALBUM_IMAGES] or None, False, None)
        except Exception:
            log.exception("album from the owner failed")

    async def _from_stranger(self, event, sender) -> None:
        m, peer, name = event.message, event.sender_id, display(sender)
        if self.strangers.memory.is_blocked(peer) or not self.strangers.allowed(peer):
            return  # blocked, or over the caps: silence, and no note for the owner either
        if m.grouped_id:
            if m.grouped_id in self._albums_seen:
                return  # one answer per album, not one per picture
            self._albums_seen.append(m.grouped_id)
        text, spoken, parts = m.raw_text or "", False, []
        if m.voice or m.video_note:
            if (m.file.duration or 0) > MAX_STRANGER_VOICE:
                text = "[прислал длинное голосовое, больше двух минут]" + (f" {text}" if text else "")
            else:
                heard = await self._hear(m)
                if not heard:
                    return
                text, spoken = heard + (f"\n{text}" if text else ""), True
        elif m.media:
            label, parts = await media.look(m, media.STRANGER_MAX)
            text = f"{label or '[прислал вложение]'} {text}".strip()
        if not text.strip():
            return
        fresh = self.strangers.is_new(peer)
        await self.client.send_read_acknowledge(peer, m)
        try:
            async with self.client.action(peer, "record-audio" if spoken else "typing"):
                out = await self.strangers.reply(peer, name, text, parts)
        except GeminiError as e:
            log.warning("Gemini failed for %s: %s", peer, e)
            await self.client.send_message(peer, "Секунду, я сейчас перегружен. Напишите ещё раз чуть позже.")
            return
        if out is None:
            return
        if out.spam:
            await self._spam(peer, name)
            return
        if fresh and self.tell_owner:
            await self.tell_owner(f"👤 Мне впервые написал {name}:\n{text[:500]}")
        if out.reaction:
            await react(self.client, m, out.reaction)
        if out.answer and spoken:
            await self._send_voice(peer, out.answer)
        elif out.answer:
            await self.client.send_message(peer, out.answer, parse_mode=None, link_preview=False)
        await self._express(peer, out, m)
        if out.note and self.tell_owner:
            await self.tell_owner(f"📨 {name} просит передать:\n{out.note}")

    async def _spam(self, peer: int, name: str) -> None:
        """Junk gets no answer and no note; the third piece from one stranger means a Telegram block."""
        from telethon.errors import RPCError
        from telethon.tl.functions.contacts import BlockRequest

        memory = self.strangers.memory
        hits = memory.spam_hit(peer)
        log.info("spam from %s (%d)", name, hits)
        if hits < SPAM_BLOCK_AFTER:
            return
        try:
            await self.client(BlockRequest(await self.client.get_input_entity(peer)))
        except (RPCError, ValueError):
            log.warning("could not block %s in Telegram, ignoring them instead", name)
        memory.block(peer)
        if self.tell_owner:
            await self.tell_owner(f"🚫 Заблокировал спамера {name}.")

    async def _express(self, chat: int, said, msg=None, reply_to: int | None = None) -> None:
        """The sticker or GIF he wanted to send along, and the keepsake from what he was just shown."""
        if self.taste is None:
            return
        if said.sticker or said.gif:
            await self.taste.express(chat, said.sticker, said.gif, reply_to)
        if said.love and msg is not None:
            await self.taste.love(msg, said.love)

    # Groups: a member with a character. No tools, no Claude, nothing about the owner: only a personality.

    async def _from_group(self, event) -> None:
        from telethon.tl.types import User

        m, chat = event.message, event.chat_id
        sender = await event.get_sender()
        if not isinstance(sender, User) or sender.bot or sender.id in (self.me_id, SERVICE):
            return  # other bots would talk with him forever
        name = sender.first_name or sender.username or "кто-то"
        owner = sender.id == self.cfg.owner_id
        who = f"{name} (хозяин)" if owner else name
        title = getattr(await event.get_chat(), "title", None) or "группа"
        memory = self.strangers.memory
        if memory.group_seen(chat, title) and self.tell_owner:
            await self.tell_owner(f"👥 Теперь я в группе «{title}». /groups — управление.")
        text, tag = m.raw_text or "", media.label(m)
        memory.group_add(chat, m.id, sender.id, name, f"{tag} {text}".strip(), media=bool(tag))
        mode = memory.group_mode(chat)
        if mode == "off" or not self.gemini or not self.gemini.ready:
            return
        addressed = self.groups.addressed(text, bool(m.mentioned), await self._replied_to_me(m))
        switch = self.groups.owner_switch(text) if addressed and owner else None
        if switch:  # «Джарвис, тихо» from the owner, right in the group
            memory.set_group_mode(chat, switch)
            await react(self.client, m, "🫡" if switch == "quiet" else "🔥")
        elif addressed:
            await self._group_reply(m, chat, title, who, text, tag, spontaneous=False)
        elif mode == "on" and self.groups.spontaneous_wanted(bool(tag), text):
            await self._group_reply(m, chat, title, who, text, tag, spontaneous=True)

    async def _replied_to_me(self, m) -> bool:
        if not getattr(m, "is_reply", False):
            return False
        try:
            replied = await m.get_reply_message()
        except Exception:
            return False
        return bool(replied and replied.sender_id == self.me_id)

    async def _group_reply(self, m, chat: int, title: str, who: str, text: str, tag: str, spontaneous: bool) -> None:
        if not self.groups.may_speak(chat, spontaneous):
            return
        parts, seen = await self._group_media(m, chat, text, tag, look=bool(tag) or not spontaneous)
        ask = self.groups.glance if spontaneous else self.groups.answer
        try:
            async with self.client.action(chat, "typing"):
                verdict = await ask(chat, title, who, text or tag, seen, parts)
        except GeminiError as e:
            log.warning("Gemini failed in group %s: %s", title, e)
            return  # in a group he stays silent rather than say he is overloaded
        await self._group_say(m, chat, title, verdict, spontaneous)

    async def _group_media(self, m, chat: int, text: str, tag: str, look: bool) -> tuple[list[dict], str]:
        """What he can see: the message's own picture or video, the one it answers, or (for «смотри») the latest."""
        if not look:
            return [], ""
        shown = [m] if tag else []
        if not shown and getattr(m, "is_reply", False):
            try:
                replied = await m.get_reply_message()
            except Exception:
                replied = None
            if replied is not None and media.label(replied):
                shown.append(replied)
        if not shown and self.groups.wants_a_look(text):
            recent = self.strangers.memory.group_media_since(chat, time.time() - RECENT_MEDIA_SECONDS)
            found = await self.client.get_messages(chat, ids=recent) if recent else None
            if found is not None:
                shown.append(found)
        parts, labels = [], []
        for item in shown[:2]:
            words, got = await media.look(item, media.STRANGER_MAX)
            labels.append(words)
            parts += got
        return parts, ", ".join(label for label in labels if label)

    async def _group_say(self, m, chat: int, title: str, verdict, spontaneous: bool) -> None:
        from telethon.errors import (ChatAdminRequiredError, ChatWriteForbiddenError, FloodWaitError, RPCError,
                                     SlowModeWaitError, UserBannedInChannelError)

        if not verdict:
            return
        sent_id = None
        try:
            if verdict.reaction and not (verdict.sticker or verdict.gif):  # a person does not do all three at once
                await react(self.client, m, verdict.reaction)
            if verdict.answer:
                sent_id = (await self.client.send_message(chat, verdict.answer, reply_to=m.id, parse_mode=None,
                                                          link_preview=False)).id
            await self._express(chat, verdict, m, reply_to=m.id)
        except (FloodWaitError, SlowModeWaitError) as e:
            self.groups.mute_for(chat, e.seconds + 5)
            return
        except (ChatWriteForbiddenError, UserBannedInChannelError, ChatAdminRequiredError):
            self.groups.mute_for(chat, 6 * 3600)
            if self.tell_owner:
                await self.tell_owner(f"🔇 В группе «{title}» мне запретили писать.")
            return
        except RPCError as e:
            log.warning("group %s: %s", title, e)
            return
        self.groups.record(chat, sent_id, verdict.answer or style.describe_marks(verdict) or "…", spontaneous)

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
        if peer == SERVICE or not self.strangers.ready or not self.strangers.allowed(peer) \
                or self.strangers.memory.is_blocked(peer):
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
