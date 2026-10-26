"""The owner's laptop as the agent sees it (the Laptop port), spoken through the hub to hands.py on the laptop.

Windows are real facts from the laptop (title, process, kind), opening a program goes through the Start menu and
Store apps before PATH, and sound is measured, so the tools above can tell «открыл приложение» from «открыл терминал».
"""
from __future__ import annotations

import base64
import json
import logging
import re

from ..ports import Offline, Window
from .hub import HandsOffline, Hub

log = logging.getLogger("jarvis4.laptop")

SNAPSHOT = re.compile(r"snapshot_id:\s*(\S+)")


class LaptopNode:
    name = "laptop"

    def __init__(self, hub: Hub):
        self.hub = hub
        self._snapshot = ""

    @property
    def online(self) -> bool:
        return self.hub.online

    async def _call(self, tool: str, args: dict | None = None, timeout: float = 60) -> dict:
        try:
            return await self.hub.call(tool, args or {}, timeout)
        except HandsOffline as e:
            raise Offline(str(e)) from e

    async def _text(self, tool: str, args: dict | None = None, timeout: float = 60) -> str:
        return (await self._call(tool, args, timeout)).get("text", "")

    async def shell(self, command: str, timeout: int = 60) -> str:
        return await self._text("run_shell", {"command": command, "timeout": timeout}, timeout + 15)

    async def windows(self) -> list[Window]:
        text = await self._text("windows_info")
        try:
            return [Window(w["title"], w.get("process", ""), w.get("kind", "app")) for w in json.loads(text)]
        except (ValueError, KeyError, TypeError):
            # An older hands.py does not know windows_info: titles only, kind unknown
            log.info("windows_info unavailable: %s", text[:120])
            listing = await self._text("list_windows", {"limit": 30})
            return [Window(line.strip(" -•")) for line in listing.splitlines() if line.strip() and "Ошибка" not in line]

    async def look(self) -> str:
        text = await self._text("look")
        found = SNAPSHOT.search(text)
        if found:
            self._snapshot = found.group(1)
        windows = await self.windows()
        shown = ", ".join(f"«{w.title}» ({w.kind})" for w in windows)
        return f"Открытые окна: {shown or 'нет'}.\n{text}"

    async def click(self, element: str) -> str:
        if element.startswith("tb_"):
            return await self._text("taskbar_click", {"which": element})
        return await self._text("ui_action", {"action": "click_element", "params": {"element_id": element},
                                              "snapshot_id": self._snapshot})

    async def open(self, name: str) -> str:
        return await self._text("open_app", {"name": name}, 40)

    async def close(self, title: str) -> str:
        return await self._text("close_window", {"title": title})

    async def focus(self, title: str) -> str:
        return await self._text("focus_window", {"title": title})

    async def type(self, text: str) -> str:
        return await self._text("type_text", {"text": text})

    async def keys(self, keys: str) -> str:
        return await self._text("hotkey", {"keys": keys})

    async def media(self, action: str) -> str:
        return await self._text("media_checked", {"action": action}, 30)

    async def volume(self, percent: int) -> str:
        return await self._text("set_volume", {"percent": percent})

    async def clipboard(self, text: str | None = None) -> str:
        if text is None:
            return await self._text("clipboard_get")
        return await self._text("clipboard_set", {"text": text})

    async def screenshot(self) -> bytes:
        out = await self._call("screenshot")
        if not out.get("image"):
            raise Offline(out.get("text") or "ноутбук не отдал скриншот")
        return base64.b64decode(out["image"])

    async def screen_do(self, name: str, args: dict) -> tuple[str, bytes | None]:
        out = await self._call("screen_do", {"name": name, **args}, 60)
        image = base64.b64decode(out["image"]) if out.get("image") else None
        return out.get("text", ""), image

    # The laptop is also a Screen for the eyes

    async def do(self, name: str, args: dict) -> tuple[str, bytes | None]:
        return await self.screen_do(name, args)

    async def audio(self) -> str:
        return await self._text("audio_status")
