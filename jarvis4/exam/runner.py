"""Runs scenarios against an agent: builds the world, delivers the events, checks, reports."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import time
import traceback
from dataclasses import asdict, dataclass, field

from . import checks as code_checks
from .agents import Agent, make
from .scenario import Scenario
from .world import World

POLL = 0.05


@dataclass
class Result:
    id: str
    title: str
    status: str  # pass | fail | skip | error
    checks: list[code_checks.Check] = field(default_factory=list)
    cost: float = 0.0  # what the agent spent
    judge_cost: float = 0.0
    seconds: float = 0.0
    note: str = ""
    timeline: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[code_checks.Check]:
        return [c for c in self.checks if not c.ok]


async def _settle(agent: Agent, world: World, during: dict | None, tools0: int, replies0: int,
                  timeout: float) -> bool:
    """Waits until the next event is due: the agent is idle, or (for a mid-task event) it has made enough tool
    calls or said enough since the previous event. False when the time ran out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if during:
            if len(world.tools) - tools0 >= during.get("tool_calls", 10 ** 9):
                return True
            if len(world.replies) - replies0 >= during.get("replies", 10 ** 9):
                return True
        if not agent.busy():
            return True
        await asyncio.sleep(POLL)
    return False


async def run_one(scenario: Scenario, agent: Agent, judge_client=None, timeout: float | None = None) -> Result:
    if agent.name in scenario.skip:
        return Result(scenario.id, scenario.title, "skip", note=str(scenario.skip[agent.name]))
    world = scenario.build_world()
    limit = timeout or scenario.timeout
    started = time.monotonic()
    result = Result(scenario.id, scenario.title, "error")
    try:
        await agent.start(world, scenario)
        tools0 = replies0 = 0
        for i, event in enumerate(scenario.events):
            if not await _settle(agent, world, event.during, tools0, replies0, limit):
                result.note = f"агент не закончил до события {i} за {limit:.0f} с"
                break
            world.deliver(i, event.describe(world))
            tools0, replies0 = len(world.tools), len(world.replies)
            msg = None
            if event.kind == "message":
                msg = world.incoming(event.chat, event.sender, event.text, event.media, event.reply_to, event.key)
            elif event.kind == "laptop_online":
                world.laptop_online = True
            await agent.deliver(i, event, msg)
        else:
            if not await _settle(agent, world, None, 0, 0, limit):
                result.note = f"агент не закончил за {limit:.0f} с"
    except Exception:
        result.note = traceback.format_exc()[-1500:]
    finally:
        try:
            await agent.stop()
        except Exception:
            result.note += "\nstop: " + traceback.format_exc()[-500:]
    result.seconds = time.monotonic() - started
    result.cost = agent.cost()
    result.timeline = list(world.timeline)
    if result.note and "Traceback" in result.note:
        return result
    result.checks = code_checks.run(scenario, world, result.cost)
    if result.note:  # ran out of time: that alone fails the scenario
        result.checks.insert(0, code_checks.Check("агент закончил вовремя", False, result.note))
    if judge_client is not None:
        from .judge import judge
        try:
            verdicts, result.judge_cost = await judge(scenario, world, judge_client)
            result.checks += verdicts
        except Exception as e:
            result.checks.append(code_checks.Check("судья ответил", False, f"{type(e).__name__}: {e}"[:300]))
    result.status = "pass" if all(c.ok for c in result.checks) else "fail"
    return result


async def run_all(scenarios: list[Scenario], agent_name: str, judge: bool = True, max_cost: float = 0.5,
                  timeout: float | None = None, echo=print, repeat: int = 1) -> list[Result]:
    client = None
    if judge:
        import anthropic
        client = anthropic.AsyncAnthropic()
    results: list[Result] = []
    spent = 0.0
    for s in [x for x in scenarios for _ in range(repeat)]:
        if spent >= max_cost:
            results.append(Result(s.id, s.title, "skip", note=f"лимит денег ${max_cost:.2f} исчерпан"))
            echo(f"–  {s.id}: пропущен, лимит ${max_cost:.2f}")
            continue
        r = await run_one(s, make(agent_name), client, timeout)
        spent += r.cost + r.judge_cost
        results.append(r)
        echo(line(r))
        for c in r.failed:
            echo(f"     ✗ {c.name}" + (f" — {c.detail}" if c.detail else ""))
        if r.status == "error":
            echo("     " + r.note.strip().replace("\n", "\n     "))
    return results


MARK = {"pass": "✓", "fail": "✗", "skip": "–", "error": "!"}


def line(r: Result) -> str:
    money = f" ${r.cost + r.judge_cost:.3f}" if r.cost or r.judge_cost else ""
    return f"{MARK[r.status]}  {r.id}: {r.title} ({r.seconds:.0f} с{money})"


def stability(results: list[Result]) -> str:
    """With repeats: how often each scenario passed (a model answers differently every time)."""
    runs: dict[str, list[str]] = {}
    for r in results:
        runs.setdefault(r.id, []).append(r.status)
    if all(len(v) == 1 for v in runs.values()):
        return ""
    return "\n".join(f"  {id}: {sum(x == 'pass' for x in v)}/{len(v)}" for id, v in runs.items())


def summary(results: list[Result]) -> str:
    counts = {k: sum(r.status == k for r in results) for k in MARK}
    cost = sum(r.cost for r in results)
    judge = sum(r.judge_cost for r in results)
    return (f"Сдано {counts['pass']} из {len(results) - counts['skip']}"
            f" (не сдано {counts['fail']}, ошибок {counts['error']}, пропущено {counts['skip']}). "
            f"Агент ${cost:.3f}, судья ${judge:.3f}.")


def write_report(results: list[Result], agent_name: str, folder: str) -> str:
    os.makedirs(folder, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    base = os.path.join(folder, f"{agent_name.replace(':', '-')}-{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=1)
    lines = [f"# Экзамен: {agent_name}, {stamp}", "", summary(results), ""]
    for r in results:
        lines += [f"## {MARK[r.status]} {r.id}: {r.title}", ""]
        if r.note:
            lines += ["```", r.note.strip(), "```", ""]
        for c in r.checks:
            lines.append(f"- {'✓' if c.ok else '✗'} {c.name}" + (f" — {c.detail}" if c.detail and not c.ok else ""))
        if r.status != "pass" and r.timeline:
            lines += ["", "Ход:", "```", *r.timeline, "```"]
        lines.append("")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return base + ".md"
