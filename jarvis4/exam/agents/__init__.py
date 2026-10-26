"""Agents that can sit the exam. Each one acts on the pretend world only."""
from __future__ import annotations

from .base import Agent


def make(name: str) -> Agent:
    """scripted:reference | scripted:silent | jarvis4"""
    if name.startswith("scripted:"):
        from .scripted import ReferenceAgent, SilentAgent
        return {"reference": ReferenceAgent, "silent": SilentAgent}[name.split(":", 1)[1]]()
    if name == "jarvis4":
        from .jarvis4 import Jarvis4  # the real core and models in the pretend world: runs on the server
        return Jarvis4()
    raise ValueError(f"нет такого агента: {name}")
