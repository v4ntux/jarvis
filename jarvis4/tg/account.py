"""Jarvis's own Telegram account (Telethon): messages in, messages out, and the Telegram port the tools use.

Chats are keys: «owner» for the owner, «u<id>» for a person, «g<id>» for a group. A message ref is «<chat>/<id>».
Everything that comes in is turned into an Incoming for the core and written to the store; everything that goes out
passes the pacer (pace.py), so Telegram never has a reason to stop the account.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import time
from typing import Awaitable, Callable

from telethon import TelegramClient, events

from ..config import Config
from ..core import Core, Incoming
from ..gem import Gemini, GeminiError
from ..ports import CallResult, TGError
from ..store import Person, Store
from . import media
from .manage import Manage
from .pace import Pace, PacedClient
from .taste import Taste

log = logging.getLogger("jarvis4.tg")

SERVICE = 777000  # Telegram's own notices, login codes among them: never answered or passed anywhere
INBOX = "/opt/jarvis/data/inbox"
MAX_FILE = 200 * 1024 * 1024
PHONE = re.compile(r"^\+?\d[\d\s()-]{8,}$")
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mp3", ".m4a"}  # sent to play in the chat, not as a file to download
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".xml", ".yaml", ".yml", ".log", ".ini"}
PROGRESS_EVERY = 6  # seconds between edits of the status message


def user_name(user) -> str:
    name = " ".join(p for p in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if p)
    return name or getattr(user, "title", None) or getattr(user, "username", None) or "без имени"


class ChatOut:
    """How the core talks in one chat. The owner's chat adds yes/no questions and a status line."""

    def __init__(self, account: "Account", chat: str):
        self.acc, self.chat = account, chat
        self.human = chat != "owner"  # a stranger gets a moment of «печатает…» before the words, like from a person

    async def say(self, text: str, reply_ref: str = "") -> str:
        if self.human:
            await self.acc.typing(self.chat)
            await asyncio.sleep(min(0.5 + len(text) * 0.03, 3.0))
        return await self.acc.send(self.chat, text, None, reply_ref)

    async def ask(self, question: str) -> bool:
        return False  # only the owner is ever asked

    async def progress(self, line: str) -> None:
        pass

    async def send_file(self, name: str, data: bytes) -> None:
        await self.acc.send(self.chat, "", [(name, data)])

    async def typing(self) -> None:
        await self.acc.typing(self.chat)

    async def voice(self, text: str) -> None:
        await self.acc.send_voice(self.chat, text)

    async def finish(self) -> None:
        pass


class OwnerOut(ChatOut):
    def __init__(self, account: "Account"):
        super().__init__(account, "owner")
        self.status: dict = {"ref": None, "lines": [], "shown": 0.0}
        self._ask: Callable[[str], Awaitable[bool]] | None = None

    async def ask(self, question: str) -> bool:
        if self._ask is None:
            self._ask = self.acc.core.asker(lambda text: self.acc.send("owner", text))
        return await self._ask(question)

    async def progress(self, line: str) -> None:
        from . import pace
        st = self.status
        st["lines"] = (st["lines"] + ["• " + line])[-6:]
        if st["ref"] is not None and time.time() - st["shown"] < PROGRESS_EVERY:
            return
        st["shown"] = time.time()
        text = "⚙️ Работаю…\n" + "\n".join(st["lines"])
        try:
            with pace.at_most(3):  # a status line is not worth waiting for: skipped when late
                if st["ref"] is None:
                    st["ref"] = await self.acc.send("owner", text, quiet=True)
                else:
                    await self.acc.edit(st["ref"], text)
        except Exception:
            log.debug("status line skipped", exc_info=True)

    async def finish(self) -> None:
        ref, self.status = self.status["ref"], {"ref": None, "lines": [], "shown": 0.0}
        if ref:
            await self.acc.delete(ref)


class Account(Manage):
    def __init__(self, cfg: Config, store: Store, gemini: Gemini):
        self.cfg, self.store, self.gem = cfg, store, gemini
        self.core: Core | None = None
        self.client: TelegramClient | None = None
        self.taste: Taste | None = None
        self.callapi = None  # tg.callapi.CallApi once the account is up and Gemini Live is configured
        self.me_id = 0
        self.ready = False
        self._outs: dict[str, ChatOut] = {}
        self._albums: set[int] = set()
        self._background: set[asyncio.Task] = set()
        self.tell_owner: Callable[[str], Awaitable[None]] | None = None

    # ---- keys and refs -----------------------------------------------------------------------------------------

    def person_key(self, user_id: int) -> str:
        return "owner" if user_id == self.cfg.owner_id else f"u{user_id}"

    @staticmethod
    def group_key(chat_id: int) -> str:
        return f"g{abs(chat_id)}"

    @staticmethod
    def ref(chat: str, msg_id: int) -> str:
        return f"{chat}/{msg_id}"

    @staticmethod
    def parse(ref: str) -> tuple[str, int]:
        chat, _, msg_id = ref.rpartition("/")
        return chat, int(msg_id)

    async def entity(self, chat: str):
        if chat == "owner":
            return await self.client.get_input_entity(self.cfg.owner_id)
        if chat.startswith("u"):
            return await self.client.get_input_entity(int(chat[1:]))
        tg_id = self.store.chat_tg_id(chat)
        if not tg_id:
            raise TGError(f"не знаю такого чата: {chat}")
        return await self.client.get_input_entity(tg_id)

    def out_for(self, chat: str) -> ChatOut:
        if chat not in self._outs:
            self._outs[chat] = OwnerOut(self) if chat == "owner" else ChatOut(self, chat)
        return self._outs[chat]

    # ---- start -------------------------------------------------------------------------------------------------

    async def start(self, core: Core) -> bool:
        self.core = core
        cfg = self.cfg
        if not cfg.tg_api_id or not cfg.tg_api_hash:
            return False
        self.client = PacedClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash, device_model="Jarvis Server",
                                  system_version="Ubuntu", app_version="4.0", lang_code="ru", system_lang_code="ru-RU",
                                  connection_retries=-1, retry_delay=5)
        self.client.pace = Pace(cfg.owner_id)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            log.warning("own Telegram account is not logged in")
            await self.client.disconnect()
            return False
        me = await self.client.get_me()
        self.me_id = me.id
        self.taste = Taste(self.client, self.store, gem=self.gem)
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        self.client.add_event_handler(self._on_album, events.Album())
        await self._reach_owner()
        self.ready = True
        self._spawn(self.taste.warm())
        self._spawn(self._sync_people())
        from .callapi import CallApi
        api = CallApi(self, core, cfg, self.store)
        if await api.start():
            self.callapi = api
        log.info("own account %s is up, calls %s", user_name(me), "on" if self.callapi else "off")
        return True

    @property
    def calls(self):
        """The call machinery when calls work, else None (the owner's «ring me» paths check this)."""
        return self.callapi.calls if self.callapi else None

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _reach_owner(self) -> bool:
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

    async def _sync_people(self) -> None:
        """Who Jarvis already talks with, and the groups he is in: the store must know them before they write."""
        try:
            from telethon.tl.functions.contacts import GetContactsRequest

            async for d in self.client.iter_dialogs(limit=300):
                e = d.entity
                if d.is_user and not getattr(e, "bot", False) and not getattr(e, "is_self", False) and e.id != SERVICE:
                    self.person(e, dialog=True)
                elif d.is_group or d.is_channel:
                    self.store.chat_seen(self.group_key(d.id), "group" if d.is_group else "channel", d.name, d.id)
            for user in getattr(await self.client(GetContactsRequest(hash=0)), "users", []):
                if not getattr(user, "bot", False) and user.id != self.me_id:
                    self.person(user)
        except Exception:
            log.exception("syncing people failed")

    def person(self, user, dialog: bool | None = None) -> Person:
        return self.store.upsert_person(self.person_key(user.id), user_name(user), getattr(user, "username", None) or "",
                                        user.id, dialog=dialog)

    # ---- messages in -------------------------------------------------------------------------------------------

    async def _on_message(self, event) -> None:
        try:
            m = event.message
            if m.grouped_id:
                return  # part of an album: _on_album takes all of it at once
            if event.is_group:
                await self._group(event)
            elif event.is_private and event.sender_id != SERVICE:
                sender = await event.get_sender()
                if sender is not None and not getattr(sender, "bot", False):
                    await self._private(event, sender)
        except Exception as e:
            log.exception("message from %s failed", event.sender_id)
            if event.sender_id == self.cfg.owner_id and self.tell_owner:  # a lost message of his must never be silent
                try:
                    await self.tell_owner(f"споткнулся на твоём сообщении ({type(e).__name__}: {str(e)[:80]}). "
                                          "напиши ещё раз, а если повторится, скажи: гляну логи")
                except Exception:
                    log.exception("could not report the failure either")

    async def _on_album(self, event) -> None:
        try:
            if not event.is_private or event.sender_id != self.cfg.owner_id:
                return
            msgs = list(event.messages)
            images, saved = [], []
            for m in msgs[:6]:
                if m.photo:
                    name = f"photo_{m.id}.jpg"
                    data = await m.download_media(file=bytes)
                    with open(os.path.join(INBOX, name), "wb") as f:
                        f.write(data)
                    images.append(base64.b64encode(data).decode())
                    saved.append(name)
            caption = next((m.raw_text for m in msgs if m.raw_text), "")
            text = (caption + "\n" if caption else "") + f"[альбом: {len(msgs)} шт.]" + \
                   (f" [фото сохранены в inbox: {', '.join(saved)}]" if saved else "")
            first = msgs[0]
            await self.core.on_message(Incoming(ref=self.ref("owner", first.id), chat="owner", sender="owner", text=text,
                                                media={"kind": "photo"} if images else None, images=images,
                                                ts=first.date.timestamp()))
        except Exception:
            log.exception("album failed")

    async def classify(self, m, look: bool) -> tuple[dict | None, list[str], str]:
        """(media dict, pictures Claude can see, extra words for the text) of a message."""
        info, images, extra = media.kind_of(m), [], ""
        if info is None:
            return None, images, extra
        kind = info["kind"]
        if kind in ("sticker", "gif", "video", "circle") and look and self.gem.ready:
            try:
                label, parts = await media.look(m, media.OWNER_MAX if look is True else media.STRANGER_MAX)
                if parts:
                    info["about"] = await self.gem.describe(parts)
            except Exception as e:
                log.warning("could not look at %s: %s", kind, e)
        if kind == "photo" and look:
            data = await m.download_media(file=bytes)
            name = f"photo_{m.id}.jpg"
            os.makedirs(INBOX, exist_ok=True)
            with open(os.path.join(INBOX, name), "wb") as f:
                f.write(data)
            images.append(base64.b64encode(data).decode())
            extra = f"[фото сохранено в inbox как {name}]"
        return info, images, extra

    async def _hear(self, m) -> str:
        data = await m.download_media(file=bytes)
        if self.gem.ready:
            try:
                return await self.gem.transcribe(data, m.file.mime_type or "audio/ogg")
            except GeminiError as e:
                log.warning("Gemini transcription failed: %s", e)
        from ..ops import voice
        path = os.path.join(INBOX, f"tg_voice_{m.chat_id}_{m.id}.ogg")
        with open(path, "wb") as f:
            f.write(data)
        try:
            return await voice.transcribe(path)
        finally:
            os.unlink(path)

    async def _incoming(self, m, chat: str, sender: str, owner_like: bool, group: bool = False) -> Incoming | None:
        text = m.raw_text or ""
        media_info: dict | None = None
        images: list[str] = []
        if m.voice or m.audio or m.video_note:
            heard = await self._hear(m) if (m.voice or m.audio) else ""
            if heard:
                text = heard + (f"\n{text}" if text else "")
            media_info = {"kind": "voice" if (m.voice or m.audio) else "circle"}
            if not heard and (m.voice or m.audio):
                return None
        elif m.document and not m.sticker and not m.gif and not m.video and not (m.file.mime_type or "").startswith("image/"):
            if (m.file.size or 0) > MAX_FILE:
                return None
            name = os.path.basename(m.file.name or f"file_{m.id}{m.file.ext or ''}")
            path = os.path.join(INBOX, name)
            if owner_like:
                await m.download_media(file=path)
                text += f"\n[файл сохранён: {path}]"
                if os.path.splitext(path)[1].lower() in TEXT_EXT and os.path.getsize(path) < 60_000:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        text += f"\nСодержимое:\n{f.read()}"
            media_info = {"kind": "file", "about": name}
        else:
            media_info, images, extra = await self.classify(m, look=owner_like or (True if group else True))
            if extra:
                text = (text + "\n" + extra).strip()
        if not text and not media_info:
            return None
        if m.sticker:
            if self.taste:
                self.taste.saw_sticker(m)
            before = self.store.recent(chat, 1)  # what the sticker answers, so it can be said back at a similar moment
            self.store.sticker_seen(chat, m.sticker.id, ((m.file.emoji if m.file else "") or "").replace("️", ""),
                                    (media_info or {}).get("about", ""), before[0].text if before else "")
        reply_ref = await self._reply_ref(m, chat)
        return Incoming(ref=self.ref(chat, m.id), chat=chat, sender=sender, text=text, media=media_info,
                        reply_ref=reply_ref, images=images, group=group, mentioned=bool(getattr(m, "mentioned", False)),
                        ts=m.date.timestamp())

    async def _reply_ref(self, m, chat: str) -> str:
        if not getattr(m, "is_reply", False):
            return ""
        try:
            replied = await m.get_reply_message()
        except Exception:
            return ""
        if replied is None:
            return ""
        await self.remember(replied, chat)
        return self.ref(chat, replied.id)

    async def remember(self, m, chat: str) -> None:
        """Puts a message Jarvis did not receive live (history, a replied-to message) into the store."""
        sender = "jarvis" if m.out else self.person_key(m.sender_id or 0)
        info = media.kind_of(m)
        text = m.raw_text or ""
        self.store.add_msg_if_new(self.ref(chat, m.id), chat, sender, text, info,
                                  self.ref(chat, m.reply_to_msg_id) if getattr(m, "reply_to_msg_id", None) else "",
                                  ts=m.date.timestamp())

    async def _private(self, event, sender) -> None:
        m = event.message
        if sender.id == self.cfg.owner_id:
            inc = await self._incoming(m, "owner", "owner", owner_like=True)
        else:
            person = self.person(sender, dialog=True)
            if person.blocked:
                return
            await self.client.send_read_acknowledge(sender, m)
            inc = await self._incoming(m, person.key, person.key, owner_like=False)
        if inc is not None:
            await self.core.on_message(inc)

    async def _group(self, event) -> None:
        from telethon.tl.types import User

        m, sender = event.message, await event.get_sender()
        if not isinstance(sender, User) or sender.bot or sender.id in (self.me_id, SERVICE):
            return
        chat = self.group_key(event.chat_id)
        title = getattr(await event.get_chat(), "title", None) or "группа"
        if self.store.chat_seen(chat, "group", title, event.chat_id) and self.tell_owner:
            await self.tell_owner(f"👥 Теперь я в группе «{title}»")
        who = self.person(sender)
        sender_key = "owner" if sender.id == self.cfg.owner_id else who.key
        inc = await self._incoming(m, chat, sender_key, owner_like=sender.id == self.cfg.owner_id, group=True)
        if inc is not None:
            await self.core.on_message(inc)

    # ---- messages out ------------------------------------------------------------------------------------------

    async def send(self, chat: str, text: str, files: list[tuple[str, bytes]] | None = None, reply_ref: str = "",
                   quiet: bool = False) -> str:
        from telethon.errors import (ChatWriteForbiddenError, FloodWaitError, PeerFloodError, UserBannedInChannelError,
                                     UserIsBlockedError)

        if fake := self.dry("send", f"{chat}: {text[:120]}" + (f" +{len(files)} файл." if files else "")):
            return fake
        entity = await self.entity(chat)
        reply_to = self.parse(reply_ref)[1] if reply_ref else None
        try:
            if files:
                items = []
                for name, data in files:
                    item = io.BytesIO(data)
                    item.name = name
                    items.append(item)
                playable = all(os.path.splitext(n)[1].lower() in IMAGE_EXT | VIDEO_EXT for n, _ in files)
                sent = await self.client.send_file(entity, items if len(items) > 1 else items[0], caption=text or None,
                                                   parse_mode=None, force_document=not playable, reply_to=reply_to,
                                                   supports_streaming=True)
            else:
                sent = await self.client.send_message(entity, text, parse_mode=None, link_preview=False,
                                                      reply_to=reply_to, silent=quiet)
        except UserIsBlockedError:
            if chat.startswith("u"):
                self.store.upsert_person(chat, blocked=True)
            raise TGError("человек заблокировал мой аккаунт")
        except PeerFloodError:
            raise TGError("Telegram (антиспам) не даёт моему аккаунту писать первым тем, кто мне не писал. Пусть человек "
                          "напишет сам, или проверь аккаунт в @SpamBot")
        except FloodWaitError as e:
            raise TGError(f"Telegram просит подождать {e.seconds} с перед следующим сообщением")
        except (ChatWriteForbiddenError, UserBannedInChannelError):
            raise TGError("в этот чат мне нельзя писать")
        first = sent[0] if isinstance(sent, list) else sent
        return self.ref(chat, first.id)

    def dry(self, what: str, detail: str = "") -> str:
        """Rehearsal mode: nothing leaves the account, it is only logged, and a made-up ref comes back so the tools
        carry on as if it had worked. Empty when the mode is off."""
        if not self.cfg.dry_run:
            return ""
        self._fake = getattr(self, "_fake", 0) + 1
        log.info("DRY %s %s", what, detail)
        return f"dry/{self._fake}"

    async def send_voice(self, chat: str, text: str) -> str:
        """A voice message: Edge TTS on the server, sent as a voice note."""
        if fake := self.dry("voice", f"{chat}: {text[:120]}"):
            return fake
        from ..ops import voice

        path = await voice.synthesize(text)
        try:
            sent = await self.client.send_file(await self.entity(chat), path, voice_note=True)
        finally:
            if os.path.exists(path):
                os.unlink(path)
        return self.ref(chat, sent.id)

    async def edit(self, ref: str, text: str) -> None:
        from telethon.errors import RPCError
        if self.dry("edit", ref):
            return
        chat, msg_id = self.parse(ref)
        try:
            await self.client.edit_message(await self.entity(chat), msg_id, text, parse_mode=None, link_preview=False)
        except RPCError:
            pass

    async def delete(self, ref: str) -> None:
        from telethon.errors import RPCError
        if self.dry("delete", ref):
            return
        chat, msg_id = self.parse(ref)
        try:
            await self.client.delete_messages(await self.entity(chat), [msg_id])
        except RPCError:
            pass

    async def typing(self, chat: str) -> None:
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction

        try:
            await self.client(SetTypingRequest(await self.entity(chat), SendMessageTypingAction()))
        except (RPCError, TGError, ValueError):
            pass

    # ---- the Telegram port (what the tools use) ----------------------------------------------------------------

    async def lookup(self, query: str) -> Person | str:
        q = query.strip()
        try:
            if q.startswith("@") or "t.me/" in q:
                user = await self.client.get_entity(q)
            elif q.isdigit():
                user = await self.client.get_entity(int(q))
            elif PHONE.match(q):
                user = await self._import_phone(q, q)
                if user is None:
                    return f"Ошибка: в Telegram нет пользователя с номером {q} (или он скрыл себя от поиска по номеру)."
            else:
                return f"Ошибка: «{q}» не нашёл."
        except (ValueError, TypeError) as e:
            return f"Ошибка: не нашёл {q}: {e}"
        if getattr(user, "bot", False) or not hasattr(user, "first_name"):
            return f"Ошибка: {q} — не человек."
        return self.person(user)

    async def sync_history(self, chat: str, limit: int) -> None:
        entity = await self.entity(chat)
        fetched = [m async for m in self.client.iter_messages(entity, limit=limit)]
        for m in reversed(fetched):
            await self.remember(m, chat)

    async def forward(self, refs: list[str], to: str) -> list[str]:
        from telethon.errors import RPCError

        if self.dry("forward", f"{refs} → {to}"):
            return [f"dry/{self._fake}.{i}" for i, _ in enumerate(refs)]
        target = await self.entity(to)
        by_chat: dict[str, list[int]] = {}
        for ref in refs:
            chat, msg_id = self.parse(ref)
            by_chat.setdefault(chat, []).append(msg_id)
        new: list[str] = []
        for chat, ids in by_chat.items():
            try:
                sent = await self.client.forward_messages(target, ids, from_peer=await self.entity(chat))
            except RPCError as e:
                raise TGError(f"Telegram не дал переслать: {e}")
            sent = sent if isinstance(sent, list) else [sent]
            new += [self.ref(to, s.id) for s in sent if s is not None]
        return new

    async def react(self, ref: str, emoji: str) -> None:
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import SendReactionRequest
        from telethon.tl.types import ReactionEmoji

        if self.dry("react", f"{ref} {emoji}"):
            return
        chat, msg_id = self.parse(ref)
        try:
            await self.client(SendReactionRequest(peer=await self.entity(chat), msg_id=msg_id,
                                                  reaction=[ReactionEmoji(emoticon=emoji)]))
        except (RPCError, TGError):
            pass  # Telegram refuses emoji it does not offer: a missing like is not an error

    @property
    def why(self) -> str:
        """Why the last sticker or GIF did not go out."""
        return self.taste.why if self.taste else "стикеры ещё не готовы"

    async def sticker(self, chat: str, emoji: str, reply_ref: str = "", about: str = "") -> str:
        if fake := self.dry("sticker", f"{chat} {emoji} ({about})"):
            return fake
        sent = await self.taste.express(await self.entity(chat), chat, sticker=emoji, about=about,
                                        reply_to=self.parse(reply_ref)[1] if reply_ref else None)
        return self.ref(chat, sent.id) if sent else ""

    async def gif(self, chat: str, query: str, reply_ref: str = "", about: str = "") -> str:
        if fake := self.dry("gif", f"{chat} {query} ({about})"):
            return fake
        sent = await self.taste.express(await self.entity(chat), chat, gif=query, about=about,
                                        reply_to=self.parse(reply_ref)[1] if reply_ref else None)
        return self.ref(chat, sent.id) if sent else ""

    async def leave(self, chat: str) -> str:
        from telethon.errors import RPCError
        if self.dry("leave", chat):
            return "вышел (репетиция)"
        try:
            await self.client.delete_dialog(await self.entity(chat))
        except RPCError as e:
            raise TGError(f"Telegram не дал выйти: {e}")
        return "вышел"

    async def call(self, person: str, mode: str, goal: str) -> CallResult:
        if self.dry("call", f"{person} {mode}: {goal[:100]}"):
            return CallResult(True, "(репетиция) позвонил, ничего не отправлялось")
        if self.callapi is None:
            raise TGError("звонки в Telegram сейчас не работают (/status скажет почему)")
        return await self.callapi.call(person, mode, goal)
