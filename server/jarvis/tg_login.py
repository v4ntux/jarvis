"""One-time login of Jarvis's own Telegram account. The owner runs it by hand on the server:

    cd /opt/jarvis && set -a && . ./.env && set +a && .venv/bin/python -m jarvis.tg_login

It asks for the account's phone number, the code Telegram sends and the 2FA password if
there is one. The session stays in /opt/jarvis/data and never leaves the server.
"""
import asyncio
import glob
import os

from . import config
from .tg_user import display, make_client


async def main() -> None:
    cfg = config.load()
    if not cfg.tg_api_id or not cfg.tg_api_hash:
        raise SystemExit("В /opt/jarvis/.env нет TG_API_ID и TG_API_HASH.")
    os.makedirs(os.path.dirname(cfg.tg_session), exist_ok=True)
    client = make_client(cfg)
    await client.start()  # asks for phone, code and password in this terminal
    me = await client.get_me()
    await client.disconnect()
    for path in glob.glob(cfg.tg_session + ".session*"):
        os.chmod(path, 0o600)
    print(f"\nГотово, Джарвис вошёл как {display(me)}.\nТеперь: sudo systemctl restart jarvis")


if __name__ == "__main__":
    asyncio.run(main())
