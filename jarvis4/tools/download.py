"""Downloading media for anybody: a video, picture or sound from TikTok, Instagram, Pinterest, YouTube and so on, sent
right into the chat. The same thing happens without a model when somebody sends a bare link (core._grab)."""
from __future__ import annotations

from ..ops import grab
from ..ports import TGError
from ..types import Result, fail
from .base import B, S, Ctx, Risk, Tool
from .telegram import _reach_guard, resolve


async def media_download(ctx: Ctx, a: dict) -> Result:
    url = grab.link_in(str(a.get("url") or "")) or str(a.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return fail("нужна ссылка на видео или пост.")
    chat, reply = ctx.itx.chat, ctx.itx.ref if ctx.itx.kind == "group" else ""
    if a.get("to") and ctx.itx.owner:
        person, error = await resolve(ctx, a["to"])
        if person is None:
            return fail(error)
        chat, reply = person.key, ""
    try:
        said = await grab.deliver(ctx.pf.tg, ctx.store, chat, url, ctx.itx.owner, reply, bool(a.get("audio")))
    except (grab.GrabError, TGError) as e:
        return fail(f"не скачалось: {e}")
    return Result(True, said, evidence=said)


async def _guard(ctx: Ctx, a: dict):
    if a.get("to") and ctx.itx.owner:
        return await _reach_guard(ctx, {"to": a["to"]}, "отправить видео")
    return None


def tools() -> list[Tool]:
    return [
        Tool("media_download", "Скачать видео, фото или звук по ссылке (TikTok, Instagram, Pinterest, YouTube, X, VK, "
             "Reddit и др.) и сразу отправить файлом в этот чат (Ван может указать to). audio=true — только звук mp3. "
             "Для всех, не только для Вана.", {"url": S, "audio": B, "to": S}, ["url"], media_download, risk=Risk.OWN,
             terminal=True, light=True, others=True, guard=_guard, label=lambda a, r: "качаю видео"),
    ]
