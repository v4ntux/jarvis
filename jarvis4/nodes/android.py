"""The owner's Android phone, over ADB (through Tailscale): a screen to look at and press, notifications, apps.

As a Screen it speaks the same computer-toolset words as the laptop, so the eyes work on the phone unchanged: a click is
a tap, a drag is a swipe, scroll is a swipe, a right click is a long press, keys are Android keys. Pictures are scaled with
ffmpeg (the server has it, and Pillow is not needed). Nothing here is installed on the phone except what the owner turns
on himself: «Отладка по USB» (once, with a cable, then `adb tcpip 5555`) or «Отладка по Wi-Fi».
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import shlex
import struct

from ..ports import Offline

log = logging.getLogger("jarvis4.android")

LONG_EDGE = 1280
ADB_KEYBOARD = "com.android.adbkeyboard/.AdbIME"
KEYS = {"return": 66, "enter": 66, "back": 4, "escape": 4, "esc": 4, "home": 3, "menu": 82, "tab": 61, "space": 62,
        "delete": 67, "backspace": 67, "up": 19, "down": 20, "left": 21, "right": 22, "volume_up": 24, "volume_down": 25,
        "power": 26, "recent": 187, "app_switch": 187, "search": 84, "page_up": 92, "page_down": 93}
SCROLL = 0.35  # of the screen height for one wheel click
NOTE = re.compile(r"NotificationRecord\(.*?pkg=(\S+)")
TITLE = re.compile(r"android\.title=(?:String \()?(.+?)\)?\s*$")
TEXT = re.compile(r"android\.text=(?:String \()?(.+?)\)?\s*$")


class AndroidError(RuntimeError):
    pass


def png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n" or len(png) < 24:
        raise AndroidError("телефон отдал не картинку")
    return struct.unpack(">II", png[16:24])


class AndroidNode:
    name = "android"

    def __init__(self, target: str = "", adb: str = "adb"):
        self.target, self.adb = target, adb
        self._online = False
        self.scale = 1.0
        self.size = (0, 0)  # device pixels, as of the last screenshot

    @property
    def online(self) -> bool:
        return self._online

    # ---- adb ---------------------------------------------------------------------------------------------------

    async def _run(self, *args: str, timeout: float = 20, binary: bool = False, serial: bool = True) -> tuple[int, bytes]:
        cmd = [self.adb, *(["-s", self.target] if self.target and serial else []), *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL if binary else asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            return 127, "adb не установлен на сервере (apt install adb)".encode()
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "adb не ответил вовремя".encode()
        return proc.returncode, out

    async def refresh(self) -> bool:
        """Reconnects when the phone dropped (a network target) and says whether it is there."""
        if self.target and ":" in self.target:
            code, out = await self._run("get-state", timeout=8)
            if code != 0 or b"device" not in out:
                await self._run("connect", self.target, timeout=10, serial=False)
        code, out = await self._run("get-state", timeout=8)
        self._online = code == 0 and out.strip() == b"device"
        return self._online

    async def poll_loop(self, every: float = 30) -> None:
        while True:
            try:
                before = self._online
                await self.refresh()
                if before != self._online:
                    log.info("android %s: %s", self.target or "usb", "on" if self._online else "off")
            except Exception:
                log.exception("android poll failed")
            await asyncio.sleep(every)

    async def shell(self, command: str, timeout: float = 30) -> str:
        if not self._online and not await self.refresh():
            raise Offline("телефон не на связи по ADB")
        code, out = await self._run("shell", command, timeout=timeout)
        text = out.decode(errors="replace").strip()
        return f"(код {code})\n{text}" if code else (text or "(пусто, код 0)")

    # ---- the screen --------------------------------------------------------------------------------------------

    async def _scale(self, png: bytes, width: int, height: int, crop: tuple[int, int, int, int] | None = None) -> bytes:
        vf = (f"crop={crop[2] - crop[0]}:{crop[3] - crop[1]}:{crop[0]}:{crop[1]}," if crop else "") + f"scale={width}:{height}"
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-loglevel", "error", "-f", "image2pipe", "-c:v", "png", "-i", "pipe:0", "-vf", vf,
            "-f", "image2pipe", "-c:v", "png", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate(png)
        if proc.returncode != 0 or not out:
            raise AndroidError("ffmpeg не смог уменьшить скриншот")
        return out

    async def _capture(self) -> bytes:
        if not self._online and not await self.refresh():
            raise Offline("телефон не на связи по ADB")
        code, png = await self._run("exec-out", "screencap", "-p", binary=True, timeout=25)
        if code != 0:
            raise Offline("не удалось снять экран телефона (экран выключен или нет доступа)")
        return png

    async def screenshot(self) -> bytes:
        png = await self._capture()
        width, height = png_size(png)
        self.size = (width, height)
        self.scale = min(1.0, LONG_EDGE / max(width, height))
        if self.scale < 1.0:
            png = await self._scale(png, round(width * self.scale), round(height * self.scale))
        return png

    def _real(self, point) -> tuple[int, int]:
        if not self.size[0]:
            raise AndroidError("сначала нужен скриншот: я ещё не знаю размер экрана")
        x, y = round(float(point[0]) / self.scale), round(float(point[1]) / self.scale)
        return max(0, min(self.size[0] - 1, x)), max(0, min(self.size[1] - 1, y))

    async def _input(self, *args: str) -> str:
        code, out = await self._run("shell", "input", *args, timeout=15)
        if code != 0:
            raise AndroidError(out.decode(errors="replace").strip()[:200] or "ввод не удался")
        return "OK"

    async def do(self, name: str, a: dict) -> tuple[str, bytes | None]:
        if name == "screenshot":
            return "Скриншот", await self.screenshot()
        if name == "zoom":
            png = await self._capture()
            (x0, y0), (x1, y1) = self._real(a["region"][:2]), self._real(a["region"][2:])
            if x1 <= x0 or y1 <= y0:
                raise AndroidError("область для zoom пустая")
            factor = min(LONG_EDGE / max(x1 - x0, y1 - y0), 3.0)
            return "Область увеличена.", await self._scale(png, max(2, round((x1 - x0) * factor) // 2 * 2),
                                                           max(2, round((y1 - y0) * factor) // 2 * 2), (x0, y0, x1, y1))
        if name in ("left_click", "double_click", "triple_click"):
            x, y = self._real(a["coordinate"])
            for _ in range({"double_click": 2, "triple_click": 3}.get(name, 1)):
                await self._input("tap", str(x), str(y))
            return "OK", None
        if name == "right_click":  # a long press is what a right click is on a phone
            x, y = self._real(a["coordinate"])
            return await self._input("swipe", str(x), str(y), str(x), str(y), "700"), None
        if name == "left_click_drag":
            (x0, y0), (x1, y1) = self._real(a["start_coordinate"]), self._real(a["coordinate"])
            return await self._input("swipe", str(x0), str(y0), str(x1), str(y1), "450"), None
        if name == "scroll":
            return await self._scroll(a), None
        if name == "type":
            return await self._type(str(a.get("text", ""))), None
        if name == "key":
            return await self._key(str(a.get("text", "")), int(a.get("repeat") or 1)), None
        if name == "wait":
            await asyncio.sleep(min(float(a.get("duration") or 1), 60))
            return "OK", None
        raise AndroidError(f"на телефоне нет действия «{name}» (нет мыши и курсора): нажимай, свайпай, печатай и жми клавиши")

    async def _scroll(self, a: dict) -> str:
        width, height = self.size
        x, y = self._real(a["coordinate"]) if a.get("coordinate") else (width // 2, height // 2)
        reach = round(height * SCROLL * max(1, min(int(a.get("scroll_amount") or 3), 10)) / 3)
        direction = a.get("scroll_direction", "down")
        # the content moves the way the wheel says, so the finger moves the other way
        dx, dy = {"down": (0, -reach), "up": (0, reach), "right": (-reach, 0), "left": (reach, 0)}[direction]
        x1, y1 = max(0, min(width - 1, x + dx)), max(0, min(height - 1, y + dy))
        return await self._input("swipe", str(x), str(y), str(x1), str(y1), "350")

    async def _key(self, combo: str, repeat: int) -> str:
        code = KEYS.get(combo.strip().lower())
        if code is None and re.fullmatch(r"[a-z0-9]", combo.strip().lower()):
            return await self._type(combo.strip())
        if code is None:
            raise AndroidError(f"клавиши «{combo}» на телефоне нет. Есть: " + ", ".join(sorted(set(KEYS))))
        for _ in range(max(1, min(repeat, 20))):
            await self._input("keyevent", str(code))
        return "OK"

    async def _type(self, text: str) -> str:
        if text.isascii():
            await self._input("text", text.replace(" ", "%s").replace("\n", "%s"))
            return "OK"
        code, out = await self._run("shell", "ime", "list", "-s", timeout=10)
        if ADB_KEYBOARD.encode() not in out:
            raise AndroidError("русские буквы на телефон печатаются только через приложение ADBKeyBoard (бесплатное, "
                               "github.com/senzhk/ADBKeyBoard): поставь его на телефон. Английские работают и так.")
        await self._run("shell", "ime", "set", ADB_KEYBOARD, timeout=10)
        encoded = base64.b64encode(text.encode()).decode()
        code, out = await self._run("shell", "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", encoded, timeout=10)
        if code != 0:
            raise AndroidError(out.decode(errors="replace")[:200])
        return "OK"

    # ---- facts about the phone ---------------------------------------------------------------------------------

    async def status(self) -> str:
        parts = []
        for label, command, pattern in (
                ("модель", "getprop ro.product.model", None),
                ("Android", "getprop ro.build.version.release", None),
                ("батарея", "dumpsys battery", r"level: (\d+)"),
                ("экран", "dumpsys power", r"mWakefulness=(\w+)"),
                ("сейчас открыто", "dumpsys window", r"mCurrentFocus=Window\{\S+ \S+ ([^\s}]+)")):
            text = await self.shell(command, 15)
            found = re.search(pattern, text) if pattern else None
            value = found.group(1) if found else (text.splitlines()[-1] if not pattern and text else "?")
            parts.append(f"{label}: {value}" + ("%" if label == "батарея" else ""))
        return "; ".join(parts)

    async def notifications(self, limit: int = 15) -> str:
        text = await self.shell("dumpsys notification --noredact", 30)
        found, current = [], None
        for line in text.splitlines():
            pkg = NOTE.search(line)
            if pkg:
                current = {"pkg": pkg.group(1), "title": "", "text": ""}
                found.append(current)
            elif current is not None:
                title, body = TITLE.search(line.strip()), TEXT.search(line.strip())
                if title and not current["title"]:
                    current["title"] = title.group(1)[:100]
                if body and not current["text"]:
                    current["text"] = body.group(1)[:200]
        shown = [f"{n['pkg']}: {n['title']} — {n['text']}".strip(" —:") for n in found if n["title"] or n["text"]]
        return "\n".join(shown[:limit]) or "Уведомлений нет."

    async def open_app(self, package: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.]+", package):
            raise AndroidError("имя пакета выглядит странно")
        out = await self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1", 20)
        if "No activities found" in out or "monkey aborted" in out:
            raise AndroidError(f"пакет {package} не запускается (нет такого приложения?)")
        return f"Запускаю {package}."

    async def apps(self, query: str = "") -> str:
        out = await self.shell("pm list packages -3", 20)
        names = [line.removeprefix("package:") for line in out.splitlines() if line.startswith("package:")]
        wanted = query.strip().lower()
        hit = [n for n in names if wanted in n.lower()] if wanted else names
        return "\n".join(hit[:60]) or "Таких приложений нет."
