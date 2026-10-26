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


def tools() -> list[Tool]:
    return [
        Tool("server_shell", "Команда bash на сервере Джарвиса (его дом, работает всегда): скрипты, скачивание, "
             "архивы, бекап, обработка файлов. Свои секреты (.env, сессия Telegram) не читать и не трогать.",
             {"command": S, "timeout": I}, ["command"], server_shell, risk=Risk.OWN, guard=_guard_shell,
             label=lambda a, r: "команда на сервере"),
        Tool("monitor_status", "Что с сайтами Вана и серверами: статус, диск, память, входы по SSH. Цифры отвечай только "
             "из этого результата.", {}, [], monitor_status, light=True, label=lambda a, r: "смотрю мониторинг"),
        Tool("web_search", "Поиск в интернете (бесплатно). news=true для свежих новостей. Для фактов, незнакомых слов, "
             "мемов и «как это делается».", {"query": S, "news": B}, ["query"], web_search, light=True,
             label=lambda a, r: f"ищу: {a.get('query', '')[:40]}"),
        Tool("read_url", "Скачать страницу и получить её текст.", {"url": S}, ["url"], read_url, light=True,
             label=lambda a, r: "читаю страницу"),
    ]
