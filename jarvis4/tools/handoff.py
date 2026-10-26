"""The mouth hands real work to the brain. It says a short «щас гляну» first, then the brain does the job."""
from __future__ import annotations

from ..types import Result, fail
from .base import S, Ctx, Tool


async def do_task(ctx: Ctx, a: dict) -> Result:
    goal = str(a.get("goal", "")).strip()
    if not goal:
        return fail("не сказано, что делать (goal).")
    ack = str(a.get("ack") or "").strip()
    if ack:
        await ctx.itx.out.say(ack)
    effort = a.get("effort") if a.get("effort") in ("simple", "normal", "deep") else "normal"
    return Result(True, "Передал мозгу.", handoff=f"{effort}:{goal}")


def tools() -> list[Tool]:
    return [Tool(
        "do_task", "Передать задачу мозгу, когда нужен ноутбук (программы, окна, экран, файлы, клики, команды), "
        "сервер (скрипты, скачивание, обработка файлов), или она из нескольких шагов, или нужно думать. goal — "
        "задача целиком, ТОЧНО СЛОВАМИ ВАНА плюс детали из разговора. ack — что сказать Вану сейчас, 1–3 слова "
        "(«щас», «гляну»), можно пусто. effort: simple — одно-два простых действия, normal — обычная задача, "
        "deep — сложная, только если Ван сказал думать. Вопросов не задавай и вариантов не предлагай: передавай сразу.",
        {"goal": S, "ack": S, "effort": {"type": "string", "enum": ["simple", "normal", "deep"]}}, ["goal"], do_task,
        terminal=True, light=True)]
