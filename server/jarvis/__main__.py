import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

from . import config
from .brain import Brain
from .hub import Hub
from .memory import Memory

log = logging.getLogger("jarvis")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = config.load()
    memory = Memory(cfg.db_path)
    hub = Hub(cfg.hands_token)
    await hub.start(cfg.hands_host, cfg.hands_port)
    brain = Brain(cfg, memory, hub)
    bot = Bot(cfg.bot_token)
    dp = Dispatcher()
    # Strangers get no reply at all
    dp.message.filter(F.from_user.id == cfg.owner_id)

    @dp.message(Command("start"))
    async def start(m: Message) -> None:
        await m.answer("Джарвис на связи.")

    @dp.message(Command("reset"))
    async def reset(m: Message) -> None:
        memory.clear()
        await m.answer("Историю разговора очистил.")

    @dp.message(Command("cost"))
    async def cost(m: Message) -> None:
        day = memory.spent_since(time.time() - 86400)
        total = memory.spent_since(0)
        await m.answer(f"За сутки: ${day:.4f}\nВсего: ${total:.4f}")

    @dp.message(Command("status"))
    async def status(m: Message) -> None:
        await m.answer("Ноутбук: " + ("на связи" if hub.online else "не подключён"))

    @dp.message(F.text)
    async def chat(m: Message) -> None:
        await bot.send_chat_action(m.chat.id, "typing")
        try:
            answer = await brain.reply(m.text)
        except Exception as e:
            log.exception("brain failed")
            answer = f"Ошибка мозга: {type(e).__name__}"
        for i in range(0, len(answer), 4000):
            await m.answer(answer[i : i + 4000])

    log.info("starting, model=%s", cfg.model)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
