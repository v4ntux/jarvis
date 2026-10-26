"""What the agent needs from the world, as plain interfaces. Real Telegram, the laptop and the server implement
them; so does the exam's pretend world. Tools only ever use these, so the same agent runs in both.

Chats are named by a key: a person's key for a private chat, a group's key for a group, «owner» for the owner.
Messages are named by a ref the store knows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .store import Person


class Offline(Exception):
    """The laptop (or another device) is not connected."""


class TGError(Exception):
    """Telegram refused or failed; the text says why in words the owner can read."""


@dataclass
class CallResult:
    answered: bool
    text: str  # what happened, in words
    transcript: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Window:
    title: str
    process: str = ""
    kind: str = "app"  # app | terminal | browser


class Telegram(Protocol):
    async def lookup(self, query: str) -> Person | str:
        """Someone not in the store yet: by @username, link, +phone or numeric id. A string is the error."""

    async def sync_history(self, chat: str, limit: int) -> None:
        """Bring the newest messages of a chat from Telegram into the store (the world already has them)."""

    async def send(self, chat: str, text: str, files: list[tuple[str, bytes]] | None = None,
                   reply_ref: str = "") -> str:
        """Send; returns the new message's ref."""

    async def forward(self, refs: list[str], to: str) -> list[str]: ...
    async def react(self, ref: str, emoji: str) -> None: ...
    async def sticker(self, chat: str, emoji: str, reply_ref: str = "") -> str: ...
    async def gif(self, chat: str, query: str, reply_ref: str = "") -> str: ...
    async def leave(self, chat: str) -> str: ...
    async def call(self, person: str, mode: str, goal: str) -> CallResult:
        """mode «say»: say goal and hang up. «talk»: a live conversation about goal."""
    async def typing(self, chat: str) -> None: ...


class Laptop(Protocol):
    @property
    def online(self) -> bool: ...
    async def shell(self, command: str, timeout: int = 60) -> str: ...
    async def windows(self) -> list[Window]: ...
    async def look(self) -> str: ...
    async def click(self, element: str) -> str: ...
    async def open(self, name: str) -> str: ...
    async def close(self, title: str) -> str: ...
    async def focus(self, title: str) -> str: ...
    async def type(self, text: str) -> str: ...
    async def keys(self, keys: str) -> str: ...
    async def media(self, action: str) -> str: ...
    async def volume(self, percent: int) -> str: ...
    async def clipboard(self, text: str | None = None) -> str: ...
    async def screenshot(self) -> bytes: ...
    async def screen_do(self, name: str, args: dict) -> tuple[str, bytes | None]:
        """One member of the computer toolset on the real screen: the answer in words, and a picture when asked."""


class Screen(Protocol):
    """Any screen Jarvis can look at and press: the laptop's, a phone's."""
    name: str

    @property
    def online(self) -> bool: ...
    async def do(self, name: str, args: dict) -> tuple[str, bytes | None]: ...
    async def screenshot(self) -> bytes: ...


class Server(Protocol):
    async def shell(self, command: str, timeout: int = 60) -> str: ...
    async def monitor(self) -> str: ...


class Web(Protocol):
    async def search(self, query: str, news: bool = False) -> str: ...
    async def fetch(self, url: str) -> str: ...


@dataclass
class Platform:
    tg: Telegram
    server: Server
    web: Web
    laptop: Laptop | None = None
    extras: dict = field(default_factory=dict)  # phone, android, ssh nodes, ... added as stages arrive
