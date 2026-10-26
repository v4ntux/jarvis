"""Skills: ways of doing things that worked, kept so the next time costs almost nothing.

A skill is a name, when to use it, and the steps (tool calls included). The brain saves one after real work succeeded
(one found on the internet and tried counts), opens it with skill_get next time, and the outcome of that task is held
against the skill: it climbs draft → verified → trusted, or turns «broken» and gets fixed. The owner can also teach by
showing: «смотри, как я делаю» records his clicks on the laptop, and the recording becomes a skill.
"""
from __future__ import annotations

import re

from ..types import Result, fail
from .base import S, Ctx, Tool

NAME = re.compile(r"^[a-zа-яё0-9][a-zа-яё0-9 _\-]{1,40}$", re.I)
ROUTINE = "routine:"


def _check_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name.strip().lower())
    return name if NAME.match(name) else ""


async def skill_get(ctx: Ctx, a: dict) -> Result:
    name = _check_name(str(a.get("name", "")))
    skill = ctx.store.skill(name) if name else None
    if skill is None:
        return fail(f"навыка «{a.get('name', '')}» нет. Есть: " + (", ".join(s["name"] for s in ctx.store.skills()) or "ничего"))
    if skill["name"] not in ctx.itx.skills_used:
        ctx.itx.skills_used.append(skill["name"])
    body = skill["body"]
    if body.startswith(ROUTINE):
        body = (f"Это записанный приём Вана. Запусти laptop_routine(name='{body[len(ROUTINE):]}'), потом проверь результат "
                "фактом (laptop_windows) и скажи, что получилось.")
    return Result(True, f"Навык «{skill['name']}» [{skill['status']}, удач {skill['successes']}, сбоев {skill['failures']}]\n"
                  f"Когда: {skill['description']}\nШаги:\n{body}")


async def skill_save(ctx: Ctx, a: dict) -> Result:
    """Only after real work: a skill invented from nothing would be a guess dressed up as a fact."""
    name = _check_name(str(a.get("name", "")))
    description, steps = str(a.get("description", "")).strip(), str(a.get("steps", "")).strip()
    if not name:
        return fail("имя навыка — 2–40 символов, буквы, цифры, пробелы.")
    if len(description) < 10 or len(steps) < 30:
        return fail("опиши, когда применять (description), и по шагам, как делать (steps, с инструментами и проверкой).")
    done = [r for r in ctx.store.journal(40, task=ctx.itx.id) if r["ok"] and r["tool"] not in ctx.core.READ_TOOLS]
    if not done:
        return fail("навык записывают после того, как дело реально получилось. Сначала сделай его.")
    created = ctx.store.skill_save(name, description, steps)
    return Result(True, f"Навык «{name}» {'записан' if created else 'обновлён'} (пока черновик: подтвердится, когда сработает "
                  "ещё раз).", evidence=f"skill {name}")


async def skill_forget(ctx: Ctx, a: dict) -> Result:
    name = _check_name(str(a.get("name", "")))
    return Result(True, "Забыл навык.") if name and ctx.store.skill_forget(name) else fail("такого навыка нет.")


async def show_me_start(ctx: Ctx, a: dict) -> Result:
    """«Смотри, как я делаю»: the laptop records his clicks and keys (passwords are never recorded)."""
    lap = ctx.pf.laptop
    if lap is None or not lap.online or not hasattr(lap, "record"):
        return fail("ноутбук не подключён, записывать нечего.")
    name = _check_name(str(a.get("name", "")))
    when = str(a.get("when", "")).strip()
    if not name or len(when) < 8:
        return fail("нужны короткое имя навыка и when — когда его применять (одной фразой).")
    said = await lap.record(name)
    if said.startswith("Ошибка"):
        return fail(said)
    ctx.store.skill_save(name, when, ROUTINE + name)
    return Result(True, f"Записываю. Показывай, а как закончишь, скажи «готово». {said}", evidence=f"запись {name}")


async def show_me_done(ctx: Ctx, a: dict) -> Result:
    lap = ctx.pf.laptop
    if lap is None or not lap.online or not hasattr(lap, "stop_record"):
        return fail("ноутбук не подключён.")
    said = await lap.stop_record()
    if said.startswith("Ошибка"):
        return fail(said)
    return Result(True, said + " Это черновик навыка: подтвердится, когда я сам повторю и увижу результат.",
                  evidence="запись остановлена")


def tools() -> list[Tool]:
    return [
        Tool("skill_get", "Открыть навык по имени: когда применять и по шагам, как делать. Если в списке навыков есть "
             "подходящий — сначала открой его и следуй ему, не придумывай заново.", {"name": S}, ["name"], skill_get),
        Tool("skill_save", "Сохранить навык после того, как дело реально получилось (в том числе по найденному в интернете "
             "и проверенному): name — коротко, description — когда применять, steps — по шагам, какие инструменты и "
             "как проверить результат. Так в следующий раз то же самое делается быстро и бесплатно.",
             {"name": S, "description": S, "steps": S}, ["name", "description", "steps"], skill_save),
        Tool("skill_forget", "Забыть навык по имени.", {"name": S}, ["name"], skill_forget),
        Tool("show_me_start", "Ван говорит «смотри, как я делаю» или «запомни, как…»: начать запись его кликов и клавиш на "
             "ноутбуке. name — короткое имя, when — когда применять. Пароли не записываются.",
             {"name": S, "when": S}, ["name", "when"], show_me_start, light=True),
        Tool("show_me_done", "Ван сказал «готово»: остановить запись показа.", {}, [], show_me_done, light=True),
    ]
