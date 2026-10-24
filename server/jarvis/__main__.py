import asyncio
import base64
import logging
import os
import time
import uuid

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import config, voice
from .brain import INBOX, Brain, Session
from .hub import Hub
from .memory import Memory

log = logging.getLogger("jarvis")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".py", ".js", ".ts", ".html", ".xml", ".yaml", ".yml", ".log", ".ini"}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    os.makedirs(INBOX, exist_ok=True)
    memory = Memory(cfg.db_path)
    hub = Hub(cfg.hands_token)
    await hub.start(cfg.hands_host, cfg.hands_port)
    brain = Brain(cfg, memory, hub)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    # Strangers get no reply at all
    dp.message.filter(F.from_user.id == cfg.owner_id)
    dp.callback_query.filter(F.from_user.id == cfg.owner_id)

    state = {"task": None, "voice_replies": True}
    confirms: dict[str, asyncio.Future] = {}

    def make_session(chat_id: int) -> Session:
        status: dict = {"msg": None, "lines": []}

        async def progress(line: str) -> None:
            status["lines"] = (status["lines"] + ["• " + line])[-6:]
            text = "⚙️ Работаю…\n" + "\n".join(status["lines"])
            try:
                if status["msg"] is None:
                    status["msg"] = await bot.send_message(chat_id, text)
                else:
                    await status["msg"].edit_text(text)
            except TelegramBadRequest:
                pass

        async def confirm(question: str) -> bool:
            key = uuid.uuid4().hex[:12]
            fut = asyncio.get_running_loop().create_future()
            confirms[key] = fut
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Да", callback_data=f"ok:{key}"),
                InlineKeyboardButton(text="❌ Нет", callback_data=f"no:{key}")]])
            msg = await bot.send_message(chat_id, "⚠️ " + question[:3500], reply_markup=kb)
            try:
                return await asyncio.wait_for(fut, 300)
            except asyncio.TimeoutError:
                return False
            finally:
                confirms.pop(key, None)
                await msg.edit_reply_markup(reply_markup=None)

        async def send_file(name: str, data: bytes) -> None:
            file = BufferedInputFile(data, filename=name)
            if os.path.splitext(name)[1].lower() in IMAGE_EXT and len(data) < 10 * 1024 * 1024:
                await bot.send_photo(chat_id, file)
            else:
                await bot.send_document(chat_id, file)

        session = Session(progress, confirm, send_file)
        session.status = status
        return session

    async def answer_with(m: Message, text: str, images: list[str] | None = None, spoken: bool = False) -> None:
        if state["task"] and not state["task"].done():
            await m.answer("Я ещё занят прошлой задачей. /stop — прервать её.")
            return
        session = make_session(m.chat.id)
        await bot.send_chat_action(m.chat.id, "typing")
        state["task"] = asyncio.create_task(brain.reply(text, session, images))
        try:
            answer = await state["task"]
        except asyncio.CancelledError:
            answer = "Остановил."
        except Exception as e:
            log.exception("brain failed")
            answer = f"Ошибка мозга: {type(e).__name__}: {e}"[:500]
        finally:
            state["task"] = None
            if session.status["msg"]:
                try:
                    await session.status["msg"].delete()
                except TelegramBadRequest:
                    pass
        for i in range(0, len(answer), 4000):
            await m.answer(answer[i: i + 4000], disable_web_page_preview=True)
        if spoken and state["voice_replies"]:
            try:
                path = await voice.synthesize(answer)
                await m.answer_voice(FSInputFile(path))
                os.unlink(path)
            except Exception:
                log.exception("tts failed")

    @dp.callback_query(F.data.regexp(r"^(ok|no):"))
    async def on_confirm(q: CallbackQuery) -> None:
        verdict, key = q.data.split(":", 1)
        fut = confirms.get(key)
        if fut and not fut.done():
            fut.set_result(verdict == "ok")
        await q.answer("Выполняю" if verdict == "ok" else "Отменено")

    @dp.message(Command("start"))
    async def start(m: Message) -> None:
        await m.answer("Джарвис на связи. Пиши текстом или голосом, присылай фото и файлы.\n"
                       "/stop прервать · /status ноутбук · /cost расход · /memory что помню · "
                       "/voice голосовые ответы вкл/выкл · /reset забыть разговор")

    @dp.message(Command("stop"))
    async def stop(m: Message) -> None:
        task = state["task"]
        if task and not task.done():
            task.cancel()
            for fut in confirms.values():
                if not fut.done():
                    fut.set_result(False)
        else:
            await m.answer("Сейчас ничего не выполняю.")

    @dp.message(Command("reset"))
    async def reset(m: Message) -> None:
        memory.clear()
        await m.answer("Историю разговора очистил. Факты из /memory остались.")

    @dp.message(Command("cost"))
    async def cost(m: Message) -> None:
        day = memory.spent_since(time.time() - 86400)
        total = memory.spent_since(0)
        await m.answer(f"За сутки: ${day:.4f} из ${cfg.daily_limit:.2f}\nВсего: ${total:.4f}")

    @dp.message(Command("status"))
    async def status(m: Message) -> None:
        await m.answer("Ноутбук: " + ("на связи" if hub.online else "не подключён"))

    @dp.message(Command("memory"))
    async def show_memory(m: Message) -> None:
        facts = memory.facts()
        await m.answer("\n".join(f"#{i}: {t}" for i, t in facts) or "Пока ничего не запомнил.")

    @dp.message(Command("voice"))
    async def toggle_voice(m: Message) -> None:
        state["voice_replies"] = not state["voice_replies"]
        await m.answer("Голосовые ответы " + ("включены." if state["voice_replies"] else "выключены."))

    @dp.message(F.voice | F.audio | F.video_note)
    async def on_voice(m: Message) -> None:
        media = m.voice or m.audio or m.video_note
        path = os.path.join(INBOX, f"voice_{m.message_id}.ogg")
        await bot.download(media, destination=path)
        await bot.send_chat_action(m.chat.id, "typing")
        try:
            text = await voice.transcribe(path)
        finally:
            os.unlink(path)
        if not text:
            await m.answer("Не расслышал, повтори.")
            return
        await m.answer(f"🎙 {text}")
        caption = f"\n{m.caption}" if m.caption else ""
        await answer_with(m, text + caption, spoken=True)

    @dp.message(F.photo)
    async def on_photo(m: Message) -> None:
        buf = await bot.download(m.photo[-1])
        data = buf.read()
        name = f"photo_{m.message_id}.jpg"
        with open(os.path.join(INBOX, name), "wb") as f:
            f.write(data)
        text = (m.caption or "Что на фото?") + f"\n[фото сохранено в inbox как {name}]"
        await answer_with(m, text, images=[base64.b64encode(data).decode()])

    @dp.message(F.document)
    async def on_document(m: Message) -> None:
        doc = m.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await m.answer("Файл больше 20 МБ, Telegram-бот такие не скачивает.")
            return
        name = os.path.basename(doc.file_name or f"file_{m.message_id}")
        path = os.path.join(INBOX, name)
        await bot.download(doc, destination=path)
        ext = os.path.splitext(name)[1].lower()
        text = (m.caption or "Посмотри файл.") + f"\n[файл сохранён: {path}]"
        images = None
        if ext in IMAGE_EXT:
            with open(path, "rb") as f:
                images = [base64.b64encode(f.read()).decode()]
        elif ext in TEXT_EXT and os.path.getsize(path) < 60_000:
            with open(path, encoding="utf-8", errors="replace") as f:
                text += f"\nСодержимое:\n{f.read()}"
        await answer_with(m, text, images=images)

    @dp.message(F.text)
    async def chat(m: Message) -> None:
        await answer_with(m, m.text)

    async def reminders_loop() -> None:
        while True:
            for rid, text in memory.due_reminders():
                if memory.finish_reminder(rid):
                    try:
                        await bot.send_message(cfg.owner_id, f"⏰ Напоминание: {text}")
                    except Exception:
                        log.exception("reminder %s failed", rid)
            await asyncio.sleep(20)

    asyncio.create_task(reminders_loop())
    log.info("starting, model=%s smart=%s", cfg.model, cfg.smart_model)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
