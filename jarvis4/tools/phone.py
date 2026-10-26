"""The owner's own phone: an SMS or a robot call. Plain SMS and calls work when mobile internet is cut off, so this is
how Jarvis gets through when Telegram does not. Only the owner's number is ever used, with a daily limit."""
from __future__ import annotations

from ..ops.phone import PhoneError
from ..types import Result, fail
from .base import S, Ctx, Risk, Tool


def _phone(ctx: Ctx):
    return ctx.pf.extras.get("phone")


async def _send(ctx: Ctx, a: dict, kind: str) -> Result:
    phone = _phone(ctx)
    text = str(a.get("text", "")).strip()
    if phone is None:
        return fail("SMS и звонки на телефон не настроены.")
    if not text:
        return fail("нечего отправлять.")
    try:
        said = await (phone.sms if kind == "sms" else phone.call)("Джарвис: " + text)
    except PhoneError as e:
        return fail(str(e))
    return Result(True, said, evidence="sms отправлено" if kind == "sms" else "робот набрал")


async def send_sms(ctx: Ctx, a: dict) -> Result:
    return await _send(ctx, a, "sms")


async def phone_call(ctx: Ctx, a: dict) -> Result:
    return await _send(ctx, a, "call")


def tools() -> list[Tool]:
    return [
        Tool("send_sms", "SMS на телефон Вана (обычная связь, работает без интернета). Платно, лимит в день: только по "
             "его просьбе или когда очень срочно.", {"text": S}, ["text"], send_sms, risk=Risk.OWN,
             label=lambda a, r: "SMS Вану"),
        Tool("phone_call", "Звонок робота на телефон Вана: проговорит text и положит трубку. Платно, лимит в день: только "
             "по его просьбе или когда очень срочно. Обычный звонок в Telegram — tg_call, он бесплатный.",
             {"text": S}, ["text"], phone_call, risk=Risk.OWN, label=lambda a, r: "звонок на телефон Вана"),
    ]
