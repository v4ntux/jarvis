import asyncio
import base64
import logging
import os
import uuid

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from . import config, phone, voice, watchers
from .brain import INBOX, Brain, Session
from .hub import Hub
from .memory import Memory

log = logging.getLogger("jarvis")

BILLING = "https://console.anthropic.com/settings/billing"
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
    background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks from being garbage collected
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    # Strangers get no reply at all
    dp.message.filter(F.from_user.id == cfg.owner_id)
    dp.callback_query.filter(F.from_user.id == cfg.owner_id)

    state = {"task": None, "voice_replies": True}
    confirms: dict[str, asyncio.Future] = {}
    acks: dict[str, asyncio.Event] = {}

    def make_session(chat_id: int) -> Session:
        status: dict = {"msg": None, "lines": []}

        async def progress(line: str) -> None:
            status["lines"] = (status["lines"] + ["• " + line])[-6:]
            text = "⚙️ Работаю…\n" + "\n".join(status["lines"])
            try:
                if status["msg"] is None:
                    status["msg"] = await bot.send_message(chat_id, text, disable_notification=True)
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

    def busy() -> bool:
        return bool(state["task"] and not state["task"].done())

    async def run_brain(chat_id: int, text: str, images: list[str] | None = None) -> str | None:
        """One request through the brain; progress and confirmations go to this Telegram chat.

        None when the brain is busy. The check and the start happen with no await in between.
        """
        if busy():
            return None
        session = make_session(chat_id)
        state["task"] = asyncio.create_task(brain.reply(text, session, images))
        await bot.send_chat_action(chat_id, "typing")
        try:
            answer = await state["task"]
        except asyncio.CancelledError:
            answer = "Остановил."
        except anthropic.BadRequestError as e:
            log.exception("brain failed")
            answer = (f"На счёте Anthropic кончились деньги, пополни: {BILLING}" if "credit balance" in str(e)
                      else f"Ошибка мозга: {e}"[:500])
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
        return answer

    async def send_long(chat_id: int, text: str, quiet: bool = False) -> None:
        for i in range(0, len(text), 4000):
            await bot.send_message(chat_id, text[i: i + 4000], disable_web_page_preview=True,
                                   disable_notification=quiet)

    async def respond(chat_id: int, text: str, images: list[str] | None = None, spoken: bool = False) -> None:
        answer = await run_brain(chat_id, text, images)
        if answer is None:
            await bot.send_message(chat_id, "Я ещё занят прошлой задачей. /stop — прервать её.")
            return
        await send_long(chat_id, answer)
        if spoken and state["voice_replies"]:
            await send_voice(answer)

    async def ask_from_laptop(text: str) -> str:
        """The laptop's voice or panel asks; the laptop speaks the answer, Telegram keeps a quiet copy."""
        busy_text = "Я ещё занят прошлой задачей. Скажи «стоп», чтобы прервать её."
        if busy():
            return busy_text
        await bot.send_message(cfg.owner_id, f"🎙 С ноутбука: {text}"[:4000], disable_notification=True)
        answer = await run_brain(cfg.owner_id, f"[голосом у ноутбука] {text}")
        if answer is None:
            return busy_text
        await send_long(cfg.owner_id, answer, quiet=True)
        return answer

    def stop_task() -> bool:
        task = state["task"]
        if not task or task.done():
            return False
        task.cancel()
        for fut in confirms.values():
            if not fut.done():
                fut.set_result(False)
        return True

    async def send_voice(text: str) -> None:
        path = None
        try:
            path = await voice.synthesize(text)
            await bot.send_voice(cfg.owner_id, FSInputFile(path))
        except Exception:
            log.exception("tts failed")
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    async def to_phone(text: str, call: bool) -> str:
        """SMS (and a robot call) to the owner's phone; returns what happened, never raises."""
        results = []
        for send in (brain.phone.sms, brain.phone.call) if call else (brain.phone.sms,):
            try:
                results.append(await send("Джарвис: " + text))
            except phone.PhoneError as e:
                results.append(f"Ошибка: {e}")
        return "\n".join(results)

    async def alarm(text: str) -> None:
        """Voice messages in Telegram until «Принял»; no answer in 5 minutes means SMS and a robot call."""
        key = uuid.uuid4().hex[:12]
        acks[key] = heard = asyncio.Event()
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принял", callback_data=f"ack:{key}")]])
        msg = await bot.send_message(cfg.owner_id, "🚨 " + text[:3500], reply_markup=kb)

        async def escalate() -> None:
            if brain.phone.ready:
                report = await to_phone(text, call=True)
                await bot.send_message(cfg.owner_id, "📵 Ты не ответил, поэтому продублировал на телефон.\n" + report)

        try:
            await phone.ring_until_heard(heard, lambda: send_voice(text), escalate)
        finally:
            acks.pop(key, None)
            try:
                await msg.edit_reply_markup(reply_markup=None)
            except TelegramBadRequest:
                pass

    async def on_event(event: dict, detail: str) -> None:
        """A watcher fired or a background job ended: tell the owner, then run the task if there is one."""
        text = event.get("notify") or f"Сработало: {event['what']}"
        if detail:
            text += f"\n\n{detail[-1500:]}"
        if event.get("urgent"):
            task = asyncio.create_task(alarm(text))
            background.add(task)
            task.add_done_callback(background.discard)
        else:
            await bot.send_message(cfg.owner_id, "🔔 " + text[:4000], disable_web_page_preview=True)
            if event.get("sms"):
                report = await to_phone(text, call=False)
                if report.startswith("Ошибка"):
                    await bot.send_message(cfg.owner_id, "📵 " + report)
        if event.get("task"):
            # Wait for the owner's own request to finish instead of interrupting it
            while busy():
                await asyncio.sleep(5)
            await respond(cfg.owner_id, f"[Сработало автоматически: {event['what']}]\n{event['task']}"
                          + (f"\nДанные: {detail[-1500:]}" if detail else ""))

    @dp.callback_query(F.data.regexp(r"^(ok|no):"))
    async def on_confirm(q: CallbackQuery) -> None:
        verdict, key = q.data.split(":", 1)
        fut = confirms.get(key)
        if fut and not fut.done():
            fut.set_result(verdict == "ok")
        await q.answer("Выполняю" if verdict == "ok" else "Отменено")

    @dp.callback_query(F.data.startswith("ack:"))
    async def on_ack(q: CallbackQuery) -> None:
        heard = acks.get(q.data.split(":", 1)[1])
        if heard:
            heard.set()
        await q.answer("Принято")

    @dp.callback_query(F.data.startswith("unwatch:"))
    async def on_unwatch(q: CallbackQuery) -> None:
        wid = int(q.data.split(":", 1)[1])
        await q.answer(f"Удалил #{wid}" if memory.remove_watcher(wid) else "Его уже нет")
        await q.message.edit_reply_markup(reply_markup=None)

    @dp.message(Command("start", "help"))
    async def start(m: Message) -> None:
        await m.answer("Джарвис на связи. Пиши текстом или голосом, присылай фото и файлы.\n"
                       "Работаю на сервере 24/7, ноутбук нужен только для дел на нём.\n"
                       "/stop прервать · /status ноутбук · /cost расход · /memory что помню · "
                       "/watchers наблюдатели · /phonetest проверить SMS (/phonetest call — звонок) · "
                       "/voice голосовые ответы вкл/выкл · /reset забыть разговор")

    @dp.message(Command("phonetest"))
    async def phone_test(m: Message) -> None:
        call = "call" in (m.text or "").lower()
        await m.answer(await to_phone("проверка связи. Если слышишь или читаешь это, всё работает.", call=call))

    @dp.message(Command("watchers"))
    async def show_watchers(m: Message) -> None:
        rows = memory.watchers()
        if not rows:
            await m.answer("Наблюдателей нет. Скажи, например: «скажи, когда закончится загрузка» "
                           "или «каждое утро в 8 присылай погоду».")
            return
        text = "\n".join(f"#{i} {watchers.describe(spec)}" + (f"\n   ⚠️ {st['error']}" if st.get("error") else "")
                         for i, spec, st, _ in rows)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 #{i}", callback_data=f"unwatch:{i}")] for i, *_ in rows])
        await m.answer(text[:4000], reply_markup=kb)

    @dp.message(Command("stop"))
    async def stop(m: Message) -> None:
        if not stop_task():
            await m.answer("Сейчас ничего не выполняю.")

    @dp.message(Command("reset"))
    async def reset(m: Message) -> None:
        memory.clear()
        await m.answer("Историю разговора очистил. Факты из /memory остались.")

    @dp.message(Command("cost"))
    async def cost(m: Message) -> None:
        total = memory.spent_since(0)
        used, rub = memory.phone_since(config.day_start())
        await m.answer(f"Сегодня: ${brain.spent_today():.4f} из ${cfg.daily_limit:.2f} "
                       f"(обнуляется в полночь по Москве)\nВсего: ${total:.4f}\n"
                       f"SMS и звонки сегодня: {used} из {cfg.phone_daily_limit}, {rub:.2f} ₽")

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
        await respond(m.chat.id, text + caption, spoken=True)

    @dp.message(F.photo)
    async def on_photo(m: Message) -> None:
        buf = await bot.download(m.photo[-1])
        data = buf.read()
        name = f"photo_{m.message_id}.jpg"
        with open(os.path.join(INBOX, name), "wb") as f:
            f.write(data)
        text = (m.caption or "Что на фото?") + f"\n[фото сохранено в inbox как {name}]"
        await respond(m.chat.id, text, images=[base64.b64encode(data).decode()])

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
        await respond(m.chat.id, text, images=images)

    @dp.message(F.text)
    async def chat(m: Message) -> None:
        await respond(m.chat.id, m.text)

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
    brain.notify = on_event
    hub.on_ask, hub.on_stop = ask_from_laptop, stop_task

    async def laptop_notice(text: str) -> None:
        await send_long(cfg.owner_id, "💻 " + text)

    hub.on_notice = laptop_notice
    asyncio.create_task(watchers.Watchers(memory, hub, on_event).loop())
    log.info("starting, model=%s smart=%s", cfg.model, cfg.smart_model)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
