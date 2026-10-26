"""python -m jarvis4: the real thing, on the server, with /opt/jarvis/.env loaded by systemd."""
import asyncio
import logging

from . import config
from .app import App


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("primp", "httpcore", "aiohttp.access", "telethon.client.downloads", "telethon.network", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one line per request drowns the real log
    cfg = config.load()
    if not cfg.anthropic_key:
        raise SystemExit("ANTHROPIC_API_KEY не задан")
    asyncio.run(App(cfg).run())


if __name__ == "__main__":
    main()
