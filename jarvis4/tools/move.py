"""Moving projects from another server onto this one. The order of the steps and the safety live in ops/relocate.py."""
from __future__ import annotations

import itertools
import os
import time

from .. import guard
from ..ops import relocate, shell
from ..ops.relocate import Refused, Relocator
from ..types import Result, fail
from .base import B, S, Ctx, Risk, Tool

_job_ids = itertools.count(1)
_moves: dict[str, dict] = {}  # "server:project" -> what is going on
_plans: dict[str, tuple[float, object, object]] = {}  # a fresh plan is reused by the call that follows the owner's «да»
PLAN_TTL = 300

FULL_ACCESS = ("У меня на {server} только смотреть, а перенос — это работа: остановить, снять архив, поднять. Дай мне "
               "полный ключ, как это делается (я сам доступ себе не завожу): на главном сервере "
               "`ssh-keygen -t ed25519 -N '' -f /opt/jarvis/data/keys/{server}_full`, публичную часть (.pub) добавить на "
               "{server} в authorized_keys, а в настройки JARVIS_NODES дописать "
               '`"{server}-full": {{"target": "<пользователь>@<адрес>", "key": "/opt/jarvis/data/keys/{server}_full", '
               '"mode": "full"}}` и перезапустить меня. Потом вызывать с server="{server}-full".')


class SshOld:
    """The old server as relocate.py sees it."""

    def __init__(self, nodes, server: str):
        self.nodes, self.server = nodes, server

    async def run(self, command: str, timeout: float = 60) -> tuple[int, str]:
        return await self.nodes.exec(self.server, command, timeout)

    async def fetch(self, command: str, dest: str, timeout: float = 1800) -> tuple[int, str]:
        return await self.nodes.fetch(self.server, command, dest, timeout)


class LocalHere:
    """This server."""

    async def run(self, command: str, timeout: float = 60) -> tuple[int, str]:
        code, text = await shell.exec(command, int(timeout))
        return (124, f"не уложилась в {int(timeout)} с") if code == shell.TIMED_OUT else (code, text)


def _backups(ctx: Ctx) -> str:
    return os.path.join(os.path.dirname(ctx.cfg.data_dir.rstrip("/")), "backup", "migrate")


def _nodes(ctx: Ctx, server: str):
    """(nodes, None) when the server can be worked on, else (None, the failure to return)."""
    nodes = ctx.pf.extras.get("ssh")
    if nodes is None or not nodes.servers:
        return None, fail("других серверов не подключено. Ван добавляет сервер сам: кладёт на него ключ и называет в настройках.")
    found = nodes.servers.get((server or "").strip().lower())
    if found is None:
        return None, fail(f"сервера «{server}» нет. Есть: {', '.join(nodes.names())}")
    if found.mode != "full":
        return None, fail(FULL_ACCESS.format(server=found.name))
    return nodes, None


def _relocator(ctx: Ctx, nodes, server: str, project: str, **kw) -> Relocator:
    return Relocator(SshOld(nodes, server), LocalHere(), project, server=server, backups=_backups(ctx), **kw)


async def _plan(ctx: Ctx, nodes, server: str, project: str):
    key = f"{server.lower()}:{project}"
    seen = _plans.get(key)
    if seen and time.time() - seen[0] < PLAN_TTL:
        return seen[1], seen[2]
    facts, check = await _relocator(ctx, nodes, server, project).plan()
    _plans[key] = (time.time(), facts, check)
    return facts, check


async def _tell(ctx: Ctx, text: str) -> None:
    """The owner hears it in his chat, and the message is kept like any other of Jarvis's."""
    try:
        ref = await ctx.core.out_for("owner").say(text)
        if ref:
            ctx.store.add_msg(ref, "owner", "jarvis", text)
    except Exception:
        pass


async def projects_scan(ctx: Ctx, a: dict) -> Result:
    server = str(a.get("server", ""))
    nodes, problem = _nodes(ctx, server)
    if problem:
        return problem
    code, text = await nodes.exec(server, relocate.SCAN, 60)
    if code != 0 and not text:
        return fail(f"не смог посмотреть {server} (код {code})")
    return Result(True, relocate.render_scan(server, text), evidence=f"scan {server}")


async def project_move(ctx: Ctx, a: dict) -> Result:
    server, project = str(a.get("server", "")).strip(), str(a.get("project", "")).strip().lower()
    nodes, problem = _nodes(ctx, server)
    if problem:
        return problem
    key = f"{server.lower()}:{project}"
    if _moves.get(key, {}).get("running"):
        return fail(f"перенос «{project}» уже идёт: {_moves[key]['last']}")
    try:
        facts, check = await _plan(ctx, nodes, server, project)
    except Refused as e:
        return fail(str(e))
    relocator = _relocator(ctx, nodes, server, project)
    text = relocator.describe(facts, check)
    if a.get("plan_only"):
        return Result(True, text, evidence=f"plan {server}:{project}")
    if not check.ready:
        return fail(text)

    job = next(_job_ids)
    state = _moves[key] = {"job": job, "running": True, "last": "готовлюсь", "started": time.time()}
    frozen_at = ctx.core.frozen_until

    async def note(step: str, ok: bool, line: str) -> None:
        state["last"] = f"{step}: {line}"
        ctx.store.journal_add(ctx.itx.id, f"project_move:{step}", {"server": server, "project": project}, ok, line)
        if step != "dump":
            await _tell(ctx, ("✓ " if ok else "✗ ") + f"{project}: {line}")

    live = _relocator(ctx, nodes, server, project, note=note, aborted=lambda: ctx.core.frozen_until > frozen_at)

    async def go() -> None:
        try:
            outcome = await live.move(facts, check)
        except Exception as e:  # move() puts the old project back itself; this is for what it could not foresee
            outcome = relocate.Outcome(False, f"Перенос «{project}» сломался ({type(e).__name__}: {relocate.mask(str(e))[:200]}). "
                                              "Проверь старое на сервере руками: откат могу сделать командой project_move_back.")
        state.update(running=False, last=outcome.text[:200])
        ctx.store.journal_add(ctx.itx.id, "project_move:result", {"server": server, "project": project}, outcome.ok,
                              outcome.text, outcome.evidence)
        await _tell(ctx, outcome.text)

    ctx.core.spawn(go())
    return Result(True, f"Перенос «{project}» запущен в фоне (#{job}). Пишу в чат на каждом этапе. Готово — только когда "
                        "придёт итоговое сообщение с проверкой: до него не говори, что перенесено.", evidence=f"move #{job} started")


async def _guard_move(ctx: Ctx, a: dict):
    if a.get("plan_only"):
        return None
    server, project = str(a.get("server", "")).strip(), str(a.get("project", "")).strip().lower()
    nodes, problem = _nodes(ctx, server)
    if problem:
        return None
    try:
        facts, check = await _plan(ctx, nodes, server, project)
    except Refused:
        return None
    if not check.ready:
        return None  # nothing will change: the tool says what is missing
    where = "в docker compose" if facts.kind == "compose" else "как папку с файлами"
    return guard.ask(
        f"Перенос «{project}» с {server} на этот сервер ({facts.size_kb // 1024} МБ, {where}). Порядок: дамп базы, "
        f"остановлю его на {server} (не удаляю), архив, распакую в {facts.path}, запущу здесь и проверю. Не поднимется — "
        f"запущу старое обратно. Бэкап остаётся. Начинать?", f"move:{server.lower()}:{project}")


async def project_move_back(ctx: Ctx, a: dict) -> Result:
    server, project = str(a.get("server", "")).strip(), str(a.get("project", "")).strip().lower()
    nodes, problem = _nodes(ctx, server)
    if problem:
        return problem
    if _moves.get(f"{server.lower()}:{project}", {}).get("running"):
        return fail("перенос ещё идёт: дождись итога, потом откатывать")
    outcome = await _relocator(ctx, nodes, server, project).rollback()
    _plans.pop(f"{server.lower()}:{project}", None)
    return Result(outcome.ok, outcome.text if outcome.ok else "Ошибка: " + outcome.text, evidence=outcome.evidence)


async def project_moves(ctx: Ctx, a: dict) -> Result:
    if not _moves:
        return Result(True, "Переносов с последнего запуска не было.")
    lines = []
    for key, st in _moves.items():
        lines.append(f"#{st['job']} {key}: " + ("идёт, " if st["running"] else "закончен, ") + st["last"])
    return Result(True, "\n".join(lines))


def tools() -> list[Tool]:
    return [
        Tool("projects_scan", "Что лежит на другом сервере Вана (папки в /opt и docker-контейнеры), чтобы выбрать, что "
             "переносить. VPN ноутбука (amnezia) помечен «не трогать».", {"server": S}, ["server"], projects_scan,
             label=lambda a, r: f"смотрю проекты на {a.get('server', '')}"),
        Tool("project_move", "Перенести проект с другого сервера на этот (папка /opt/<project>). Умеет сам: проверки, дамп "
             "Postgres, остановить старое (не удалять), архив, распаковка, запуск и проверка здоровья, откат на старое при "
             "сбое. Всегда сначала вызови с plan_only=true и покажи, что мешает; без plan_only спросит Вана один раз и "
             "работает в фоне, итог придёт сообщением. Docker-проекты с общей сетью переноси в порядке зависимостей (проект, "
             "который создаёт сеть, первым). Нужен сервер с полным доступом. Руками (ssh_run, server_shell) такой перенос "
             "не делать: боты на polling нельзя держать на двух серверах сразу, а бэкап и проверки без этого инструмента "
             "легко забыть. Cloudflare (DNS, маршруты) не трогает: это Ван.",
             {"server": S, "project": S, "plan_only": B}, ["server", "project"], project_move, risk=Risk.RED,
             guard=_guard_move, label=lambda a, r: f"перенос {a.get('project', '')}"),
        Tool("project_move_back", "Вернуть всё как было после неудачного или ненужного переноса: остановить копию здесь и "
             "запустить старое на прежнем сервере (по записи о последнем переносе). Только если Ван просит или перенос "
             "сломался и сам не откатился.", {"server": S, "project": S}, ["server", "project"], project_move_back,
             risk=Risk.OWN, label=lambda a, r: f"откат переноса {a.get('project', '')}"),
        Tool("project_moves", "Какие переносы идут или закончились с последнего запуска.", {}, [], project_moves),
    ]
