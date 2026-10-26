"""Test helpers: a model that follows a script, so the agent loop, the guard and the core are tested without money."""
from __future__ import annotations

import inspect
import itertools
from typing import Callable

from .llm import Call, Reply

_ids = itertools.count(1)


def say(text: str, cost: float = 0.001) -> Reply:
    return Reply([{"type": "text", "text": text}], text=text, stop="end_turn", cost=cost)


def use(_tool: str, _say: str = "", _cost: float = 0.001, **args) -> Reply:
    """A reply that calls one tool (and may say something beside it). Keyword arguments are the tool's arguments."""
    return use_many([(_tool, args)], _say, _cost)


def use_many(calls: list[tuple[str, dict]], text: str = "", cost: float = 0.001) -> Reply:
    blocks = [{"type": "text", "text": text}] if text else []
    made = []
    for name, args in calls:
        call = Call(f"toolu_{next(_ids)}", name, args)
        made.append(call)
        blocks.append({"type": "tool_use", "id": call.id, "name": name, "input": args})
    return Reply(blocks, text=text, calls=made, stop="tool_use", cost=cost)


class ScriptedLLM:
    """complete() returns the next scripted reply; a step may be a function (model, system, messages, tools) → Reply.

    `calls` keeps what the agent asked, for tests that look at the prompt or the tool list.
    """

    def __init__(self, script: list[Reply | Callable], store=None):
        self.script = list(script)
        self.calls: list[dict] = []
        self.store = store
        self.ready = True

    async def complete(self, model, system, messages, tools=None, max_tokens=4000, effort="medium", kind="",
                       schema=None) -> Reply:
        self.calls.append({"model": model, "system": system, "messages": [dict(m) for m in messages],
                           "tools": [t.get("name") or t.get("type") for t in tools or []], "kind": kind, "effort": effort})
        if not self.script:
            raise AssertionError(f"скрипт кончился; спросили {model} ({kind}); последнее: {messages[-1]!r:.300}")
        step = self.script.pop(0)
        reply = step(model, system, messages, tools) if callable(step) else step
        if inspect.isawaitable(reply):
            reply = await reply
        if self.store is not None and reply.cost:
            self.store.log_usage(model, 100, 50, reply.cost, kind)
        reply.model = model
        return reply
