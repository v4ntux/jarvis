"""Agents that can sit the exam. Each one acts on the pretend world only."""
from __future__ import annotations

from .base import Agent


def make(name: str) -> Agent:
    """scripted:reference | scripted:silent | jarvis3"""
    if name.startswith("scripted:"):
        from .scripted import ReferenceAgent, SilentAgent
        return {"reference": ReferenceAgent, "silent": SilentAgent}[name.split(":", 1)[1]]()
    if name == "jarvis3":
        from .jarvis3 import Jarvis3  # needs the old bot's dependencies and a real API key: runs on the server
        return Jarvis3()
    raise ValueError(f"нет такого агента: {name}")
