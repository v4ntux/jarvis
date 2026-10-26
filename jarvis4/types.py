"""The few shapes everything shares: one interaction, how it talks back, and the results tools return."""
from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from typing import Protocol

from .store import Msg

_ids = itertools.count(1)


class Out(Protocol):
    """How one interaction talks back: the chat it came from (Telegram), or the log (exam, tests)."""

    async def say(self, text: str, reply_ref: str = "") -> str: ...
    async def ask(self, question: str) -> bool: ...  # a yes/no question to the owner
    async def progress(self, line: str) -> None: ...  # the one task message that keeps updating
    async def send_file(self, name: str, data: bytes) -> None: ...
    async def typing(self) -> None: ...
    async def finish(self) -> None: ...  # the task is over: take the status line away


class Mode(str, enum.Enum):
    CHAT = "chat"  # small talk: jokes, stickers, slang
    WORK = "work"  # a task: short, to the point
    ANGRY = "angry"  # the owner is angry: no jokes at all


@dataclass
class Interaction:
    """One thing Jarvis answers: a burst of messages from one chat, a fired watcher, a timer."""
    kind: str  # owner | person | group | event
    chat: str  # where answers go
    sender: str
    text: str
    msgs: list[Msg]
    owner: bool
    out: Out
    id: str = field(default_factory=lambda: f"t{next(_ids)}")
    think: bool = False  # the owner said «подумай»: Opus may be used
    spontaneous: bool = False  # nobody addressed Jarvis, he chose to speak (groups)
    mode: Mode = Mode.CHAT
    spent: float = 0.0
    approved: set[str] = field(default_factory=set)  # yes/no questions already answered yes in this task
    declined: bool = False  # the owner said no to something: nothing is pushed against that
    images: list[str] = field(default_factory=list)  # base64 pictures the owner sent
    note: str = ""  # extra line for the model (a fired watcher, the urge to send a sticker)
    spoken: bool = False  # the owner sent a voice message: the answer may come back as a voice too
    skills_used: list[str] = field(default_factory=list)  # skills the model opened (skill_get) in this task

    @property
    def ref(self) -> str:
        return self.msgs[-1].ref if self.msgs else ""


@dataclass
class Result:
    ok: bool
    text: str
    evidence: str = ""  # what proves it (a window list, a message id): goes into the journal and to the checker
    image: bytes | None = None
    handoff: str = ""  # do_task: the goal for the brain


def fail(text: str) -> Result:
    return Result(False, text if text.startswith("Ошибка") else "Ошибка: " + text)
