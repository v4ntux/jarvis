"""WebSocket hub: the laptop connects here (inside Tailscale) and executes tools."""
import asyncio
import hmac
import itertools
import logging
import time

from aiohttp import WSMsgType, web

log = logging.getLogger("jarvis.hub")


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

    async def handle(self, request: web.Request) -> web.WebSocketResponse:
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.token):
            raise web.HTTPUnauthorized()
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
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, host, port).start()
        log.info("hub listening on %s:%d", host, port)
