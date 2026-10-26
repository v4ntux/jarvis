"""The Android phone as tools: its state, notifications, apps, a shell. Anything with buttons goes through
screen_task(device="android"), which presses the phone's screen the way it presses the laptop's."""
from __future__ import annotations

from .. import guard
from ..nodes.android import AndroidError
from ..ports import Offline
from ..types import Result, fail
from .base import S, Ctx, Risk, Tool


def _phone(ctx: Ctx):
    node = ctx.pf.extras.get("android")
    if node is None:
        raise Offline("телефон не подключён: его надо один раз настроить (README_JARVIS4, раздел про Android)")
    return node


def _wrap(fn):
    async def run(ctx: Ctx, a: dict) -> Result:
        try:
            return await fn(_phone(ctx), a)
        except (Offline, AndroidError) as e:
            return fail(str(e))
    return run


async def _status(node, a) -> Result:
    return Result(True, await node.status())


async def _notifications(node, a) -> Result:
    return Result(True, await node.notifications(int(a.get("limit") or 15)))


async def _open(node, a) -> Result:
    said = await node.open_app(str(a.get("package", "")))
    return Result(True, said, evidence=said)


async def _apps(node, a) -> Result:
    return Result(True, await node.apps(str(a.get("query", ""))))


async def _shell(node, a) -> Result:
    out = await node.shell(str(a.get("command", "")), min(int(a.get("timeout") or 30), 120))
    return Result(True, out)


async def _guard_shell(ctx: Ctx, a: dict):
    return guard.shell(str(a.get("command", "")), "android")


def tools() -> list[Tool]:
    return [
        Tool("android_status", "Состояние телефона Вана: модель, Android, батарея, экран, что открыто сейчас.", {}, [],
             _wrap(_status), label=lambda a, r: "смотрю телефон"),
        Tool("android_notifications", "Уведомления на телефоне Вана: от какого приложения, заголовок, текст.",
             {"limit": {"type": "integer"}}, [], _wrap(_notifications), label=lambda a, r: "читаю уведомления"),
        Tool("android_apps", "Установленные приложения телефона (имена пакетов); query — часть имени.", {"query": S}, [],
             _wrap(_apps)),
        Tool("android_open", "Запустить приложение на телефоне по имени пакета (найди через android_apps).",
             {"package": S}, ["package"], _wrap(_open), risk=Risk.OWN, label=lambda a, r: "открываю на телефоне"),
        Tool("android_shell", "Команда оболочки на телефоне (adb shell). Для кнопок и экрана лучше screen_task.",
             {"command": S, "timeout": {"type": "integer"}}, ["command"], _wrap(_shell), risk=Risk.OWN,
             guard=_guard_shell, label=lambda a, r: "команда на телефоне"),
    ]
