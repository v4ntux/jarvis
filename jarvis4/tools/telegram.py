"""Jarvis's own Telegram account as tools: find people, read chats, forward, write, call, react, leave.

The model names messages by ref and people by name; the guard decides whom Jarvis may reach on his own (anyone
the owner named, or everybody he already talks with when the owner asked for all of them) and the platform paces
and delivers. Nothing here shells out: the Telegram session is never touched by anything but the transport.
"""
from __future__ import annotations

import os
import re

from .. import guard, names
from ..ports import TGError
from ..store import Person
from ..types import Result, fail
from .base import B, I, S, Ctx, Risk, Tool, owner_person

OWNER_WORDS = {"owner", "ван", "van", "me", "я", "мне", "батя", "хозяин"}
MASS = re.compile(r"(?<!\w)(всем|всех|каждому|каждого|все,? кто|all|everyone)(?!\w)", re.I)
FILE_ROOTS = ("/opt/jarvis/data/inbox", "/opt/jarvis/data/work")


async def resolve(ctx: Ctx, query: str) -> tuple[Person | None, str]:
    """The person meant by a name, @username, number or key; or why not."""
    q = str(query or "").strip()
    if not q:
        return None, "Ошибка: не сказано, кому."
    if q.lower() in OWNER_WORDS:
        return owner_person(ctx.store), ""
    found = ctx.store.find_people(q)
    if len(found) == 1:
        return found[0], ""
    if len(found) > 1:
        return None, f"Ошибка: под «{q}» подходят несколько: " + "; ".join(p.label for p in found[:8]) + ". Уточни, кому."
    if q.startswith("@") or "t.me/" in q or re.fullmatch(r"\+?\d[\d\s()-]{6,}", q):
        got = await ctx.pf.tg.lookup(q)
        if isinstance(got, str):
            return None, got
        return got, ""
    return None, f"Ошибка: «{q}» нет среди моих чатов и контактов. Нужен @username или номер телефона."


def authorized(ctx: Ctx, person: Person) -> bool:
    """Did the owner name this person, or ask for everyone Jarvis already talks with?"""
    if person.key == "owner":
        return True
    said = ctx.owner_words()
    if names.mentioned(person.username and "@" + person.username or person.name, said) or \
            any(names.mentioned(n, said) for n in person.names() if n and len(n) >= 3):
        return True
    return bool(person.dialog and MASS.search(said))


async def _reach_guard(ctx: Ctx, args: dict, verb: str) -> guard.Verdict | None:
    person, error = await resolve(ctx, args.get("to", ""))
    if person is None:
        return None  # the tool itself reports it
    if person.blocked:
        return guard.deny(f"{person.name} заблокировал мой аккаунт: {verb} ему не буду")
    if person.key == "owner":
        return None
    if not person.dialog:
        return guard.ask(f"Я ещё не писал {person.label}, а новые знакомства Telegram считает спамом. "
                         f"{verb.capitalize()} первым?", f"first:{person.key}")
    if not authorized(ctx, person):
        return guard.ask(f"{verb.capitalize()} {person.label}? Этого человека ты не называл.", f"reach:{person.key}")
    return None


def _file(name: str) -> tuple[str, bytes] | str:
    """A file of Jarvis's own (inbox or work) to send to a person; anything else stays put."""
    path = os.path.realpath(os.path.expanduser(name))
    if not any(path.startswith(os.path.realpath(root) + os.sep) for root in FILE_ROOTS):
        return "Ошибка: людям можно отправлять только файлы из inbox и work."
    if not os.path.isfile(path):
        return f"Ошибка: нет файла {name}."
    if os.path.getsize(path) > 1000 * 1024 * 1024:
        return f"Ошибка: {os.path.basename(path)} больше 1 ГБ."
    with open(path, "rb") as f:
        return os.path.basename(path), f.read()


def render(ctx: Ctx, messages) -> str:
    lines = []
    for m in messages:
        who = ctx.core.name_of(m.sender)
        media = ""
        if m.media:
            what = {"sticker": "стикер", "gif": "гифка", "photo": "фото", "video": "видео", "voice": "голосовое"}
            media = f"[{what.get(m.media.get('kind'), 'вложение')} {m.media.get('emoji') or m.media.get('about') or ''}".rstrip() + "] "
        lines.append(f"[{m.ref}] {who}: {media}{m.text}".rstrip())
    return "\n".join(lines)


# ---- tools ---------------------------------------------------------------------------------------------------------

async def tg_find(ctx: Ctx, a: dict) -> Result:
    person, error = await resolve(ctx, a.get("query", ""))
    if person is None:
        return fail(error)
    state = ("заблокировал меня" if person.blocked else "переписка уже есть" if person.dialog
             else "переписки ещё не было (писать первым нельзя без вопроса)")
    return Result(True, f"{person.label}, id {person.tg_id or '?'}; {state}.")


async def tg_history(ctx: Ctx, a: dict) -> Result:
    chat = str(a.get("chat") or ctx.itx.chat)
    key = chat
    if chat not in {c[0] for c in ctx.store.chats()} and not ctx.store.person(chat):
        person, error = await resolve(ctx, chat)
        if person is None:
            group = next((c for c in ctx.store.chats("group") if names.matches(chat, [c[2]])), None)
            if group is None:
                return fail(error)
            key = group[0]
        else:
            key = person.key
    await ctx.pf.tg.sync_history(key, max(50, int(a.get("limit") or 30)))
    found = ctx.store.chat_msgs(key, str(a.get("kind") or ""), min(int(a.get("limit") or 30), 100), str(a.get("after") or ""))
    if not found:
        return Result(True, "Подходящих сообщений нет.")
    return Result(True, render(ctx, found))


async def tg_chats(ctx: Ctx, a: dict) -> Result:
    lines = []
    for p in ctx.store.people():
        if p.key == "owner" or not p.dialog:
            continue
        last = ctx.store.recent(p.key, 1)
        tail = f"; последнее: {last[0].text[:60]}" if last and last[0].text else ""
        lines.append(f"{p.label}{' — ЗАБЛОКИРОВАЛ меня' if p.blocked else ''}{tail}")
    return Result(True, "\n".join(lines[: int(a.get("limit") or 30)]) or "Мне пока никто не писал.")


async def tg_send(ctx: Ctx, a: dict) -> Result:
    person, error = await resolve(ctx, a.get("to", ""))
    if person is None:
        return fail(error)
    text = str(a.get("text") or "").strip()
    files = []
    if a.get("file"):
        got = _file(str(a["file"]))
        if isinstance(got, str):
            return fail(got)
        files.append(got)
    if not text and not files:
        return fail("нечего отправлять: ни текста, ни файла.")
    try:
        ref = await ctx.pf.tg.send(person.key, text, files)
    except TGError as e:
        return fail(str(e))
    ctx.store.add_msg(ref, person.key, "jarvis", text, {"kind": "file", "about": files[0][0]} if files else None,
                      task=ctx.itx.id)
    if person.key != "owner":
        ctx.store.upsert_person(person.key, dialog=True)
    return Result(True, f"Отправил {person.label}.", evidence=f"сообщение {ref}")


async def tg_forward(ctx: Ctx, a: dict) -> Result:
    refs = [str(r).strip("[]@ ") for r in a.get("refs") or []]
    if not refs:
        return fail("не указано, что пересылать (refs).")
    missing = [r for r in refs if ctx.store.msg(r) is None]
    if missing:
        return fail(f"нет сообщений {', '.join(missing)}. Ссылки берутся из переписки или tg_history.")
    person, error = await resolve(ctx, a.get("to") or "owner")
    if person is None:
        return fail(error)
    try:
        new = await ctx.pf.tg.forward(refs, person.key)
    except TGError as e:
        return fail(str(e))
    for old, fresh in zip(refs, new):
        src = ctx.store.msg(old)
        ctx.store.add_msg(fresh, person.key, "jarvis", src.text, src.media, forward_ref=old, task=ctx.itx.id)
    kinds = sorted({(ctx.store.msg(r).media or {}).get("kind", "текст") for r in refs})
    source = ctx.core.name_of(ctx.store.msg(refs[0]).chat)
    return Result(True, f"Переслал {len(new)} сообщ. ({', '.join(kinds)}) из чата {source} → {person.label}.",
                  evidence="пересланы " + ", ".join(new))


async def tg_call(ctx: Ctx, a: dict) -> Result:
    person, error = await resolve(ctx, a.get("to", ""))
    if person is None:
        return fail(error)
    mode = "talk" if a.get("mode") == "talk" else "say"
    goal = str(a.get("goal") or "").strip()
    if not goal:
        return fail("не сказано, о чём говорить (goal).")
    try:
        res = await ctx.pf.tg.call(person.key, mode, goal)
    except TGError as e:
        return fail(str(e))
    return Result(res.answered, res.text if res.answered else res.text, evidence=("дозвонился" if res.answered else "не дозвонился"))


async def tg_react(ctx: Ctx, a: dict) -> Result:
    ref = str(a.get("ref") or ctx.itx.ref)
    if not ref:
        return fail("не на что ставить реакцию.")
    await ctx.pf.tg.react(ref, str(a.get("emoji") or "👍"))
    return Result(True, "Поставил.")


async def _where(ctx: Ctx, to: str) -> tuple[str, str]:
    """(chat key, reply ref) for a sticker or GIF: the chat Jarvis is in, as a reply when it is a group."""
    if not to:
        return ctx.itx.chat, ctx.itx.ref if ctx.itx.kind == "group" else ""
    person, _ = await resolve(ctx, to)
    return (person.key if person else ctx.itx.chat), ""


async def tg_sticker(ctx: Ctx, a: dict) -> Result:
    chat, reply = await _where(ctx, str(a.get("to") or ""))
    emoji = str(a.get("emoji") or "😄")
    ref = await ctx.pf.tg.sticker(chat, emoji, reply)
    if ref:
        ctx.store.add_msg(ref, chat, "jarvis", "", {"kind": "sticker", "emoji": emoji}, reply_ref=reply, task=ctx.itx.id)
    return Result(bool(ref), "Отправил стикер." if ref else "Ошибка: подходящего стикера не нашлось.")


async def tg_gif(ctx: Ctx, a: dict) -> Result:
    chat, reply = await _where(ctx, str(a.get("to") or ""))
    query = str(a.get("query") or "")
    ref = await ctx.pf.tg.gif(chat, query, reply)
    if ref:
        ctx.store.add_msg(ref, chat, "jarvis", "", {"kind": "gif", "about": query}, reply_ref=reply, task=ctx.itx.id)
    return Result(bool(ref), "Отправил гифку." if ref else "Ошибка: гифка не нашлась.")


async def tg_leave(ctx: Ctx, a: dict) -> Result:
    chat = str(a.get("chat") or ctx.itx.chat)
    group = next((c for c in ctx.store.chats("group") if chat in (c[0], c[2]) or names.matches(chat, [c[2]])), None)
    if group is None:
        return fail(f"«{chat}» не группа, из которой можно выйти.")
    try:
        await ctx.pf.tg.leave(group[0])
    except TGError as e:
        return fail(str(e))
    return Result(True, f"Вышел из группы «{group[2]}».", evidence="вышел")


async def pass_to_owner(ctx: Ctx, a: dict) -> Result:
    note = str(a.get("note") or "").strip()
    if not note:
        return fail("записка пустая.")
    await ctx.core.relay(ctx.itx, note)
    return Result(True, "Передам.")


async def _guard_send(ctx: Ctx, a: dict):
    return await _reach_guard(ctx, a, "написать")


async def _guard_call(ctx: Ctx, a: dict):
    return await _reach_guard(ctx, a, "позвонить")


async def _guard_forward(ctx: Ctx, a: dict):
    return await _reach_guard(ctx, {"to": a.get("to") or "owner"}, "переслать")


def tools() -> list[Tool]:
    return [
        Tool("tg_find", "Найти человека в моих чатах и контактах (имя, @username, номер): кто он, есть ли переписка, "
             "не заблокировал ли меня.", {"query": S}, ["query"], tg_find, light=True,
             label=lambda a, r: f"ищу {a.get('query', '')}"),
        Tool("tg_history", "Прочитать чат: сообщения со ссылками [ref]. kind: sticker | gif | photo | video | voice — "
             "только такие. after — ref сообщения, после которого читать. Ссылки нужны для tg_forward и tg_react.",
             {"chat": S, "kind": S, "after": S, "limit": I}, [], tg_history, light=True,
             label=lambda a, r: f"читаю чат {a.get('chat', '')}"),
        Tool("tg_chats", "С кем у меня есть переписка и что было последним. Для «напиши всем, с кем я общаюсь».",
             {"limit": I}, [], tg_chats, light=True),
        Tool("tg_send", "Написать человеку от своего аккаунта: текст и/или файл из inbox/work. Только тем, кого Ван "
             "назвал (или «всем» знакомым). Звонить вместо этого нельзя: звонок — это tg_call.",
             {"to": S, "text": S, "file": S}, ["to"], tg_send, risk=Risk.OUT, light=True, guard=_guard_send,
             label=lambda a, r: f"пишу {a.get('to', '')}"),
        Tool("tg_forward", "Переслать сообщения (стикеры, гифки, фото, текст) по ссылкам refs. Кому — to, по "
             "умолчанию Вану. Это правильный способ «скинь этот стикер», а не скрипты на сервере.",
             {"refs": {"type": "array", "items": S}, "to": S}, ["refs"], tg_forward, risk=Risk.OUT, light=True,
             guard=_guard_forward, label=lambda a, r: "пересылаю"),
        Tool("tg_call", "Позвонить человеку в Telegram. mode=say — сказать goal и положить трубку; mode=talk — "
             "живой разговор на тему goal (привет, пообщаться). Звонок не заменяется сообщением. Не взял трубку — "
             "так и скажи Вану, ему не пиши.",
             {"to": S, "mode": {"type": "string", "enum": ["say", "talk"]}, "goal": S}, ["to", "goal"], tg_call,
             risk=Risk.OUT, light=True, guard=_guard_call, label=lambda a, r: f"звоню {a.get('to', '')}"),
        Tool("tg_react", "Поставить эмодзи-реакцию под сообщением (по умолчанию под последним).",
             {"emoji": S, "ref": S}, ["emoji"], tg_react, terminal=True, light=True, others=True),
        Tool("tg_sticker", "Отправить стикер по эмодзи (в текущий чат или to). Редко и только в тему.",
             {"emoji": S, "to": S}, ["emoji"], tg_sticker, terminal=True, light=True, others=True),
        Tool("tg_gif", "Отправить гифку по поисковому запросу из 2–4 слов по-английски (в текущий чат или to). "
             "«#12» — одна из любимых гифок.", {"query": S, "to": S}, ["query"], tg_gif, terminal=True, light=True,
             others=True),
        Tool("tg_leave", "Выйти из группы (по умолчанию из текущей). Только по просьбе Вана.", {"chat": S}, [], tg_leave,
             risk=Risk.OWN, light=True),
        Tool("pass_to_owner", "Передать Вану короткую записку: кто пишет и что просит. Для чужих людей.",
             {"note": S}, ["note"], pass_to_owner, others=True, light=True),
    ]
