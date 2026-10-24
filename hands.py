"""Jarvis 3.0 hands: connects to the server brain over Tailscale and runs core/actions.py.

The server decides, the laptop executes. Only functions in ALLOWED can run,
whatever the server asks for.
"""
import asyncio
import json
import logging
import os
import queue
import subprocess
import sys
import threading

import aiohttp

from core import actions, desktop, settings  # noqa: F401  settings loads .env

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

# Apps installed outside Start menu, taught by the user: {"telegram": "D:\\...\\Telegram.exe"}
APPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps.json")


def _apps():
    try:
        with open(APPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def add_app(name, path):
    if not os.path.isfile(path):
        raise actions.ActionError("Файла %s нет." % path)
    apps = _apps()
    apps[name.strip().lower()] = path
    with open(APPS_FILE, "w", encoding="utf-8") as f:
        json.dump(apps, f, ensure_ascii=False, indent=1)
    return "Запомнил: «%s» → %s." % (name, path)


def open_app(name):
    path = _apps().get(str(name).strip().lower())
    if path:
        # Launching a running single-instance app (Telegram) brings its window back from the tray
        previous = actions.foreground_window()
        os.startfile(path)
        actions._after_launch(previous, os.path.splitext(os.path.basename(path))[0])
        return "Запускаю %s." % name
    return actions.open_app(name)


# Laptop-side tools that are not plain core/actions functions
LOCAL = {"add_app": add_app, "open_app": open_app}


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
        """Returns {"text": ...} plus "image" (base64 PNG) when a screenshot was taken."""
        if tool not in ALLOWED and tool not in ("look", "ui_action", "add_app"):
            return {"text": "Ошибка: инструмент %s запрещён на ноутбуке." % tool}
        try:
            if tool == "look":
                return look(bool(args.get("screenshot")))
            if tool == "ui_action":
                res = desktop.execute(args.get("action"), args.get("params") or {}, args.get("snapshot_id"))
                return {"text": ("" if res.ok else "Ошибка: ") + res.message}
            result = (LOCAL.get(tool) or getattr(actions, tool))(**args)
        except actions.ActionError as e:
            return {"text": "Ошибка: %s" % e}
        except Exception as e:
            log.exception("action %s failed", tool)
            return {"text": "Ошибка: %s: %s" % (type(e).__name__, e)}
        return {"text": "Готово." if result is None else str(result)}

    async def submit(self, tool, args):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.jobs.put((tool, args, lambda r: loop.call_soon_threadsafe(fut.set_result, r)))
        return await fut


def look(screenshot):
    """UIA snapshot of the active window as compact text, the cheap way for the brain to see."""
    snap = desktop.observe(include_screenshot=screenshot)
    lines = ["snapshot_id: %s" % snap["snapshot_id"], "окно: %s" % snap["window"]["title"]]
    for el in snap["elements"]:
        if not (el["name"] or el["value"] or el["editable"]):
            continue
        b = el["bounds"]
        line = "%s %s «%s»" % (el["id"], el["role"], el["name"][:80])
        if el["value"]:
            line += " = «%s»" % el["value"][:80]
        if el["editable"]:
            line += " [поле ввода]"
        if not el["enabled"]:
            line += " [недоступен]"
        lines.append(line + " @%d,%d" % (b["left"] + b["width"] // 2, b["top"] + b["height"] // 2))
    if snap["truncated"]:
        lines.append("(список обрезан, элементов больше)")
    out = {"text": "\n".join(lines)}
    if snap.get("screenshot"):
        shot = snap["screenshot"]
        out["image"] = shot["base64"]
        out["text"] += "\nСкриншот %dx%d приложен, click_at принимает координаты на нём." % (
            shot["width"], shot["height"])
    return out


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
                log.info("<- %s", result["text"][:300])
                await ws.send_json({"id": data.get("id"), **result})


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
    # pythonw (autostart) has no console, so log to a file next to the script
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        filename=None if sys.stderr else os.path.join(os.path.dirname(__file__), "hands.log"),
                        encoding="utf-8")
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
