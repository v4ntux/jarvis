"""Watchers: «tell me when X happens». Checked by code, never by a model, so a watcher costs nothing until it fires.

What a watcher may do when it fires: tell the owner. If it carries a task (`then`) it only proposes it, unless the
owner ordered the task himself when the watcher was made (`auto`): then Jarvis announces it and waits 30 seconds
for a «стоп». A watcher Jarvis made on his own initiative never acts on a laptop or a server by itself.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import logging
import re
import time
from typing import Awaitable, Callable

from .clock import TZ
from .ports import Offline
from .store import Store

log = logging.getLogger("jarvis4.watch")

SOURCES = ("time", "laptop", "server", "url", "laptop_offline", "laptop_online")
CONDITIONS = ("changed", "contains", "missing", "below", "above", "equals")
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_SETS = {"daily": set(range(7)), "weekdays": set(range(5)), "weekends": {5, 6}, "ежедневно": set(range(7)),
            "каждый день": set(range(7)), "всегда": set(range(7)), "будни": set(range(5)), "по будням": set(range(5)),
            "выходные": {5, 6}, "по выходным": {5, 6}}
RU_DAYS = {"пн": "mon", "по": "mon", "вт": "tue", "ср": "wed", "чт": "thu", "че": "thu", "пт": "fri", "пя": "fri",
           "сб": "sat", "су": "sat", "вс": "sun", "во": "sun"}
TICK = 15
CHECK_TIMEOUT = 30
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _days(text: str) -> set[int]:
    text = (text or "daily").strip().lower()
    if text in DAY_SETS:
        return DAY_SETS[text]
    words = [d.strip() for d in re.split(r"[,\s]+", text) if d.strip()]
    days = {DAYS[d[:3]] for d in words if d[:3] in DAYS} | {DAYS[RU_DAYS[d[:2]]] for d in words if d[:2] in RU_DAYS}
    if not days:
        raise ValueError(f"не понял дни «{text}»: нужно daily, weekdays, weekends или mon,tue,...")
    return days


def next_time(at: str, days: str, after: float) -> float:
    """Next moment at HH:MM on the owner's clock on one of the days, strictly after `after`."""
    hour, minute = (int(p) for p in at.split(":"))
    allowed = _days(days)
    base = dt.datetime.fromtimestamp(after, TZ)
    for add in range(8):
        moment = (base + dt.timedelta(days=add)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.weekday() in allowed and moment.timestamp() > after:
            return moment.timestamp()
    raise ValueError("не нашёл подходящий день")


def normalize(args: dict) -> dict:
    """Checks what was asked and keeps only the fields that matter."""
    source = args.get("source")
    if source not in SOURCES:
        raise ValueError(f"source должен быть одним из: {', '.join(SOURCES)}")
    what = (args.get("what") or "").strip()
    if not what:
        raise ValueError("нужно what: что отслеживаем, коротко")
    spec = {"what": what, "source": source, "notify": (args.get("notify") or "").strip(),
            "task": (args.get("then") or args.get("task") or "").strip(), "urgent": bool(args.get("urgent")),
            "auto": bool(args.get("auto")), "by": args.get("by") or "jarvis"}
    if source == "time":
        at = (args.get("at") or "").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
            raise ValueError("для time нужно at в виде ЧЧ:ММ")
        days = (args.get("days") or "").strip().lower()
        spec.update(at=at, days=days or "daily", once=bool(args["once"]) if "once" in args else not days)
        _days(spec["days"])
        return spec
    if source == "laptop_offline":
        spec.update(minutes=max(1, int(args.get("minutes") or 10)), once=bool(args.get("once", False)))
        return spec
    if source == "laptop_online":
        spec.update(once=bool(args.get("once", True)))
        return spec
    key = "url" if source == "url" else "command"
    if not (args.get(key) or "").strip():
        raise ValueError(f"для {source} нужно {key}")
    condition = args.get("condition") or "changed"
    if condition not in CONDITIONS:
        raise ValueError(f"condition должен быть одним из: {', '.join(CONDITIONS)}")
    value = str(args.get("value") if args.get("value") is not None else "").strip()
    if condition != "changed" and not value:
        raise ValueError(f"для condition={condition} нужно value")
    if condition in ("below", "above"):
        float(value.replace(",", "."))
    spec.update({key: args[key].strip()}, condition=condition, value=value,
                every_min=max(1, int(args.get("every_min") or 5)), once=bool(args.get("once", True)))
    return spec


def first_check(spec: dict, now: float) -> float:
    return next_time(spec["at"], spec["days"], now) if spec["source"] == "time" else now


def describe(spec: dict) -> str:
    s = spec["source"]
    if s == "time":
        when = f"один раз в {spec['at']}" if spec.get("once") else f"в {spec['at']}, дни: {spec['days']}"
    elif s == "laptop_offline":
        when = f"ноутбук пропал дольше {spec['minutes']} мин"
    elif s == "laptop_online":
        when = "ноутбук снова на связи"
    else:
        target = spec.get("url") or spec.get("command")
        cond = spec["condition"] + (f" {spec['value']}" if spec["value"] else "")
        when = f"{s}: {target[:80]} → {cond}, каждые {spec['every_min']} мин"
    flags = [f for f, on in (("срочно", spec.get("urgent")), ("с задачей", spec.get("task"))) if on]
    return f"{spec['what']} ({', '.join([when, *flags])})"


def evaluate(spec: dict, output: str, state: dict) -> bool:
    """True when the watcher should fire; updates state in place."""
    cond, value = spec["condition"], spec["value"]
    if cond == "changed":
        digest = hashlib.sha256(output.strip().encode()).hexdigest()
        fired = state.get("hash") is not None and digest != state["hash"]
        state["hash"] = digest
        return fired
    text = output.strip()
    if cond == "contains":
        hit = value.lower() in text.lower()
    elif cond == "missing":
        hit = value.lower() not in text.lower()
    elif cond == "equals":
        hit = text == value
    else:
        found = NUMBER.search(text)
        if not found:
            raise ValueError(f"в выводе нет числа: {text[:100]!r}")
        number, limit = float(found.group().replace(",", ".")), float(value.replace(",", "."))
        hit = number < limit if cond == "below" else number > limit
    fired = hit and not state.get("was")  # on the switch to true, not on every check while it stays true
    state["was"] = hit
    return fired


Fire = Callable[[list[tuple[dict, str]]], Awaitable[None]]


class Watchers:
    def __init__(self, store: Store, platform, fire: Fire, frozen: Callable[[], bool] = lambda: False):
        self.store, self.pf, self.fire, self.frozen = store, platform, fire, frozen
        self.running: set[asyncio.Task] = set()
        self.laptop_since = time.time()
        self._was_online: bool | None = None

    def laptop_online(self) -> bool:
        lap = self.pf.laptop
        return bool(lap and lap.online)

    async def loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("watcher pass failed")
            await asyncio.sleep(TICK)

    async def tick(self) -> None:
        now = time.time()
        online = self.laptop_online()
        if self._was_online is not None and online != self._was_online:
            self.laptop_since = now
        self._was_online = online
        batch: list[tuple[dict, str]] = []
        for wid, spec, state, due in self.store.watchers():
            if due > now:
                continue
            try:
                got = await self._check(wid, spec, state, now)
            except Exception:
                log.exception("watcher %s failed", wid)
                self.store.update_watcher(wid, state, now + 300)
                continue
            if got is not None:
                batch.append((spec, got))
        if batch and not self.frozen():
            task = asyncio.create_task(self.fire(batch))
            self.running.add(task)
            task.add_done_callback(self.running.discard)

    async def _check(self, wid: int, spec: dict, state: dict, now: float) -> str | None:
        """The detail text when the watcher fired, else None."""
        source, fired, detail = spec["source"], False, ""
        if source == "time":
            fired, due = True, next_time(spec["at"], spec["days"], now)
        elif source == "laptop_offline":
            hit = not self.laptop_online() and now - self.laptop_since >= spec["minutes"] * 60
            fired, state["was"], due = hit and not state.get("was"), hit, now + 30
        elif source == "laptop_online":
            online = self.laptop_online()
            fired, state["was"], due = online and state.get("was") is False, online, now + 30
        else:
            due = now + spec["every_min"] * 60
            output = await self._observe(spec)
            try:
                if output is None or output.startswith("Ошибка"):
                    raise ValueError(output or "ноутбук не подключён")
                fired = evaluate(spec, output, state)
            except ValueError as e:
                state["error"] = str(e)[:200]
                self.store.update_watcher(wid, state, now + 60 if output is None else due)
                return None
            state.pop("error", None)
            detail = output.strip()
        if fired and spec.get("once"):
            self.store.remove_watcher(wid)
        else:
            self.store.update_watcher(wid, state, due)
        if fired:
            log.info("watcher %s fired: %s", wid, spec["what"])
            return detail
        return None

    async def _observe(self, spec: dict) -> str | None:
        if spec["source"] == "url":
            return await self.pf.web.fetch(spec["url"])
        if spec["source"] == "server":
            return await self.pf.server.shell(spec["command"], CHECK_TIMEOUT)
        if not self.laptop_online():
            return None
        try:
            return await self.pf.laptop.shell(spec["command"], CHECK_TIMEOUT)
        except Offline:
            return None
