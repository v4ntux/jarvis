"""Memory as tools: facts, lessons from the owner's corrections, the journal of what Jarvis did, reminders, watchers."""
from __future__ import annotations

import datetime as dt
import time

from .. import clock, persona, watch
from ..types import Result, fail
from .base import B, I, S, Ctx, Tool


async def remember(ctx: Ctx, a: dict) -> Result:
    text = str(a.get("text", "")).strip()
    if not text:
        return fail("нечего запоминать.")
    return Result(True, f"Запомнил (#{ctx.store.remember(text)}).")


async def forget(ctx: Ctx, a: dict) -> Result:
    return Result(True, "Удалил.") if ctx.store.forget(int(a.get("id", 0))) else fail("нет такого факта.")


async def learn_lesson(ctx: Ctx, a: dict) -> Result:
    """The owner corrected Jarvis: the rule becomes part of the prompt whenever a similar thing comes up."""
    text = str(a.get("text", "")).strip()
    if not persona.is_correction(ctx.itx.text) and not ctx.itx.note:
        return fail("урок записывают, только когда Ван поправил или недоволен. Сейчас его просьба обычная: просто сделай.")
    if len(text) < 8:
        return fail("урок слишком короткий: напиши, что делать и чего не делать.")
    lesson_id = ctx.store.add_lesson(text, str(a.get("when") or ""), source=ctx.itx.text[:200])
    return Result(True, f"Записал урок #{lesson_id}.", evidence=text)


async def journal(ctx: Ctx, a: dict) -> Result:
    rows = ctx.store.journal(min(int(a.get("limit") or 12), 40), query=str(a.get("query") or ""))
    if not rows:
        return Result(True, "В журнале ничего такого нет.")
    lines = []
    for r in rows:
        args = ", ".join(f"{k}={str(v)[:400]}" for k, v in r["args"].items())
        lines.append(f"{clock.human(r['ts'])} {r['tool']}({args}) → {'ок' if r['ok'] else 'ошибка'}: {r['result'][:200]}")
    return Result(True, "\n".join(lines))


async def set_reminder(ctx: Ctx, a: dict) -> Result:
    now = time.time()
    if a.get("in_minutes"):
        due = now + int(a["in_minutes"]) * 60
    elif a.get("at"):
        at = dt.datetime.fromisoformat(str(a["at"]))
        due = (at if at.tzinfo else at.replace(tzinfo=clock.TZ)).timestamp()
    else:
        return fail("нужно in_minutes или at (ISO по времени Вана).")
    if due < now:
        return fail("это время уже прошло.")
    call = bool(a.get("call"))
    rid = ctx.store.add_reminder(due, str(a.get("text") or "Ты просил напомнить."), call)
    return Result(True, f"{'Звонок' if call else 'Напоминание'} #{rid} на {clock.human(due)} по твоему времени.",
                  evidence=f"reminder {rid}")


async def list_reminders(ctx: Ctx, a: dict) -> Result:
    rows = ctx.store.pending_reminders()
    return Result(True, "\n".join(f"#{i} {clock.human(d)}{' 📞' if c else ''} {t}" for i, d, t, c in rows)
                  or "Напоминаний нет.")


async def add_watcher(ctx: Ctx, a: dict) -> Result:
    try:
        spec = watch.normalize({**a, "by": "owner" if ctx.itx.owner else "jarvis"})
    except (ValueError, KeyError) as e:
        return fail(str(e))
    # An action may only run by itself if the owner asked for it in so many words: Jarvis's own idea just reports
    if spec["task"] and not spec["auto"]:
        spec["task"] = spec["task"]
    wid = ctx.store.add_watcher(spec, watch.first_check(spec, time.time()))
    return Result(True, f"Наблюдатель #{wid}: {watch.describe(spec)}.", evidence=f"watcher {wid}")


async def list_watchers(ctx: Ctx, a: dict) -> Result:
    rows = ctx.store.watchers()
    return Result(True, "\n".join(f"#{i} {watch.describe(s)}" + (f" [ошибка: {st['error']}]" if st.get("error") else "")
                                  for i, s, st, _ in rows) or "Наблюдателей нет.")


async def remove_watcher(ctx: Ctx, a: dict) -> Result:
    return Result(True, "Удалил.") if ctx.store.remove_watcher(int(a.get("id", 0))) else fail("нет такого наблюдателя.")


def tools() -> list[Tool]:
    return [
        Tool("remember", "Запомнить факт о Ване или его делах (имена, пути, предпочтения).", {"text": S}, ["text"],
             remember, light=True),
        Tool("forget", "Забыть факт по номеру.", {"id": I}, ["id"], forget, light=True),
        Tool("learn_lesson", "Ван тебя поправил или недоволен: запиши урок «в такой ситуации делай так, а так не "
             "делай», он будет перед глазами всякий раз, когда ситуация повторится. when — коротко, когда это "
             "применять.", {"text": S, "when": S}, ["text"], learn_lesson, light=True,
             label=lambda a, r: "записал урок"),
        Tool("journal", "Журнал моих действий: что я делал, с какими точными аргументами и что вышло. query — слово "
             "для поиска. Для «что ты писал», «что делал вчера».", {"query": S, "limit": I}, [], journal, light=True),
        Tool("set_reminder", "Напоминание или звонок в нужное время: in_minutes или at (ISO по времени Вана), call=true "
             "— позвонить.", {"text": S, "in_minutes": I, "at": S, "call": B}, [], set_reminder, light=True),
        Tool("list_reminders", "Список напоминаний.", {}, [], list_reminders, light=True),
        Tool("add_watcher", "Наблюдатель «скажи, когда…»: source = time | laptop_online | laptop_offline | url | server "
             "| laptop; what — коротко; notify — что сказать. then — действие, которое я предложу сделать, когда "
             "сработает (само оно запустится только с auto=true, а auto ставь, лишь если Ван прямо приказал делать "
             "это сам). Для time: at ЧЧ:ММ, days; для url/server/laptop: url или command, condition, value, "
             "every_min.",
             {"source": S, "what": S, "notify": S, "then": S, "auto": B, "at": S, "days": S, "minutes": I, "url": S,
              "command": S, "condition": S, "value": S, "every_min": I, "once": B, "urgent": B}, ["source", "what"],
             add_watcher, light=True, label=lambda a, r: "завожу наблюдателя"),
        Tool("list_watchers", "Список наблюдателей.", {}, [], list_watchers, light=True),
        Tool("remove_watcher", "Удалить наблюдателя по номеру.", {"id": I}, ["id"], remove_watcher, light=True),
    ]
