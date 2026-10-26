"""The agent loop: think, act with tools, look at the result, repeat. One loop serves both speeds.

The mouth (Haiku) answers chat and does the light things itself; for real work it calls do_task and stops. The brain
runs the task on the cheapest model that fits it and climbs the ladder after two failures in a row. Rules that must
hold (the guard, the journal, the budget, «стоп») live outside the model, in Core.run_tool and here.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from . import context
from .budget import OutOfMoney, Tier
from .llm import LLMError
from .types import Interaction, Mode, Result

log = logging.getLogger("jarvis4.agent")

MOUTH_PROMPT = """Ты сейчас быстрый «рот» Джарвиса: отвечаешь в чате сразу. Простое делай сам инструментами (tg_*, поиск, \
память, журнал, мониторинг). Всё, что требует ноутбука, сервера, экрана, файлов или многих шагов, отдавай через do_task \
и больше ничего не пиши: мозг сделает и сам ответит. Если нужно действие (скинуть, переслать, позвонить, написать, \
открыть, закрыть, проверить), его делает инструмент: не говори, что сделал, пока инструмент не вернул успех."""
BRAIN_PROMPT = """Ты сейчас «мозг» Джарвиса: тебе передали задачу, выполни её сам инструментами. Ответ Вану после дела \
короткий: что сделано и чего не вышло."""

SPEAK = ("[система] Ты сделал дело (или оно не вышло), а Вану не сказал ни слова. Скажи ему одной-двумя короткими "
         "фразами, что вышло. Если не вышло или человек не взял трубку, так и скажи; ничего не выдумывай.")
MAX_RESULT = 5000
KEEP_IMAGES = 1
TIER_FOR = {"simple": Tier.MOUTH, "normal": Tier.BRAIN, "deep": Tier.DEEP}


@dataclass
class Outcome:
    text: str = ""
    handoff: str = ""  # «effort:goal» when the mouth passed the task on
    silent: bool = False  # nothing to say (a refusal, an empty answer in a group)
    steps: int = 0
    tools_used: int = 0
    stopped_by: str = ""  # money | steps | error


def prune_images(messages: list[dict]) -> None:
    """Only the newest picture stays in the conversation: old screenshots are the main cost of watching a screen."""
    seen = 0
    for msg in reversed(messages):
        content = msg["content"]
        if msg["role"] != "user" or not isinstance(content, list):
            continue
        holders = [content] + [b["content"] for b in content if isinstance(b, dict) and isinstance(b.get("content"), list)]
        for items in holders:
            for i in range(len(items) - 1, -1, -1):
                part = items[i]
                if isinstance(part, dict) and part.get("type") == "image":
                    seen += 1
                    if seen > KEEP_IMAGES:
                        items[i] = {"type": "text", "text": "(старая картинка убрана)"}


class Agent:
    def __init__(self, core, role: str):
        self.core, self.role = core, role

    def _tools(self, itx: Interaction):
        from .tools import for_role
        return for_role(self.core.registry, self.role, itx.owner)

    def _effort(self, itx: Interaction, tier: Tier) -> str:
        if tier >= Tier.DEEP:
            return "high"
        return "low" if itx.mode == Mode.CHAT and self.role == "mouth" else "medium"

    async def run(self, itx: Interaction, goal: str = "", effort: str = "normal") -> Outcome:
        core, cfg = self.core, self.core.cfg
        tools = self._tools(itx)
        by_name = {t.name: t for t in tools}
        specs = [t.spec() for t in tools]
        system = context.system(core, itx, self.role, goal)
        system[0] = {**system[0], "text": system[0]["text"] + "\n\n" + (MOUTH_PROMPT if self.role == "mouth" else BRAIN_PROMPT)} \
            if itx.owner else system[0]
        messages = context.messages(core, itx, goal)
        tier = Tier.MOUTH if self.role == "mouth" else TIER_FOR.get(effort, Tier.BRAIN)
        out = Outcome()
        streak = 0
        worked = failed = nudged = False  # real work was done / something did not work / the model was told to speak
        for step in range(cfg.max_steps):
            out.steps = step + 1
            if self.role == "brain" and not core.budget.task_room(itx.spent):
                out.stopped_by = "money"
                out.text = (f"остановился: задача уже стоит ${itx.spent:.2f}, мой лимит на задачу ${cfg.task_limit:.2f}. "
                            "скажи «продолжай», если надо дальше")
                return out
            try:
                model = core.budget.pick(tier, owner=itx.owner, think=itx.think)
            except OutOfMoney as e:
                out.stopped_by = "money"
                out.text = e.text if itx.owner else ""
                out.silent = not itx.owner
                return out
            prune_images(messages)
            try:
                reply = await core.llm.complete(
                    model, system, messages, specs, max_tokens=8000 if self.role == "brain" else 3000,
                    effort=self._effort(itx, tier),
                    kind=("" if itx.owner else "others") + (f"{self.role}" if itx.owner else ""))
            except LLMError as e:
                out.stopped_by = "error"
                out.text = self._llm_text(e) if itx.owner else ""
                out.silent = not itx.owner
                return out
            itx.spent += reply.cost
            if reply.refusal:
                out.silent = True
                return out
            if not reply.calls:
                out.text = reply.text
                return out
            messages.append({"role": "assistant", "content": reply.raw})
            blocks, all_terminal, handoff = [], True, ""
            for call in reply.calls:
                tool = by_name.get(call.name)
                result = await core.run_tool(tool, call, itx, show=self.role == "brain")
                out.tools_used += 1
                all_terminal = all_terminal and bool(tool and tool.terminal)
                handoff = handoff or result.handoff
                streak = 0 if result.ok else streak + 1
                worked = worked or bool(tool and not tool.terminal)
                failed = failed or not result.ok
                blocks.append(self._block(call.id, result, streak))
            if handoff:
                out.handoff = handoff
                return out
            if all_terminal:
                if not reply.text.strip() and (worked or failed) and itx.owner and not nudged and not handoff:
                    # Work was done (or failed) and the model only waved a reaction: the owner must hear how it went
                    nudged = True
                    messages.append({"role": "user", "content": blocks + [{"type": "text", "text": SPEAK}]})
                    continue
                out.text = reply.text  # the words beside a sticker or a reaction are the message
                return out
            messages.append({"role": "user", "content": blocks})
            if streak >= 2 and self.role == "brain" and tier < Tier.DEEP:
                tier = Tier(tier + 1)  # two failures in a row: the next model on the ladder takes over
                streak = 0
        out.stopped_by = "steps"
        out.text = "остановился: слишком много шагов. скажи «продолжай», если надо дальше"
        return out

    @staticmethod
    def _block(call_id: str, result: Result, streak: int) -> dict:
        text = result.text if len(result.text) <= MAX_RESULT else result.text[:MAX_RESULT] + "\n…(обрезано)"
        if streak >= 2:
            text += f"\n[{streak} ошибки подряд. Смени подход: другой инструмент или параметры, не повторяй то же.]"
        content = [{"type": "text", "text": text or "(пусто)"}]
        if result.image:
            import base64
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                        "data": base64.b64encode(result.image).decode()}})
        block = {"type": "tool_result", "tool_use_id": call_id, "content": content}
        if not result.ok:
            block["is_error"] = True
        return block

    @staticmethod
    def _llm_text(e: LLMError) -> str:
        if e.kind == "billing":
            return "на счёте Anthropic кончились деньги, пополни"
        if e.kind == "auth":
            return "у меня не работает ключ Anthropic"
        return "Claude сейчас не отвечает, повтори чуть позже"
