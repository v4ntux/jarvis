"""The tool catalog, and which tools each speaker gets.

The mouth (Haiku) gets the light tools plus do_task. The brain gets everything except do_task. Strangers and
group members' chats get only the harmless few.
"""
from __future__ import annotations

from . import handoff, laptop, memory, phone, server, telegram
from .base import Ctx, Risk, Tool


def all_tools() -> dict[str, Tool]:
    found = {}
    for module in (telegram, laptop, server, memory, handoff, phone):
        for tool in module.tools():
            found[tool.name] = tool
    return found


def for_role(registry: dict[str, Tool], role: str, owner: bool, laptop_online: bool = True) -> list[Tool]:
    """role: «mouth» or «brain»."""
    if not owner:
        return [t for t in registry.values() if t.others]
    if role == "mouth":
        return [t for t in registry.values() if t.light and t.name != "pass_to_owner"]
    return [t for t in registry.values() if t.name not in ("do_task", "pass_to_owner")]


__all__ = ["Ctx", "Risk", "Tool", "all_tools", "for_role"]
