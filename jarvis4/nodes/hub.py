"""Hub for the laptop (inside Tailscale or an SSH tunnel).

/hands  WebSocket: the laptop executes the brain's tools.
/ask    the laptop's own voice and chat panel ask the brain; /stop cancels.
"""
import asyncio
import hmac
import itertools
import logging
import time
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web

log = logging.getLogger("jarvis4.hub")


class HandsOffline(RuntimeError):
    pass


class Hub:
    def __init__(self, token: str):
        self.token = token
        self.ws: web.WebSocketResponse | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.ids = itertools.count(1)
        # When the laptop last connected or dropped, for the «laptop is gone» watcher
        self.since = time.time()
        # Set by the bot: answer a question spoken at the laptop, stop the current task
        self.on_ask: Callable[[str], Awaitable[str]] | None = None
        self.on_stop: Callable[[], bool] | None = None
        # The laptop reports on its own: its learned routines, and news for the owner
        self.routines: list[str] = []
        self.on_notice: Callable[[str], Awaitable[None]] | None = None
        self._notices: set[asyncio.Task] = set()

    @property
    def online(self) -> bool:
        return self.ws is not None and not self.ws.closed

    async def call(self, tool: str, args: dict, timeout: float = 60) -> dict:
        if not self.online:
            raise HandsOffline("Ноутбук не подключён.")
        call_id = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[call_id] = fut
        try:
            await self.ws.send_json({"id": call_id, "tool": tool, "args": args})
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"text": "Ошибка: ноутбук не ответил вовремя."}
        finally:
            self.pending.pop(call_id, None)

    def _check(self, request: web.Request) -> None:
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.token):
            raise web.HTTPUnauthorized()

    async def ask(self, request: web.Request) -> web.Response:
        self._check(request)
        text = str((await request.json()).get("text") or "").strip()
        if not text or not self.on_ask:
            raise web.HTTPBadRequest()
        return web.json_response({"answer": await self.on_ask(text)})

    async def stop(self, request: web.Request) -> web.Response:
        self._check(request)
        return web.json_response({"stopped": bool(self.on_stop and self.on_stop())})

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        self._check(request)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=32 * 1024 * 1024)  # screenshots
        await ws.prepare(request)
        if self.online:
            await self.ws.close()
        self.ws = ws
        self.since = time.time()
        log.info("laptop connected from %s", request.remote)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = msg.json()
            if data.get("event") == "routines":
                self.routines = [str(n) for n in data.get("names") or []][:100]
                continue
            if data.get("event") == "notice" and self.on_notice:
                task = asyncio.create_task(self.on_notice(str(data.get("text", ""))[:3500]))
                self._notices.add(task)
                task.add_done_callback(self._notices.discard)
                continue
            fut = self.pending.get(data.get("id"))
            if fut and not fut.done():
                data.pop("id")
                data["text"] = str(data.get("text", ""))
                fut.set_result(data)  # text, plus optional image / file+name
        if self.ws is ws:
            self.ws = None
            self.since = time.time()
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_result({"text": "Ошибка: ноутбук отключился."})
        log.info("laptop disconnected")
        return ws

    async def start(self, host: str, port: int) -> None:
        app = web.Application()
        app.router.add_get("/hands", self.handle)
        app.router.add_post("/ask", self.ask)
        app.router.add_post("/stop", self.stop)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("hub listening on %s:%d", host, port)
