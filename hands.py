"""Jarvis 3.0 hands: connects to the server brain and runs tools on this laptop.

The server decides, the laptop executes. Only functions in ALLOWED (core/actions.py)
and LOCAL can run, whatever the server asks for.
"""
import asyncio
import base64
import io
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time

import aiohttp

from core import actions, desktop, probe, routines, server_link, vision

ALLOWED = {
    "open_app", "close_app", "type_text", "hotkey", "list_windows", "focus_window",
    "open_url", "web_search", "set_volume", "set_mute", "media", "set_brightness",
    "system_status", "disk_status", "open_folder", "find_files", "open_path",
    "minimize_window", "maximize_window", "show_desktop", "clipboard_get", "clipboard_set",
}
TOKEN = server_link.TOKEN
SSH_TARGET, HUB, LOCAL_PORT = server_link.SSH_TARGET, server_link.HUB, server_link.LOCAL_PORT
# The voice and the panel (jarvis.py) talk to the brain through the same tunnel
SERVER = os.environ.get("JARVIS_SERVER") or server_link.BASE.replace("http", "ws", 1) + "/hands"

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
    """The program the owner means, not whatever the name is on PATH: taught paths, running apps, the Start menu
    and installed Store apps come first (probe.open_smart); the old lookup is the last resort."""
    return probe.open_smart(name)


def media_checked(action):
    """A player key, then the fact: is sound really coming out afterwards?"""
    said = actions.media(action)
    time.sleep(1.0)
    return "%s %s" % (said or "Готово.", probe.audio_status())


MAX_FILE = 20 * 1024 * 1024  # Telegram bot uploads stop at 50 MB; base64 over WS adds a third


def run_shell(command, timeout=60):
    timeout = max(1, min(int(timeout), 600))
    script = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; " + command
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, timeout=timeout, cwd=os.path.expanduser("~"),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return "Ошибка: команда не уложилась в %d с." % timeout
    out = (proc.stdout + proc.stderr).decode("utf-8", errors="replace").strip()
    if proc.returncode:
        return "Ошибка (код %d):\n%s" % (proc.returncode, out)
    return out or "(пусто, код 0)"


def read_file(path):
    if os.path.getsize(path) > 2 * 1024 * 1024:
        raise actions.ActionError("Файл больше 2 МБ, читай частями через run_shell (Get-Content -TotalCount).")
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def write_file(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return "Записал %d символов в %s." % (len(text), path)


def send_file(path):
    if not os.path.isfile(path):
        raise actions.ActionError("Файла %s нет." % path)
    if os.path.getsize(path) > MAX_FILE:
        raise actions.ActionError("Файл больше 20 МБ, через Telegram-бота не пройдёт.")
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return {"text": "Отправил %s." % os.path.basename(path), "file": data, "name": os.path.basename(path)}


def send_screen():
    from PIL import ImageGrab

    buf = io.BytesIO()
    ImageGrab.grab(all_screens=True).save(buf, format="PNG")
    return {"text": "Скриншот отправлен пользователю.", "file": base64.b64encode(buf.getvalue()).decode(),
            "name": "screen.png"}


def put_file(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as f:
        f.write(base64.b64decode(data))
    return "Сохранил %s." % path


def record_routine(name):
    return routines.start_recording(name)


def stop_recording():
    return routines.stop_recording()


def run_routine(name):
    if not routines.find(name):
        return {"text": "Ошибка: приёма «%s» нет. Есть: %s." % (name, ", ".join(routines.names()) or "пока ни одного"),
                "missing": True}
    return routines.replay(name)


def list_routines():
    found = routines.names()
    return "Выученные приёмы: %s." % ", ".join(found) if found else "Приёмов пока нет."


def forget_routine(name):
    return "Забыл приём." if routines.forget(name) else "Ошибка: такого приёма нет."


# Laptop-side tools that are not plain core/actions functions
LOCAL = {"windows_info": probe.windows_info, "close_window": probe.close_window, "focus_window": probe.focus_window,
         "find_apps": lambda name: json.dumps(probe.find_apps(name), ensure_ascii=False),
         "audio_status": probe.audio_status, "taskbar": probe.taskbar_text, "taskbar_click": probe.taskbar_click,
         "screenshot": probe.screenshot, "screen_do": probe.screen_do, "media_checked": media_checked,
         "add_app": add_app, "open_app": open_app, "run_shell": run_shell, "read_file": read_file,
         "write_file": write_file, "send_file": send_file, "send_screen": send_screen, "put_file": put_file,
         "record_routine": record_routine, "stop_recording": stop_recording, "run_routine": run_routine,
         "list_routines": list_routines, "forget_routine": forget_routine}
# After these the brain's list of routines is out of date
CHANGES_ROUTINES = {"stop_recording", "forget_routine", "record_routine"}


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
        if tool not in ALLOWED and tool not in LOCAL and tool not in ("look", "ui_action"):
            return {"text": "Ошибка: инструмент %s запрещён на ноутбуке." % tool}
        try:
            if tool == "look":
                return look(bool(args.get("screenshot")), bool(args.get("vision")))
            if tool == "ui_action":
                res = desktop.execute(args.get("action"), args.get("params") or {}, args.get("snapshot_id"))
                if not res.ok:
                    return {"text": "Ошибка: " + res.message}
                # Hand back the next look right away: saves the brain a whole round trip
                time.sleep(0.6)
                try:
                    after = look(False)["text"]
                except actions.ActionError as e:
                    after = "(не удалось посмотреть: %s)" % e
                return {"text": res.message + "\n\nПосле действия:\n" + after}
            result = (LOCAL.get(tool) or getattr(actions, tool))(**args)
        except actions.ActionError as e:
            return {"text": "Ошибка: %s" % e}
        except Exception as e:
            log.exception("action %s failed", tool)
            return {"text": "Ошибка: %s: %s" % (type(e).__name__, e)}
        if isinstance(result, dict):
            return result
        return {"text": "Готово." if result is None else str(result)}

    async def submit(self, tool, args):
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.jobs.put((tool, args, lambda r: loop.call_soon_threadsafe(fut.set_result, r)))
        return await fut


def look(screenshot, use_vision=False):
    """UIA snapshot of the active window as compact text, the cheap way for the brain to see.

    use_vision parses the screenshot on the local GPU into text: free, and the picture
    itself goes to the brain only when screenshot is asked for too.
    """
    snap = desktop.observe(include_screenshot=screenshot or use_vision)
    lines = ["snapshot_id: %s" % snap["snapshot_id"], "окно: %s" % snap["window"]["title"]]
    for el in snap["elements"]:
        if not (el["name"] or el["value"] or el["editable"]):
            continue
        b = el["bounds"]
        line = "%s %s «%s»" % (el["id"], el["role"], el["name"][:60])
        if el["value"]:
            line += " = «%s»" % el["value"][:60]
        if el["editable"]:
            line += " [поле ввода]"
        if not el["enabled"]:
            line += " [недоступен]"
        lines.append(line + " @%d,%d" % (b["left"] + b["width"] // 2, b["top"] + b["height"] // 2))
    if snap["truncated"]:
        lines.append("(список обрезан, элементов больше)")
    out = {"text": "\n".join(lines)}
    shot = snap.get("screenshot")
    if shot and use_vision:
        try:
            seen = vision.describe(*vision.parse(shot["base64"]))
            # Vision is asked for when the element list did not help: send it instead of that list
            out["text"] = "\n".join(lines[:2] + [seen])
        except vision.VisionError as e:
            out["text"] += "\n\n(Локальное зрение недоступно: %s.)" % e
    if shot and screenshot:
        out["image"] = shot["base64"]
        out["text"] += "\nСкриншот %dx%d приложен, click_at принимает координаты на нём." % (
            shot["width"], shot["height"])
    return out


async def serve(worker):
    headers = {"Authorization": "Bearer " + TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(SERVER, headers=headers, heartbeat=20) as ws:
            log.info("connected to %s", SERVER)
            # The brain keeps the routine names in its prompt: running one costs no extra step
            await ws.send_json({"event": "routines", "names": routines.names()})
            loop = asyncio.get_running_loop()

            async def recorded(text):
                await ws.send_json({"event": "notice", "text": text})
                await ws.send_json({"event": "routines", "names": routines.names()})

            # A recording stopped by Ctrl+Alt+R ends on the recorder's thread, not in a tool call
            routines.on_finish = lambda text: asyncio.run_coroutine_threadsafe(recorded(text), loop)
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = msg.json()
                log.info("-> %s %s", data.get("tool"), str(data.get("args"))[:300])
                result = await worker.submit(data.get("tool"), data.get("args") or {})
                log.info("<- %s", result["text"][:300])
                await ws.send_json({"id": data.get("id"), **result})
                if data.get("tool") in CHANGES_ROUTINES:
                    await ws.send_json({"event": "routines", "names": routines.names()})


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
