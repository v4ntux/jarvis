import asyncio
import base64
import datetime as dt
import logging
import os
import time
import uuid

import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import calls, config, intents, phone, style, tg_user, voice, watchers, web
from .brain import INBOX, Brain, Session
from .front import Front
from .gemini import Gemini, Strangers
from .hub import Hub
from .memory import Memory
from .transport import BotChat, LogChat, file_request

log = logging.getLogger("jarvis")

BILLING = "https://console.anthropic.com/settings/billing"
BUSY = "Я ещё занят прошлой задачей. /stop — прервать её."
CALL_MARK = "[голосом в звонке] "
CALL_WORK_SECONDS = 90
HELP = ("Джарвис на связи. Пиши текстом или голосом, присылай фото и файлы, звони в Telegram.\n"
        "Работаю на сервере 24/7, ноутбук нужен только для дел на нём. Команды не нужны: просто говори, "
        "например «позвони через 5 минут» или «стоп».\n"
        "/stop прервать · /status ноутбук и Telegram · /cost расход · /memory что помню · "
        "/watchers наблюдатели (/unwatch 3 — удалить) · /calltest позвонить в Telegram · /phonetest проверить SMS "
        "(/phonetest call — звонок робота) · /voice голосовые ответы вкл/выкл · /reset забыть разговор")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("primp", "httpcore", "aiohttp.access"):  # one line per search-engine request drowns the real log
        logging.getLogger(noisy).setLevel(logging.WARNING)
    cfg = config.load()
    os.makedirs(INBOX, exist_ok=True)
    memory = Memory(cfg.db_path)
    hub = Hub(cfg.hands_token)
    await hub.start(cfg.hands_host, cfg.hands_port)
    brain = Brain(cfg, memory, hub)
    gemini = Gemini(cfg)
    front = Front(brain, gemini)
    account = tg_user.UserAccount(cfg, Strangers(cfg, memory, gemini), gemini, INBOX)
    background: set[asyncio.Task] = set()  # keeps fire-and-forget tasks from being garbage collected
    # The bot is optional now: with no token everything goes through Jarvis's own account
    bot = Bot(cfg.bot_token) if cfg.bot_token else None
    bot_chat = BotChat(bot, cfg.owner_id) if bot else LogChat(log)
    dp = Dispatcher()
    # Strangers get no reply at all
    dp.message.filter(F.from_user.id == cfg.owner_id)
    dp.callback_query.filter(F.from_user.id == cfg.owner_id)

    state = {"task": None, "voice_replies": True}
    acks: dict[str, asyncio.Event] = {}

    def owner_chat():
        """Jarvis's own account when it is up, the bot otherwise: where messages nobody asked for go."""
        return account.owner if account.ready else bot_chat

    def spawn(coro) -> None:
        task = asyncio.create_task(coro)
        background.add(task)
        task.add_done_callback(background.discard)

    def make_session(chat) -> Session:
        status: dict = {"msg": None, "lines": []}

        async def progress(line: str) -> None:
            status["lines"] = (status["lines"] + ["• " + line])[-6:]
            text = "⚙️ Работаю…\n" + "\n".join(status["lines"])
            try:
                if status["msg"] is None:
                    status["msg"] = await chat.say(text, quiet=True)
                else:
                    await chat.edit(status["msg"], text)
            except Exception:
                log.debug("progress message failed", exc_info=True)

        session = Session(progress, chat.confirm, chat.send_file)
        session.status = status
        return session

    def busy() -> bool:
        return bool(state["task"] and not state["task"].done())

    async def run_brain(chat, text: str, images: list[str] | None = None, wait: float = 0,
                        direct: bool = False, media: list[dict] | None = None) -> str | None:
        """One request: free Gemini first, the Claude brain for real work (direct: the brain at once).
        Progress and confirmations go to this chat.

        None when still busy after waiting up to wait seconds. The check and the start happen
        with no await in between.
        """
        for _ in range(int(wait * 2)):
            if not busy():
                break
            await asyncio.sleep(0.5)
        if busy():
            return None
        session = make_session(chat)
        request = brain.reply(text, session, images) if direct else front.reply(text, session, images, media)
        state["task"] = asyncio.create_task(request)
        await chat.typing()
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
            front.compact_later()
            if session.status["msg"]:
                await chat.delete(session.status["msg"])
        return answer

    async def send_long(chat, text: str, quiet: bool = False) -> None:
        for i in range(0, len(text), 4000):
            await chat.say(text[i: i + 4000], quiet=quiet)

    async def send_voice(chat, text: str) -> None:
        path = None
        try:
            path = await voice.synthesize(text)
            await chat.send_voice(path)
        except Exception:
            log.exception("tts failed")
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    async def ring(text: str) -> None:
        """A call the owner asked for: ring in Telegram, and if he does not pick up leave it in the chat."""
        picked = bool(account.ready and account.calls and await account.call_owner(text))
        if not picked:
            await owner_chat().say(f"📞 Звонил, ты не взял. {text}")

    async def run_intent(chat, text: str) -> bool:
        """Plain words that need no model at all: «стоп», «позвони мне», «позвони через две минуты»."""
        now = dt.datetime.now(config.TZ)
        intent = intents.parse(text, now)
        if intent is None:
            return False
        if intent.kind == "stop":
            return stop_task()  # nothing to stop: the word goes to the chat like any other
        if not (account.ready and account.calls):
            answer = "Позвонить в Telegram не могу: звонки сейчас не работают (/status покажет почему)."
        elif intent.at is None or intent.at - time.time() < 5:
            spawn(ring("Ты просил позвонить."))
            answer = "Звоню."
        else:
            memory.add_reminder(intent.at, "Ты просил позвонить.", call=True)
            when = dt.datetime.fromtimestamp(intent.at, config.TZ)
            answer = f"Окей, наберу в {when:%H:%M}" + (f" ({when:%d.%m})" if when.date() != now.date() else "") + "."
        memory.add("user", text)
        memory.add("assistant", answer)
        await chat.say(answer)
        return True

    async def respond(chat, text: str, images: list[str] | None = None, spoken: bool = False,
                      media: list[dict] | None = None) -> None:
        if not images and not media and await run_intent(chat, text):
            return
        answer = await run_brain(chat, text, images, media=media)
        if answer is None:
            await chat.say(BUSY)
            return
        answer, emoji = style.split_reaction(answer)
        if emoji:
            await chat.react(emoji)  # like a person's like under his message
        if answer:
            await send_long(chat, answer)
            if spoken and state["voice_replies"]:
                await send_voice(chat, answer)

    async def ask_from_laptop(text: str) -> str:
        """The laptop's voice or panel asks; the laptop speaks the answer, Telegram keeps a quiet copy."""
        busy_text = "Я ещё занят прошлой задачей. Скажи «стоп», чтобы прервать её."
        if busy():
            return busy_text
        chat = owner_chat()
        await chat.say(f"🎙 С ноутбука: {text}"[:4000], quiet=True)
        answer = await run_brain(chat, f"[голосом у ноутбука] {text}")
        if answer is None:
            return busy_text
        await send_long(chat, answer, quiet=True)
        return answer

    async def call_tool(name: str, args: dict) -> str:
        """A function Gemini Live calls during the owner's call; real work goes to the brain."""
        chat = owner_chat()
        if name != "delegate":
            return await front.run_tool(name, args, make_session(chat))
        task = asyncio.create_task(run_brain(chat, CALL_MARK + str(args.get("task", "")), wait=20, direct=True))
        done, _ = await asyncio.wait({task}, timeout=CALL_WORK_SECONDS)
        if not done:
            # The talk cannot wait that long: the answer comes to the chat when ready
            task.add_done_callback(lambda t: t.cancelled() or spawn(send_long(chat, "📞 " + (t.result() or BUSY))))
            return "Задача ещё выполняется, результат придёт в чат."
        answer = task.result()
        if answer is None:
            return "Я ещё занят прошлой задачей. Её можно прервать командой /stop в чате."
        await send_long(chat, "📞 " + answer, quiet=True)
        return answer

    def owner_persona(cue: str) -> calls.Persona:
        return calls.Persona(front.system(call=True), cue, front.call_tools(), call_tool)

    def stop_task() -> bool:
        task = state["task"]
        if not task or task.done():
            return False
        task.cancel()
        BotChat.refuse_all()
        if account.owner:
            account.owner.refuse_all()
        return True

    async def to_phone(text: str, call: bool) -> str:
        """SMS (and a robot call) to the owner's phone; returns what happened, never raises."""
        results = []
        for send in (brain.phone.sms, brain.phone.call) if call else (brain.phone.sms,):
            try:
                results.append(await send("Джарвис: " + text))
            except phone.PhoneError as e:
                results.append(f"Ошибка: {e}")
        return "\n".join(results)

    def owner_seen() -> None:
        """Any word from the owner in Jarvis's own chat stops running alarms."""
        for heard in acks.values():
            heard.set()

    async def alarm(text: str) -> None:
        """Telegram calls (or voice messages) until the owner answers; 5 minutes of silence means SMS and a robot call."""
        key = uuid.uuid4().hex[:12]
        acks[key] = heard = asyncio.Event()
        chat = owner_chat()
        msg = None
        if isinstance(chat, BotChat):
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Принял", callback_data=f"ack:{key}")]])
            msg = await bot.send_message(cfg.owner_id, "🚨 " + text[:3500], reply_markup=kb)
        else:
            await chat.say("🚨 " + text[:3500] + "\n\nОтветь что угодно или возьми трубку, и я перестану звонить.")

        async def remind() -> None:
            if await account.call_owner("Срочно. " + text):
                heard.set()
            else:
                await send_voice(chat, text)

        async def escalate() -> None:
            if brain.phone.ready:
                report = await to_phone(text, call=True)
                await chat.say("📵 Ты не ответил, поэтому продублировал на телефон.\n" + report)

        try:
            await phone.ring_until_heard(heard, remind, escalate)
        finally:
            acks.pop(key, None)
            if msg:
                try:
                    await msg.edit_reply_markup(reply_markup=None)
                except TelegramBadRequest:
                    pass

    async def on_event(event: dict, detail: str) -> None:
        """A watcher fired or a background job ended: tell the owner, then run the task if there is one."""
        text = event.get("notify") or f"Сработало: {event['what']}"
        if detail:
            text += f"\n\n{detail[-1500:]}"
        chat = owner_chat()
        if event.get("urgent"):
            spawn(alarm(text))
        else:
            await chat.say("🔔 " + text[:4000])
            if event.get("sms"):
                report = await to_phone(text, call=False)
                if report.startswith("Ошибка"):
                    await chat.say("📵 " + report)
        if event.get("task"):
            # Wait for the owner's own request to finish instead of interrupting it
            while busy():
                await asyncio.sleep(5)
            await respond(chat, f"[Сработало автоматически: {event['what']}]\n{event['task']}"
                          + (f"\nДанные: {detail[-1500:]}" if detail else ""))

    # Commands work the same in the bot and in Jarvis's own chat; each returns its answer
    async def cmd_help(_: str) -> str:
        return HELP

    async def cmd_stop(_: str) -> str:
        return "" if stop_task() else "Сейчас ничего не выполняю."  # the stopped request answers itself

    async def cmd_reset(_: str) -> str:
        memory.clear()
        return "Историю разговора очистил. Факты из /memory остались."

    async def cmd_cost(_: str) -> str:
        used, rub = memory.phone_since(config.day_start())
        return (f"Claude сегодня: ${brain.spent_today():.4f} из ${cfg.daily_limit:.2f} (обнуляется в твою полночь)\n"
                "Разговор, поиск, напоминания и звонки идут через Gemini: бесплатно. Claude — только для работы "
                "на ноутбуке и сервере или по слову «подумай».\n"
                f"Всего: ${memory.spent_since(0):.4f}\n"
                f"SMS и звонки сегодня: {used} из {cfg.phone_daily_limit}, {rub:.2f} ₽\n"
                f"Ответов чужим через Gemini сегодня: {memory.peer_replies_since(config.day_start())} "
                f"из {cfg.stranger_daily}")

    async def cmd_status(_: str) -> str:
        if account.ready:
            tg = "на связи, звонки " + ("работают" if account.calls else "не работают")
        else:
            tg = "не подключён" + ("" if cfg.tg_api_id else " (нет TG_API_ID)")
        state_of_gemini = ("подключён: бесплатный чат, чужие, звонки" if gemini.ready
                           else "не подключён (нет GEMINI_API_KEY): всё делает платный Claude, чужим не отвечаю, звонков нет")
        return (f"Ноутбук: {'на связи' if hub.online else 'не подключён'}\nСвой Telegram: {tg}\n"
                f"Gemini: {state_of_gemini}")

    async def cmd_memory(_: str) -> str:
        return "\n".join(f"#{i}: {t}" for i, t in memory.facts()) or "Пока ничего не запомнил."

    async def cmd_voice(_: str) -> str:
        state["voice_replies"] = not state["voice_replies"]
        return "Голосовые ответы " + ("включены." if state["voice_replies"] else "выключены.")

    async def cmd_phonetest(text: str) -> str:
        return await to_phone("проверка связи. Если слышишь или читаешь это, всё работает.", call="call" in text.lower())

    async def cmd_calltest(_: str) -> str:
        if not account.ready or not account.calls:
            return "Звонки не работают: свой Telegram-аккаунт не подключён. /status покажет почему."
        spawn(account.call_owner("Проверка звонка. Скажи что-нибудь, я отвечу. Чтобы закончить, скажи «пока»."))
        return "Звоню тебе в Telegram."

    async def cmd_watchers(_: str) -> str:
        rows = memory.watchers()
        if not rows:
            return ("Наблюдателей нет. Скажи, например: «скажи, когда закончится загрузка» "
                    "или «каждое утро в 8 присылай погоду».")
        return "\n".join(f"#{i} {watchers.describe(spec)}" + (f"\n   ⚠️ {st['error']}" if st.get("error") else "")
                         for i, spec, st, _ in rows) + f"\n\nУдалить: /unwatch {rows[0][0]}"

    async def cmd_unwatch(text: str) -> str:
        """A user account has no buttons: watchers are removed by number."""
        number = text.split(maxsplit=1)[1].strip().lstrip("#") if len(text.split()) > 1 else ""
        if not number.isdigit():
            return "Напиши номер: /unwatch 3. Номера покажет /watchers."
        return f"Удалил #{number}." if memory.remove_watcher(int(number)) else f"Наблюдателя #{number} нет."

    commands = {"start": cmd_help, "help": cmd_help, "stop": cmd_stop, "reset": cmd_reset, "cost": cmd_cost,
                "status": cmd_status, "memory": cmd_memory, "voice": cmd_voice, "phonetest": cmd_phonetest,
                "calltest": cmd_calltest, "watchers": cmd_watchers, "unwatch": cmd_unwatch}

    def command_of(text: str):
        words = text[1:].split(maxsplit=1) if text.startswith("/") else []
        return commands.get(words[0].split("@")[0].lower()) if words else None

    async def handle_owner(chat, text: str, images: list[str] | None = None, spoken: bool = False,
                           path: str | None = None, media: list[dict] | None = None) -> None:
        """An owner message from Jarvis's own account: a command, a file, a meme or a request."""
        if path:
            text, images = file_request(path, text)
        elif command := command_of(text):
            await send_long(chat, await command(text))
            return
        await respond(chat, text, images, spoken, media)

    @dp.callback_query(F.data.regexp(r"^(ok|no):"))
    async def on_confirm(q: CallbackQuery) -> None:
        verdict, key = q.data.split(":", 1)
        BotChat.resolve(key, verdict == "ok")
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

    @dp.message(Command("watchers"))
    async def show_watchers(m: Message) -> None:
        rows = memory.watchers()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🗑 #{i}", callback_data=f"unwatch:{i}")] for i, *_ in rows]) if rows else None
        await m.answer((await cmd_watchers(""))[:4000], reply_markup=kb)

    @dp.message(Command(*commands))
    async def on_command(m: Message) -> None:
        await send_long(bot_chat, await command_of(m.text)(m.text))

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
        await respond(bot_chat, text + caption, spoken=True)

    @dp.message(F.photo)
    async def on_photo(m: Message) -> None:
        buf = await bot.download(m.photo[-1])
        data = buf.read()
        name = f"photo_{m.message_id}.jpg"
        with open(os.path.join(INBOX, name), "wb") as f:
            f.write(data)
        text = (m.caption or "Что на фото?") + f"\n[фото сохранено в inbox как {name}]"
        await respond(bot_chat, text, images=[base64.b64encode(data).decode()])

    @dp.message(F.document)
    async def on_document(m: Message) -> None:
        doc = m.document
        if doc.file_size and doc.file_size > 20 * 1024 * 1024:
            await m.answer("Файл больше 20 МБ, Telegram-бот такие не скачивает. Пришли его в мой личный аккаунт.")
            return
        path = os.path.join(INBOX, os.path.basename(doc.file_name or f"file_{m.message_id}"))
        await bot.download(doc, destination=path)
        text, images = file_request(path, m.caption or "")
        await respond(bot_chat, text, images=images)

    @dp.message(F.text)
    async def chat(m: Message) -> None:
        await respond(bot_chat, m.text)

    async def reminders_loop() -> None:
        while True:
            for rid, text, call in memory.due_reminders():
                if memory.finish_reminder(rid):
                    try:
                        if call:
                            spawn(ring(text))
                        else:
                            await owner_chat().say(f"⏰ Напоминание: {text}")
                    except Exception:
                        log.exception("reminder %s failed", rid)
            await asyncio.sleep(10)

    async def slang_loop() -> None:
        """Once a week Jarvis reads up on what is trending, so he keeps up with the slang and the memes."""
        await asyncio.sleep(90)
        while True:
            try:
                if gemini.ready and style.refresh_due(memory):
                    await style.refresh(gemini, memory, web.search)
            except Exception:
                log.exception("slang refresh failed")
            await asyncio.sleep(6 * 3600)

    async def laptop_notice(text: str) -> None:
        await send_long(owner_chat(), "💻 " + text)

    async def tell_owner(text: str) -> None:
        await send_long(owner_chat(), text)

    spawn(reminders_loop())  # references are kept: asyncio drops tasks nobody holds
    spawn(slang_loop())
    brain.notify, brain.tg = on_event, account
    hub.on_ask, hub.on_stop, hub.on_notice = ask_from_laptop, stop_task, laptop_notice
    spawn(watchers.Watchers(memory, hub, on_event).loop())
    account.on_owner, account.owner_persona = handle_owner, owner_persona
    account.tell_owner, account.on_owner_seen = tell_owner, owner_seen
    try:
        await account.start()
    except Exception:
        log.exception("own Telegram account failed to start")
    log.info("starting, model=%s smart=%s, bot %s", cfg.model, cfg.smart_model, "on" if bot else "off")
    if bot:
        await dp.start_polling(bot)
    elif account.ready:
        await account.client.disconnected
        raise SystemExit("own Telegram account disconnected")  # systemd starts Jarvis again
    else:
        log.error("no Telegram at all: no bot token and the own account is not logged in")
        await asyncio.Event().wait()  # the laptop's voice still reaches the brain through the hub


if __name__ == "__main__":
    asyncio.run(main())
