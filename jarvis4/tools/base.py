"""What a tool is: a name and schema for the model, a risk class for the guard, and code that does the thing."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .. import guard
from ..store import Person, Store
from ..types import Interaction, Result


class Risk(enum.Enum):
    READ = "read"  # looks, never changes
    OWN = "own"  # changes Jarvis's own things or the owner's machines: done without asking
    OUT = "out"  # reaches other people: only those the owner named, paced
    RED = "red"  # the red list: asks once


@dataclass
class Ctx:
    core: Any  # jarvis4.core.Core
    itx: Interaction

    @property
    def store(self) -> Store:
        return self.core.store

    @property
    def pf(self):
        return self.core.platform

    @property
    def cfg(self):
        return self.core.cfg

    def owner_words(self) -> str:
        """Everything the owner said lately: a name in here is a person he named himself."""
        said = [m.text for m in self.store.recent("owner", 12) if m.sender == "owner" and m.text]
        said += [m.text for m in self.itx.msgs if m.sender == "owner" and m.text]
        return "\n".join(said + [self.itx.text if self.itx.owner else ""])


GuardFn = Callable[[Ctx, dict], Awaitable[guard.Verdict | None]]
RunFn = Callable[[Ctx, dict], Awaitable[Result]]
LabelFn = Callable[[dict, Result], str]


@dataclass
class Tool:
    name: str
    description: str
    props: dict = field(default_factory=dict)
    required: list[str] = field(default_factory=list)
    run: RunFn | None = None
    risk: Risk = Risk.READ
    terminal: bool = False  # nothing to think about afterwards: the turn can end after it
    light: bool = False  # the mouth (Haiku) may use it without handing the task to the brain
    others: bool = False  # strangers and group members' chats may trigger it
    guard: GuardFn | None = None
    label: LabelFn | None = None

    def spec(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": self.props, "required": self.required}}

    def line(self, args: dict, result: Result) -> str:
        if self.label:
            return self.label(args, result)
        return self.name


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}


def owner_person(store: Store) -> Person:
    return store.person("owner") or store.upsert_person("owner", "Ван", aliases=["ван", "van", "батя", "хозяин"],
                                                        dialog=True)
