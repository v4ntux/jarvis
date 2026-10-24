"""Jarvis 3.0 hands: connects to the server brain over Tailscale and runs core/actions.py.

The server decides, the laptop executes. Only functions in ALLOWED can run,
whatever the server asks for.
"""
import asyncio
import logging
import os
import queue
import subprocess
import threading

import aiohttp

from core import actions, settings  # noqa: F401  settings loads .env

ALLOWED = {
    "open_app", "close_app", "type_text", "hotkey", "list_windows", "focus_window",
    "open_url", "web_search", "set_volume", "set_mute", "media", "set_brightness",
    "system_status", "disk_status", "open_folder", "find_files", "open_path",
    "minimize_window", "maximize_window", "show_desktop", "clipboard_get", "clipboard_set",
}
TOKEN = os.environ.get("JARVIS_HANDS_TOKEN", "")
# AmneziaVPN blocks Tailscale TCP, so by default we reach the hub through SSH,
# which already works. Empty JARVIS_SSH means connect to JARVIS_SERVER directly.
SSH_TARGET = os.environ.get("JARVIS_SSH", "administrator@85.190.96.176")
HUB = os.environ.get("JARVIS_HUB", "100.115.94.31:8765")
LOCAL_PORT = 18765
SERVER = os.environ.get("JARVIS_SERVER") or (
    "ws://127.0.0.1:%d/hands" % LOCAL_PORT if SSH_TARGET else "ws://%s/hands" % HUB)

log = logging.getLogger("hands")


class Worker(threading.Thread):
    """One thread runs every action, so the selected window survives between calls."""

    def __init__(self):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()

    def run(self):
        with actions.target_window(0):
            while True:
                tool, args, done = self.jobs.get()
                done(self._execute(tool, args))

    @staticmethod
    def _execute(tool, args):
        if tool not in ALLOWED:
            return "Ошибка: инструмент %s запрещён на ноутбуке." % tool
        try:
            result = getattr(actions, tool)(**args)
        except actions.ActionError as e:
            return "Ошибка: %s" % e
        except Exception as e:
            log.exception("action %s failed", tool)
            return "Ошибка: %s: %s" % (type(e).__name__, e)
        return "Готово." if result is None else str(result)

    async def submit(self, tool, args):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.jobs.put((tool, args, lambda r: loop.call_soon_threadsafe(fut.set_result, r)))
        return await fut


async def serve(worker):
    headers = {"Authorization": "Bearer " + TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SERVER, headers=headers, heartbeat=20) as ws:
            log.info("connected to %s", SERVER)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                log.info("-> %s %s", data.get("tool"), data.get("args"))
                result = await worker.submit(data.get("tool"), data.get("args") or {})
                log.info("<- %s", result)
                await ws.send_json({"id": data.get("id"), "result": result})


async def open_tunnel():
    """Forward 127.0.0.1:LOCAL_PORT to the hub through SSH; returns the ssh process."""
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
        "-L", "127.0.0.1:%d:%s" % (LOCAL_PORT, HUB), SSH_TARGET,
        stdin=asyncio.subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    await asyncio.sleep(3)  # give ssh time to bind the local port
    return proc


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        raise SystemExit("JARVIS_HANDS_TOKEN не задан в .env")
    worker = Worker()
    worker.start()
    tunnel = None
    while True:
        if SSH_TARGET and (tunnel is None or tunnel.returncode is not None):
            tunnel = await open_tunnel()
        try:
            await serve(worker)
        except (aiohttp.ClientError, OSError) as e:
            log.warning("no connection: %s", e)
        await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
