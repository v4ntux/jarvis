"""Jarvis's own Telegram account (Telethon).

The owner talks to the brain here just like in the bot; everyone else talks to Gemini
and never reaches the brain, the tools or the laptop. Both can call: the owner's calls
go to the brain, everyone else's to Gemini. Groups, channels and bots are ignored.
"""
import asyncio
import base64
import logging
import os
import re
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from . import calls, voice
from .config import Config
from .gemini import GeminiError, Strangers
from .transport import UserChat

log = logging.getLogger("jarvis.tg")

SERVICE = 777000  # Telegram's own notices, login codes among them: never answered or passed anywhere
MAX_FILE = 200 * 1024 * 1024
PHONE = re.compile(r"^\+?\d[\d\s()-]{8,}$")
STRANGER_OPENER = "Здравствуйте, это Джарвис, ИИ-ассистент. Слушаю вас."

# The owner's message for the brain: (chat, text, images, spoken, file path)
OwnerHandler = Callable[[UserChat, str, list[str] | None, bool, str | None], Awaitable[None]]


def make_client(cfg: Config) -> TelegramClient:
    return TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash, device_model="Jarvis Server",
                          system_version="Ubuntu", app_version="3.0", lang_code="ru", system_lang_code="ru-RU")


def display(user) -> str:
    name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p)
    name = name or getattr(user, "title", None) or "без имени"
    if getattr(user, "username", None):
        name += f" (@{user.username})"
    return f"{name}, id {user.id}"


class UserAccount:
    def __init__(self, cfg: Config, strangers: Strangers, inbox: str):
        self.cfg, self.strangers, self.inbox = cfg, strangers, inbox
        self.client: TelegramClient | None = None  # Telethon refuses an empty api_id, so it is made in start()
        self.calls: calls.Calls | None = None
        self.owner: UserChat | None = None
        self.ready = False
        # Set by the bot: messages for the brain, the brain's answers in a call, notes for the owner
        self.on_owner: OwnerHandler | None = None
        self.owner_reply: calls.Reply | None = None
        self.tell_owner: Callable[[str], Awaitable[None]] | None = None
        # Set by the bot: any word from the owner here counts as «Принял» for a running alarm
        self.on_owner_seen: Callable[[], None] | None = None
        self.call_notes: dict[int, list[str]] = {}

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
        if not await self._reach_owner():
            log.warning("cannot reach the owner yet: they should write to %s first", display(me))
        try:
            self.calls = calls.Calls(self.client, self._choose, self._call_ended, self.cfg.call_minutes)
            await self.calls.start()
        except Exception:
            log.exception("calls are off")
            self.calls = None
        self.ready = True
        log.info("own account %s is up, calls %s, Gemini %s", display(me), "on" if self.calls else "off",
                 "on" if self.strangers.ready else "off")
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

    async def _from_stranger(self, event, sender) -> None:
        m, peer, name = event.message, event.sender_id, display(sender)
        text, spoken = m.raw_text or "", False
        if m.voice or m.video_note:
            heard = await self._hear(m)
            if not heard:
                return
            text, spoken = heard + (f"\n{text}" if text else ""), True
        elif m.media:
            text = "[прислал вложение]" + (f" {text}" if text else "")
        if not text.strip():
            return
        new = self.strangers.is_new(peer)
        if new and self.tell_owner:
            await self.tell_owner(f"👤 Мне впервые написал {name}:\n{text[:500]}")
        await self.client.send_read_acknowledge(peer, m)
        try:
            async with self.client.action(peer, "record-audio" if spoken else "typing"):
                out = await self.strangers.reply(peer, name, text)
        except GeminiError as e:
            log.warning("Gemini failed for %s: %s", peer, e)
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
        path = os.path.join(self.inbox, f"tg_voice_{m.chat_id}_{m.id}.ogg")
        await m.download_media(file=path)
        try:
            return await voice.transcribe(path)
        finally:
            if os.path.exists(path):
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

    async def send_to(self, to: str, text: str) -> str:
        """A message from Jarvis's account to someone the owner named: @username, link or phone number."""
        to = to.strip()
        if PHONE.match(to):
            user = await self._import_phone(to, to)
            if user is None:
                return f"Ошибка: в Telegram нет пользователя с номером {to} (или он скрыл себя от поиска по номеру)."
        else:
            try:
                user = await self.client.get_entity(to)
            except (ValueError, TypeError) as e:
                return f"Ошибка: не нашёл {to}: {e}"
        await self.client.send_message(user, text, parse_mode=None, link_preview=False)
        # Gemini sees what Jarvis wrote first, so the reply lands in context
        self.strangers.memory.add_peer(user.id, display(user), "model", text)
        return f"Отправил: {display(user)}."

    # Calls

    async def call_owner(self, text: str) -> bool:
        """Rings the owner in Telegram and says text; True when they picked up."""
        if not (self.ready and self.calls and self.owner_reply):
            return False
        return await self.calls.call(self.cfg.owner_id, text, self.owner_reply)

    def _choose(self, peer: int) -> tuple[calls.Reply, str] | None:
        if peer == self.cfg.owner_id:
            return (self.owner_reply, "Слушаю.") if self.owner_reply else None
        if peer == SERVICE or not self.strangers.ready or not self.strangers.allowed(peer):
            return None
        return self._stranger_reply(peer), STRANGER_OPENER

    def _stranger_reply(self, peer: int) -> calls.Reply:
        async def reply(text: str) -> str:
            try:
                out = await self.strangers.reply(peer, await self.name_of(peer), text, spoken=True)
            except GeminiError as e:
                log.warning("Gemini failed in a call with %s: %s", peer, e)
                return "Извините, связь с мозгом пропала. Напишите мне текстом."
            if out is None:
                return "Извините, на сегодня я наговорился. Напишите мне позже."
            answer, note = out
            if note:
                self.call_notes.setdefault(peer, []).append(note)
            return answer
        return reply

    async def _call_ended(self, peer: int, talk: calls.Conversation) -> None:
        if peer == self.cfg.owner_id or not self.tell_owner:
            return
        said = " / ".join(t for who, t in talk.transcript if who == "them")
        notes = self.call_notes.pop(peer, [])
        text = f"📞 Мне звонил {await self.name_of(peer)}, {max(1, round(talk.minutes))} мин."
        if said:
            text += f"\nГоворил: {said[:1000]}"
        if notes:
            text += "\n📨 Просил передать: " + "; ".join(notes)
        await self.tell_owner(text)
