"""One-time login of Jarvis's own Telegram account. The owner runs it by hand on the server and types the code himself:

    cd /opt/jarvis && set -a && . ./.env && set +a && .venv/bin/python -m jarvis4.login [+phone]

It asks for the account's phone (unless given), the code Telegram sends and the 2FA password if there is one. The session
stays on the server (mode 600) and is only ever opened by the running service.
"""
from __future__ import annotations

import asyncio
import glob
import os
import sys

from telethon import TelegramClient

from . import config


async def main() -> None:
    cfg = config.load()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        raise SystemExit("В .env нет TG_API_ID и TG_API_HASH (их выдаёт my.telegram.org).")
    os.makedirs(os.path.dirname(cfg.tg_session) or ".", exist_ok=True)
    client = TelegramClient(cfg.tg_session, cfg.tg_api_id, cfg.tg_api_hash, device_model="Jarvis Server",
                            system_version="Ubuntu", app_version="4.0", lang_code="ru", system_lang_code="ru-RU")
    await client.start(*sys.argv[1:2])  # asks in this terminal for the phone (unless given), the code and the password
    me = await client.get_me()
    await client.disconnect()
    for path in glob.glob(cfg.tg_session + ".session*"):
        os.chmod(path, 0o600)
    name = " ".join(p for p in (me.first_name, me.last_name) if p) or me.username or str(me.id)
    print(f"\nГотово, Джарвис вошёл как {name}. Теперь: sudo systemctl restart jarvis4")


if __name__ == "__main__":
    asyncio.run(main())
