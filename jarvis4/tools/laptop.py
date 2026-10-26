"""The owner's laptop as tools. Every action reports what really changed, so Jarvis never says «открыл» on faith.

Opening an app lists the windows that appeared, with their kind (app, terminal, browser): «Claude» can be a
desktop app, a terminal or a website, and only the result tells which one came up. Closing lists what is left.
"""
from __future__ import annotations

from .. import guard
from ..ports import Offline, Window
from ..types import Result, fail
from .base import B, I, S, Ctx, Risk, Tool

KIND_RU = {"app": "приложение", "terminal": "терминал", "browser": "сайт в браузере"}


def describe(w: Window) -> str:
    return f"«{w.title}» ({KIND_RU.get(w.kind, w.kind)}, {w.process or '?'})"


async def _lap(ctx: Ctx):
    lap = ctx.pf.laptop
    if lap is None or not lap.online:
        raise Offline("ноутбук не подключён")
    return lap


def _wrap(fn):
    async def run(ctx: Ctx, a: dict) -> Result:
        try:
            return await fn(ctx, a, await _lap(ctx))
        except Offline as e:
            return fail(str(e))
    return run


async def _shell(ctx, a, lap) -> Result:
    out = await lap.shell(str(a.get("command", "")), min(int(a.get("timeout") or 60), 600))
    return Result(not out.startswith("Ошибка"), out or "(пусто)")


async def _guard_shell(ctx: Ctx, a: dict):
    return guard.shell(str(a.get("command", "")), "laptop")


async def _windows(ctx, a, lap) -> Result:
    found = await lap.windows()
    return Result(True, "\n".join(describe(w) for w in found) or "Открытых окон нет.")


async def _look(ctx, a, lap) -> Result:
    return Result(True, await lap.look())


async def _click(ctx, a, lap) -> Result:
    out = await lap.click(str(a.get("element", "")))
    return Result(not out.startswith("Ошибка"), out)


async def _open(ctx, a, lap) -> Result:
    name = str(a.get("name", ""))
    before = {w.title for w in await lap.windows()}
    said = await lap.open(name)
    if said.startswith("Ошибка"):
        return Result(False, said)
    after = await lap.windows()
    new = [w for w in after if w.title not in before]
    if not new:
        return Result(True, f"{said} Новых окон не появилось (может, уже было открыто). Окна сейчас: "
                      + (", ".join(describe(w) for w in after) or "нет"), evidence="новых окон нет")
    text = f"{said} Появилось: " + ", ".join(describe(w) for w in new) + "."
    odd = [w for w in new if w.kind in ("terminal", "browser")]
    if odd:
        text += (" Внимание: это не приложение, а " + "/".join(KIND_RU[w.kind] for w in odd) +
                 ". Если Ван просил программу, закрой это окно и найди кнопку программы на панели задач "
                 "(laptop_look → laptop_click) или запусти её по полному пути.")
    return Result(True, text, evidence="новые окна: " + ", ".join(describe(w) for w in new))


async def _close(ctx, a, lap) -> Result:
    title = str(a.get("title", ""))
    said = await lap.close(title)
    left = await lap.windows()
    still = [w for w in left if title.lower() in w.title.lower()]
    if said.startswith("Ошибка"):
        return Result(False, said)
    text = f"{said} Окна сейчас: " + (", ".join(describe(w) for w in left) or "нет") + "."
    if still:
        return Result(False, "Ошибка: " + text + " «" + title + "» всё ещё открыто.")
    return Result(True, text, evidence="окон с таким названием не осталось")


async def _focus(ctx, a, lap) -> Result:
    out = await lap.focus(str(a.get("title", "")))
    return Result(not out.startswith("Ошибка"), out)


async def _type(ctx, a, lap) -> Result:
    return Result(True, await lap.type(str(a.get("text", ""))))


async def _keys(ctx, a, lap) -> Result:
    return Result(True, await lap.keys(str(a.get("keys", ""))))


async def _media(ctx, a, lap) -> Result:
    out = await lap.media(str(a.get("action", "play_pause")))
    return Result(not out.startswith("Ошибка"), out, evidence=out)


async def _volume(ctx, a, lap) -> Result:
    return Result(True, await lap.volume(int(a.get("percent", 40))))


async def _clipboard(ctx, a, lap) -> Result:
    return Result(True, await lap.clipboard(a.get("text") if "text" in a else None))


def tools() -> list[Tool]:
    return [
        Tool("laptop_shell", "Команда PowerShell на ноутбуке Вана с полным доступом: файлы, процессы, программы, git. "
             "Часто быстрее кликов.", {"command": S, "timeout": I}, ["command"], _wrap(_shell), risk=Risk.OWN,
             guard=_guard_shell, label=lambda a, r: "команда на ноутбуке"),
        Tool("laptop_windows", "Какие окна открыты на ноутбуке: название, вид (приложение, терминал, сайт), процесс.",
             {}, [], _wrap(_windows), label=lambda a, r: "смотрю окна"),
        Tool("laptop_look", "Посмотреть на экран ноутбука: активное окно, панель задач с кнопками (tb_…), список "
             "элементов с id для laptop_click.", {}, [], _wrap(_look), label=lambda a, r: "смотрю на экран"),
        Tool("laptop_click", "Нажать элемент из последнего laptop_look (его id).", {"element": S}, ["element"],
             _wrap(_click), risk=Risk.OWN, label=lambda a, r: f"нажимаю {a.get('element', '')}"),
        Tool("laptop_open", "Запустить программу по названию. Результат говорит, какие окна появились и что это "
             "(приложение, терминал или сайт): проверь, то ли открылось.", {"name": S}, ["name"], _wrap(_open),
             risk=Risk.OWN, label=lambda a, r: f"открываю {a.get('name', '')}"),
        Tool("laptop_close", "Закрыть окно по части названия. Результат показывает, какие окна остались.",
             {"title": S}, ["title"], _wrap(_close), risk=Risk.OWN, label=lambda a, r: f"закрываю {a.get('title', '')}"),
        Tool("laptop_focus", "Переключиться на окно по части названия.", {"title": S}, ["title"], _wrap(_focus),
             risk=Risk.OWN),
        Tool("laptop_type", "Напечатать текст в активное окно.", {"text": S}, ["text"], _wrap(_type), risk=Risk.OWN),
        Tool("laptop_keys", "Нажать сочетание клавиш, например ctrl+v, enter, alt+tab.", {"keys": S}, ["keys"],
             _wrap(_keys), risk=Risk.OWN),
        Tool("laptop_media", "Плеер: play_pause, next, previous, stop. Ответ говорит, играет ли сейчас музыка.",
             {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, ["action"],
             _wrap(_media), risk=Risk.OWN, label=lambda a, r: "плеер"),
        Tool("laptop_volume", "Громкость 0–100.", {"percent": I}, ["percent"], _wrap(_volume), risk=Risk.OWN),
        Tool("laptop_clipboard", "Буфер обмена: без text — прочитать, с text — положить.", {"text": S}, [],
             _wrap(_clipboard), risk=Risk.OWN),
    ]
