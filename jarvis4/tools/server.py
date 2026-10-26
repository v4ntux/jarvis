"""The server, the web and the monitor as tools. Jarvis's home does everything that does not need the laptop's screen."""
from __future__ import annotations

from .. import guard
from ..types import Result, fail
from .base import B, I, S, Ctx, Risk, Tool


async def server_shell(ctx: Ctx, a: dict) -> Result:
    out = await ctx.pf.server.shell(str(a.get("command", "")), min(int(a.get("timeout") or 60), 600))
    return Result(not out.startswith("Ошибка"), out or "(пусто)")


async def _guard_shell(ctx: Ctx, a: dict):
    return guard.shell(str(a.get("command", "")), "server")


async def monitor_status(ctx: Ctx, a: dict) -> Result:
    return Result(True, await ctx.pf.server.monitor())


async def web_search(ctx: Ctx, a: dict) -> Result:
    q = str(a.get("query", "")).strip()
    if not q:
        return fail("пустой запрос.")
    return Result(True, await ctx.pf.web.search(q, bool(a.get("news"))))


async def read_url(ctx: Ctx, a: dict) -> Result:
    text = await ctx.pf.web.fetch(str(a.get("url", "")))
    return Result(not text.startswith("Ошибка"), text)


async def server_job(ctx: Ctx, a: dict) -> Result:
    """A long command in the background; the owner hears when it ends, and the follow-up he ordered starts."""
    node = ctx.pf.server
    if not hasattr(node, "job"):
        return fail("фоновые задачи здесь недоступны.")
    command, what = str(a.get("command", "")), str(a.get("what") or "фоновая задача")
    then = str(a.get("then") or "") if ctx.itx.owner else ""

    async def done(job_id: int, code: int, tail: str) -> None:
        await ctx.core.on_job_done(what, job_id, code, tail, then)

    job_id = await node.job(command, what, done)
    return Result(True, f"Фоновая задача #{job_id} запущена. Когда закончится, придёт сообщение.",
                  evidence=f"job {job_id}")


async def server_jobs(ctx: Ctx, a: dict) -> Result:
    node = ctx.pf.server
    found = node.jobs() if hasattr(node, "jobs") else []
    return Result(True, "\n".join(f"#{i} {what}" for i, what in found) or "Фоновых задач нет.")


async def stop_job(ctx: Ctx, a: dict) -> Result:
    node = ctx.pf.server
    stopped = hasattr(node, "stop_job") and node.stop_job(int(a.get("id", 0)))
    return Result(True, "Остановил.") if stopped else fail("такой задачи нет или она уже закончилась.")


def tools() -> list[Tool]:
    return [
        Tool("server_shell", "Команда bash на сервере Джарвиса (его дом, работает всегда): скрипты, скачивание, "
             "архивы, бекап, обработка файлов. Свои секреты (.env, сессия Telegram) не читать и не трогать.",
             {"command": S, "timeout": I}, ["command"], server_shell, risk=Risk.OWN, guard=_guard_shell,
             label=lambda a, r: "команда на сервере"),
        Tool("server_job", "Долгая команда на сервере в фоне (скачивание, конвертация, длинный скрипт): сразу вернётся, "
             "когда закончится — Ван получит сообщение. what — коротко, что это. then — что сделать после, если Ван "
             "просил продолжение («скинь результат», «пришли файл»).", {"command": S, "what": S, "then": S},
             ["command", "what"], server_job, risk=Risk.OWN, guard=_guard_shell, label=lambda a, r: "фоновая задача"),
        Tool("server_jobs", "Какие фоновые задачи сейчас идут.", {}, [], server_jobs),
        Tool("stop_job", "Остановить фоновую задачу по номеру.", {"id": I}, ["id"], stop_job, risk=Risk.OWN),
        Tool("monitor_status", "Что с сайтами Вана и серверами: статус, диск, память, входы по SSH. Цифры отвечай только "
             "из этого результата.", {}, [], monitor_status, light=True, label=lambda a, r: "смотрю мониторинг"),
        Tool("web_search", "Поиск в интернете (бесплатно). news=true для свежих новостей. Для фактов, незнакомых слов, "
             "мемов и «как это делается».", {"query": S, "news": B}, ["query"], web_search, light=True,
             label=lambda a, r: f"ищу: {a.get('query', '')[:40]}"),
        Tool("read_url", "Скачать страницу и получить её текст.", {"url": S}, ["url"], read_url, light=True,
             label=lambda a, r: "читаю страницу"),
    ]
