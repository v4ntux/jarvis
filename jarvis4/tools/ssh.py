"""Other servers over SSH as tools."""
from __future__ import annotations

from .. import guard
from ..types import Result, fail
from .base import I, S, Ctx, Risk, Tool


def _nodes(ctx: Ctx):
    return ctx.pf.extras.get("ssh")


async def ssh_servers(ctx: Ctx, a: dict) -> Result:
    nodes = _nodes(ctx)
    return Result(True, nodes.describe() if nodes else "Других серверов не подключено.")


async def ssh_run(ctx: Ctx, a: dict) -> Result:
    nodes = _nodes(ctx)
    if nodes is None or not nodes.servers:
        return fail("других серверов не подключено. Ван добавляет сервер сам: кладёт на него ключ и называет в настройках.")
    command = str(a.get("command", "")).strip()
    try:
        text, mode = await nodes.run(str(a.get("server", "")), command, min(int(a.get("timeout") or 60), 300))
    except KeyError as e:
        return fail(str(e.args[0]))
    if text.startswith("Ошибка"):
        return fail(text)
    note = "\n(на этом сервере мне разрешено только смотреть состояние: команду я не выполнял)" if mode == "status" else ""
    return Result(True, text + note, evidence=f"ssh {a.get('server')}")


async def _guard(ctx: Ctx, a: dict):
    return guard.shell(str(a.get("command", "")), "server")


def tools() -> list[Tool]:
    return [
        Tool("ssh_servers", "Другие серверы Вана, к которым у меня есть доступ, и с какими правами.", {}, [], ssh_servers),
        Tool("ssh_run", "Команда на другом сервере Вана по SSH (server — имя из ssh_servers). Если у сервера доступ «только "
             "смотреть», вернётся его статус, а команда не выполнится.", {"server": S, "command": S, "timeout": I},
             ["server", "command"], ssh_run, risk=Risk.OWN, guard=_guard, label=lambda a, r: f"команда на {a.get('server', '')}"),
    ]
