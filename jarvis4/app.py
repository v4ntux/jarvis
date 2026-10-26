"""Jarvis 4 as one running thing: the nodes (laptop, server, web, phone), the core, the Telegram account, the loops.

Everything is built here and nowhere else, so a test can build the same thing with fakes (see tests/test_app.py).
"""
from __future__ import annotations

import asyncio
import logging
import os

from . import clock
from .alerts import Alerts
from .commands import Commands
from .config import Config
from .core import Core
from .gem import Gemini
from .nodes.hub import Hub
from .nodes.laptop import LaptopNode
from .nodes.server import ServerNode, WebNode
from .ops.monitor import Monitor
from .ops.phone import Phone
from .ports import Platform
from .store import Store
from .tg.account import Account
from .types import Interaction, Mode
from .watch import Watchers

log = logging.getLogger("jarvis4.app")

LAPTOP_MARK = "[голосом у ноутбука] "


class CaptureOut:
    """An Out that keeps the answer instead of sending it: the laptop's panel and voice read it out themselves."""

    def __init__(self, copy):
        self.copy, self.lines = copy, []

    async def say(self, text: str, reply_ref: str = "") -> str:
        self.lines.append(text)
        return ""

    async def ask(self, question: str) -> bool:
        return await self.copy.ask(question)

    async def progress(self, line: str) -> None:
        await self.copy.progress(line)

    async def send_file(self, name: str, data: bytes) -> None:
        await self.copy.send_file(name, data)

    async def typing(self) -> None:
        pass

    async def finish(self) -> None:
        await self.copy.finish()


class App:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.db_path)
        self.gem = Gemini(cfg)
        self.hub = Hub(cfg.hands_token)
        self.laptop = LaptopNode(self.hub)
        self.server = ServerNode()
        self.phone = Phone(cfg, self.store)
        self.account = Account(cfg, self.store, self.gem)
        self.platform = Platform(tg=self.account, server=self.server, web=WebNode(), laptop=self.laptop,
                                 extras={"phone": self.phone, "screens": {"laptop": self.laptop}})
        self.core = Core(cfg, self.store, self.platform, outs=self.account.out_for)
        self.alerts = Alerts(self.core, self.account, self.phone, self.store)
        self.monitor = Monitor(cfg, self.store, self.alerts.monitor_news)
        self.server.monitor_ = self.monitor
        self.watchers = Watchers(self.store, self.platform, self.core.on_watchers, self.core.frozen)
        self.commands = Commands(self)
        self.core.commands = self.commands.run
        self.account.tell_owner = self.alerts.say
        self.hub.on_ask, self.hub.on_stop = self.ask_from_laptop, self.stop_from_laptop
        self.tasks: set[asyncio.Task] = set()

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # ---- the laptop's own voice and panel ----------------------------------------------------------------------

    async def ask_from_laptop(self, text: str) -> str:
        """The laptop's voice or panel asks; the answer goes back to be read aloud, and the chat keeps a copy."""
        core = self.core
        out = CaptureOut(core.out_for("owner"))
        itx = Interaction(kind="owner", chat="owner", sender="owner", text=LAPTOP_MARK + text, msgs=[], owner=True,
                          out=out, mode=Mode.WORK)
        while core.current is not None and not core.current.done():
            await asyncio.sleep(0.3)
        core.current = asyncio.create_task(core.interact(itx))
        try:
            await core.current
        except asyncio.CancelledError:
            return "остановил"
        finally:
            core.current = None
        answer = "\n".join(out.lines).strip() or "готово"
        await self.alerts.say(f"🎙 с ноутбука: {text}\n{answer}")
        return answer

    def stop_from_laptop(self) -> bool:
        if not self.core.busy():
            return False
        self.spawn(self.core.stop_all("owner"))
        return True

    # ---- run ---------------------------------------------------------------------------------------------------

    async def start(self) -> bool:
        cfg = self.cfg
        os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
        await self.hub.start(cfg.hands_host, cfg.hands_port)
        try:
            ok = await self.account.start(self.core)
        except Exception:
            log.exception("own Telegram account failed to start")
            ok = False
        self.spawn(self.alerts.reminders_loop())
        self.spawn(self.monitor.loop())
        self.spawn(self.watchers.loop())
        log.info("Jarvis 4 is up: telegram %s, models %s / %s / %s, today %s",
                 "on" if ok else "OFF", cfg.mouth_model, cfg.brain_model, cfg.deep_model, clock.now().strftime("%d.%m %H:%M"))
        return ok

    async def run(self) -> None:
        ok = await self.start()
        if ok:
            await self.account.client.disconnected
            raise SystemExit("own Telegram account disconnected")  # systemd starts Jarvis again
        log.error("Telegram is not connected: only the laptop's voice works")
        await asyncio.Event().wait()
