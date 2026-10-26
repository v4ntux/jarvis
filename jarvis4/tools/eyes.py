"""Looking at and pressing a screen: the brain's tool for anything that has no command (a program's own buttons,
a site, a phone). Cheap checks (windows, sound) stay with laptop_windows and laptop_media; this is for the rest."""
from __future__ import annotations

from .. import guard
from ..types import Result, fail
from .base import S, Ctx, Risk, Tool


async def _guard(ctx: Ctx, a: dict):
    return guard.screen_click(str(a.get("goal", "")))


async def screen_task(ctx: Ctx, a: dict) -> Result:
    goal = str(a.get("goal", "")).strip()
    if not goal:
        return fail("не сказано, что сделать на экране (goal).")
    return await ctx.core.eyes.run(ctx.itx, goal, str(a.get("device") or "laptop"))


async def laptop_see(ctx: Ctx, a: dict) -> Result:
    lap = ctx.pf.laptop
    if lap is None or not lap.online:
        return fail("ноутбук не подключён.")
    try:
        data = await lap.screenshot()
    except Exception as e:
        return fail(str(e))
    return Result(True, "Скриншот экрана ноутбука приложен.", image=data)


def tools() -> list[Tool]:
    return [
        Tool("screen_task", "Сделать что-то на ЭКРАНЕ глазами и мышью: кнопки программ, сайты, меню, всё, для чего нет команды. "
             "goal — что именно, одной задачей. device: laptop (по умолчанию) или android. Платить, удалять важное и писать "
             "людям оно само не станет без «да» Вана. После него проверь результат фактом (laptop_windows, laptop_media).",
             {"goal": S, "device": {"type": "string", "enum": ["laptop", "android"]}}, ["goal"], screen_task,
             risk=Risk.OWN, guard=_guard, label=lambda a, r: "работаю с экраном"),
        Tool("laptop_see", "Посмотреть на экран ноутбука самому (картинкой): что открыто, что написано. Для вопросов «что у "
             "меня на экране».", {}, [], laptop_see, label=lambda a, r: "смотрю на экран"),
    ]
