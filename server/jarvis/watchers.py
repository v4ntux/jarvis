"""Watchers: «if X happens, tell me / do Y».

Conditions are checked here without AI, so a watcher costs nothing until it fires.
Only a watcher with a task wakes the brain, and only when it fires.
"""
import asyncio
import datetime as dt
import hashlib
import logging
import re
import time
from typing import Awaitable, Callable

from . import shell, web
from .config import TZ
from .hub import Hub
from .memory import Memory

log = logging.getLogger("jarvis.watchers")

SOURCES = ("time", "laptop", "server", "url", "laptop_offline", "laptop_online")
CONDITIONS = ("changed", "contains", "missing", "below", "above", "equals")
DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
DAY_SETS = {"daily": set(range(7)), "weekdays": set(range(5)), "weekends": {5, 6}}
TICK = 15  # seconds between passes over the watcher list
CHECK_TIMEOUT = 30
NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


def _days(text: str) -> set[int]:
    text = (text or "daily").strip().lower()
    if text in DAY_SETS:
        return DAY_SETS[text]
    days = {DAYS[d.strip()[:3]] for d in text.split(",") if d.strip()[:3] in DAYS}
    if not days:
        raise ValueError(f"не понял дни «{text}»: нужно daily, weekdays, weekends или mon,tue,...")
    return days


def next_time(at: str, days: str, after: float) -> float:
    """Next moment at HH:MM Moscow time on one of the days, strictly after `after`."""
    hour, minute = (int(p) for p in at.split(":"))
    allowed = _days(days)
    base = dt.datetime.fromtimestamp(after, TZ)
    for add in range(8):
        moment = (base + dt.timedelta(days=add)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if moment.weekday() in allowed and moment.timestamp() > after:
            return moment.timestamp()
    raise ValueError("не нашёл подходящий день")


def normalize(args: dict) -> dict:
    """Checks what the brain asked for and keeps only the fields that matter."""
    source = args.get("source")
    if source not in SOURCES:
        raise ValueError(f"source должен быть одним из: {', '.join(SOURCES)}")
    what = (args.get("what") or "").strip()
    if not what:
        raise ValueError("нужно what: что отслеживаем, коротко")
    spec = {"what": what, "source": source, "notify": (args.get("notify") or "").strip(),
            "task": (args.get("task") or "").strip(), "urgent": bool(args.get("urgent")), "sms": bool(args.get("sms"))}
    if source == "time":
        at = (args.get("at") or "").strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", at):
            raise ValueError("для time нужно at в виде ЧЧ:ММ")
        spec.update(at=at, days=(args.get("days") or "daily").strip().lower())
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
        when = f"в {spec['at']}, дни: {spec['days']}"
    elif s == "laptop_offline":
        when = f"ноутбук пропал дольше {spec['minutes']} мин"
    elif s == "laptop_online":
        when = "ноутбук снова на связи"
    else:
        target = spec.get("url") or spec.get("command")
        cond = spec["condition"] + (f" {spec['value']}" if spec["value"] else "")
        when = f"{s}: {target[:80]} → {cond}, каждые {spec['every_min']} мин"
    flags = [f for f, on in (("срочно", spec.get("urgent")), ("SMS", spec.get("sms")),
                             ("с задачей", spec.get("task"))) if on]
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
    # Fire on the switch to true, not on every check while it stays true
    fired = hit and not state.get("was")
    state["was"] = hit
    return fired


class Watchers:
    def __init__(self, memory: Memory, hub: Hub, fire: Callable[[dict, str], Awaitable[None]]):
        self.memory = memory
        self.hub = hub
        self.fire = fire  # fire(spec, detail): tell the owner, run the task if there is one
        self.running: set[asyncio.Task] = set()

    async def loop(self) -> None:
        while True:
            for wid, spec, state, due in self.memory.watchers():
                if due <= time.time():
                    try:
                        await self._check(wid, spec, state)
                    except Exception:
                        log.exception("watcher %s failed", wid)
                        self.memory.update_watcher(wid, state, time.time() + 300)
            await asyncio.sleep(TICK)

    async def _check(self, wid: int, spec: dict, state: dict) -> None:
        now = time.time()
        source = spec["source"]
        fired, detail = False, ""
        if source == "time":
            fired, due = True, next_time(spec["at"], spec["days"], now)
        elif source == "laptop_offline":
            hit = not self.hub.online and now - self.hub.since >= spec["minutes"] * 60
            fired, state["was"], due = hit and not state.get("was"), hit, now + 30
        elif source == "laptop_online":
            # Only a return counts: being online when the watcher is made is not news
            fired, state["was"], due = self.hub.online and state.get("was") is False, self.hub.online, now + 30
        else:
            due = now + spec["every_min"] * 60
            output = await self._observe(spec)
            try:
                if output is None or output.startswith("Ошибка"):
                    # Laptop asleep or the command broke: try again later, never fire on an error
                    raise ValueError(output or "ноутбук не подключён")
                fired = evaluate(spec, output, state)
            except ValueError as e:
                state["error"] = str(e)[:200]
                self.memory.update_watcher(wid, state, now + 60 if output is None else due)
                return
            state.pop("error", None)
            detail = output.strip()
        if fired and spec.get("once"):
            self.memory.remove_watcher(wid)
        else:
            self.memory.update_watcher(wid, state, due)
        if fired:
            log.info("watcher %s fired: %s", wid, spec["what"])
            task = asyncio.create_task(self.fire(spec, detail))
            self.running.add(task)
            task.add_done_callback(self.running.discard)

    async def _observe(self, spec: dict) -> str | None:
        """Raw text the condition looks at; None when the laptop is not there."""
        if spec["source"] == "url":
            return await web.fetch(spec["url"])
        if spec["source"] == "server":
            return await shell.run(spec["command"], CHECK_TIMEOUT)
        if not self.hub.online:
            return None
        out = await self.hub.call("run_shell", {"command": spec["command"], "timeout": CHECK_TIMEOUT},
                                  timeout=CHECK_TIMEOUT + 15)
        return out["text"]
