"""Agents with no model, for testing the exam itself.

ReferenceAgent plays each scenario's `reference` (a known good run), so every scenario must pass with it:
that proves the expectations can be met. SilentAgent only says «Готово.», so it must fail them.
"""
from __future__ import annotations

from ..scenario import Event
from ..world import Message
from .base import Agent

# Ops that are Jarvis talking in the chat, not tool calls
TALK = {"say", "react", "sticker", "gif"}


class ReferenceAgent(Agent):
    name = "scripted:reference"

    async def deliver(self, index: int, event: Event, msg: Message | None) -> None:
        ops = self.scenario.reference[index] if index < len(self.scenario.reference) else []
        for op in ops:
            self.play(dict(op), event, msg)

    def play(self, op: dict, event: Event, msg: Message | None) -> str:
        w, name = self.world, op.pop("op")
        if name not in TALK:
            w.note_tool(name, op)
        chat = op.get("chat", event.chat)
        if name == "say":
            w.say(chat, op["text"], reply_to=msg.key if msg else None)
            return ""
        if name == "react":
            w.react(chat, op.get("msg") or (msg.key if msg else None), op["emoji"])
            return ""
        if name == "sticker":
            w.sticker(chat, op.get("emoji", ""), op.get("about", ""))
            return ""
        if name == "gif":
            w.gif(chat, op.get("query", ""))
            return ""
        acts = {
            "send": lambda: w.send(op["to"], op.get("text", ""), op.get("files")),
            "forward": lambda: w.forward(op["from"], op["msgs"], op.get("to", "owner")),
            "call": lambda: w.call(op["to"], op.get("mode", "say"), op.get("text", "")),
            "leave": lambda: w.leave(op["group"]),
            "open_app": lambda: w.open_app(op["name"]),
            "close": lambda: w.close(op["name"]),
            "click": lambda: w.click(op["element"]),
            "laptop_shell": lambda: w.laptop_shell(op["cmd"]),
            "server_shell": lambda: w.server_shell(op["cmd"]),
            "monitor": w.monitor,
            "remember": lambda: w.remember(op["text"]),
            "lesson": lambda: w.lesson(op["text"]),
            "remove_watcher": lambda: w.remove_watcher(op["id"]),
            "media": lambda: w.media(op["action"]),
            "type": lambda: w.type_text(op["text"]),
            "clipboard": lambda: w.set_clipboard(op["text"]),
        }
        return acts[name]()


class SilentAgent(Agent):
    name = "scripted:silent"

    async def deliver(self, index: int, event: Event, msg: Message | None) -> None:
        if event.kind == "message":
            self.world.say(event.chat, "Готово.")
