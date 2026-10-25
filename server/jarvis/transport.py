"""Where a request talks back: the Telegram bot, or Jarvis's own account.

Both look the same to the rest of the code: say, edit, delete, typing, files, voice, yes/no.
The bot asks yes/no with buttons; a user account cannot send buttons, so it asks in words.
"""
import asyncio
import base64
import io
import os
import re
import uuid

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".xml", ".yaml", ".yml", ".log", ".ini"}
YES = re.compile(r"^\W*(да|ага|давай|выполняй|ок|ok|yes|da|davay)\W*$", re.I)
NO = re.compile(r"^\W*(нет|не надо|отмена|стоп|no|net)\W*$", re.I)
CONFIRM_SECONDS = 300


def file_request(path: str, caption: str) -> tuple[str, list[str] | None]:
    """What the brain gets for a file the owner sent: where it lies, plus the content if it is a picture or short text."""
    text = (caption or "Посмотри файл.") + f"\n[файл сохранён: {path}]"
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        with open(path, "rb") as f:
            return text, [base64.b64encode(f.read()).decode()]
    if ext in TEXT_EXT and os.path.getsize(path) < 60_000:
        with open(path, encoding="utf-8", errors="replace") as f:
            text += f"\nСодержимое:\n{f.read()}"
    return text, None


class BotChat:
    # Shared by every bot chat: the button handler finds the question by its key
    confirms: dict[str, asyncio.Future] = {}

    def __init__(self, bot: Bot, chat_id: int):
        self.bot, self.chat_id = bot, chat_id

    async def say(self, text: str, quiet: bool = False):
        return await self.bot.send_message(self.chat_id, text, disable_web_page_preview=True,
                                           disable_notification=quiet)

    async def edit(self, msg, text: str) -> None:
        try:
            await msg.edit_text(text)
        except TelegramBadRequest:
            pass

    async def delete(self, msg) -> None:
        try:
            await msg.delete()
        except TelegramBadRequest:
            pass

    async def typing(self) -> None:
        await self.bot.send_chat_action(self.chat_id, "typing")

    async def send_file(self, name: str, data: bytes) -> None:
        file = BufferedInputFile(data, filename=name)
        if os.path.splitext(name)[1].lower() in IMAGE_EXT and len(data) < 10 * 1024 * 1024:
            await self.bot.send_photo(self.chat_id, file)
        else:
            await self.bot.send_document(self.chat_id, file)

    async def send_voice(self, path: str) -> None:
        await self.bot.send_voice(self.chat_id, FSInputFile(path))

    async def confirm(self, question: str) -> bool:
        key = uuid.uuid4().hex[:12]
        fut = asyncio.get_running_loop().create_future()
        self.confirms[key] = fut
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Да", callback_data=f"ok:{key}"),
            InlineKeyboardButton(text="❌ Нет", callback_data=f"no:{key}")]])
        msg = await self.bot.send_message(self.chat_id, "⚠️ " + question[:3500], reply_markup=kb)
        try:
            return await asyncio.wait_for(fut, CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            return False
        finally:
            self.confirms.pop(key, None)
            await msg.edit_reply_markup(reply_markup=None)

    @classmethod
    def resolve(cls, key: str, yes: bool) -> None:
        fut = cls.confirms.get(key)
        if fut and not fut.done():
            fut.set_result(yes)

    @classmethod
    def refuse_all(cls) -> None:
        for fut in cls.confirms.values():
            if not fut.done():
                fut.set_result(False)


class UserChat:
    """The owner's private chat with Jarvis's own account (Telethon)."""

    def __init__(self, client, peer: int):
        self.client, self.peer = client, peer
        self.pending: asyncio.Future | None = None

    async def say(self, text: str, quiet: bool = False):
        return await self.client.send_message(self.peer, text, link_preview=False, silent=quiet, parse_mode=None)

    async def edit(self, msg, text: str) -> None:
        from telethon.errors import RPCError

        try:
            await msg.edit(text, parse_mode=None, link_preview=False)
        except RPCError:
            pass

    async def delete(self, msg) -> None:
        from telethon.errors import RPCError

        try:
            await msg.delete()
        except RPCError:
            pass

    async def typing(self) -> None:
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import SetTypingRequest
        from telethon.tl.types import SendMessageTypingAction

        try:
            await self.client(SetTypingRequest(self.peer, SendMessageTypingAction()))
        except RPCError:
            pass

    async def send_file(self, name: str, data: bytes) -> None:
        file = io.BytesIO(data)
        file.name = name
        await self.client.send_file(self.peer, file, force_document=os.path.splitext(name)[1].lower() not in IMAGE_EXT)

    async def send_voice(self, path: str) -> None:
        await self.client.send_file(self.peer, path, voice_note=True)

    async def confirm(self, question: str) -> bool:
        self.refuse_all()
        self.pending = fut = asyncio.get_running_loop().create_future()
        await self.say("⚠️ " + question[:3500] + "\n\nОтветь «да» или «нет».")
        try:
            return await asyncio.wait_for(fut, CONFIRM_SECONDS)
        except asyncio.TimeoutError:
            return False
        finally:
            if self.pending is fut:
                self.pending = None

    def answer(self, text: str) -> bool:
        """True when this owner message was the answer to a pending yes/no question."""
        if not self.pending or self.pending.done():
            return False
        for pattern, verdict in ((YES, True), (NO, False)):
            if pattern.match(text or ""):
                self.pending.set_result(verdict)
                return True
        return False

    def refuse_all(self) -> None:
        if self.pending and not self.pending.done():
            self.pending.set_result(False)
