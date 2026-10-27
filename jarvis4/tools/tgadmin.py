"""The rest of the Telegram account as tools: join, block, ignore, make channels and groups, post, delete them, run a
group (kick, ban, mute, admins, pins, title, link) and change the profile.

The owner may ask for anything; deleting a channel or group is the one red-list action (asked once). In a group, its
own Telegram admins may ask Jarvis to moderate (kick, ban, mute, pin, delete a message), never against the owner.
"""
from __future__ import annotations

import os

from .. import guard, names
from ..ports import TGError
from ..types import Result, fail
from .base import I, S, Ctx, Risk, Tool
from .telegram import FILE_ROOTS, _file, authorized, resolve

ACTIONS = ("kick", "ban", "unban", "mute", "unmute", "admin", "unadmin", "invite", "pin", "unpin", "delete", "title",
           "about", "link")
ON_PERSON = {"kick", "ban", "unban", "mute", "unmute", "admin", "unadmin", "invite"}
ON_MESSAGE = {"pin", "unpin", "delete"}
FOR_ADMINS = {"kick", "ban", "unban", "mute", "unmute", "pin", "unpin", "delete"}  # what a group's own admins may ask
DONE = {"kick": "выкинул", "ban": "забанил", "unban": "разбанил", "mute": "замьютил", "unmute": "размьютил",
        "admin": "сделал админом", "unadmin": "снял админку", "invite": "добавил", "pin": "закрепил",
        "unpin": "открепил", "delete": "удалил сообщение", "title": "поменял название", "about": "поменял описание"}


def find_chat(ctx: Ctx, name: str) -> tuple[str, str, str] | str:
    """(key, kind, title) of a group or channel Jarvis is in, by key, title or a piece of it; or the error."""
    q = str(name or "").strip()
    rows = [r for r in ctx.store.chats() if r[1] in ("group", "channel")]
    if not q:
        return "Ошибка: не сказано, какой чат."
    exact = [r for r in rows if q in (r[0], r[2])]
    found = exact or [r for r in rows if names.matches(q, [r[2]]) or q.lower() in r[2].lower()]
    if len(found) == 1:
        return found[0][0], found[0][1], found[0][2]
    if found:
        return "Ошибка: подходят несколько: " + "; ".join(f"«{r[2]}»" for r in found[:8]) + ". Уточни."
    return f"Ошибка: я не состою в «{q}». Вступить — tg_join со ссылкой."


async def tg_join(ctx: Ctx, a: dict) -> Result:
    try:
        key, title, kind = await ctx.pf.tg.join(str(a.get("link") or ""))
    except TGError as e:
        return fail(str(e))
    return Result(True, f"Вступил в {'канал' if kind == 'channel' else 'группу'} «{title}».", evidence=key)


async def _person(ctx: Ctx, who: str):
    person, error = await resolve(ctx, who)
    if person is None:
        return None, error
    if person.key == "owner":
        return None, "Ошибка: это Ван."
    return person, ""


async def tg_block(ctx: Ctx, a: dict) -> Result:
    person, error = await _person(ctx, a.get("who", ""))
    if person is None:
        return fail(error)
    on = not a.get("off")
    try:
        await ctx.pf.tg.block(person.key, on)
    except TGError as e:
        return fail(str(e))
    ctx.store.set_ignored(person.key, on)
    return Result(True, f"{'Заблокировал' if on else 'Разблокировал'} {person.label}.", evidence=person.key)


async def tg_ignore(ctx: Ctx, a: dict) -> Result:
    person, error = await _person(ctx, a.get("who", ""))
    if person is None:
        return fail(error)
    on = not a.get("off")
    ctx.store.set_ignored(person.key, on)
    return Result(True, f"{person.label}: " + ("игнорю (сообщения читаю, не отвечаю)." if on else "снова отвечаю."),
                  evidence=person.key)


async def tg_create(ctx: Ctx, a: dict) -> Result:
    title = str(a.get("title") or "").strip()
    if not title:
        return fail("нужно название.")
    members = []
    for who in a.get("members") or []:
        person, error = await _person(ctx, who)
        if person is None:
            return fail(error)
        members.append(person.key)
    channel = a.get("kind") != "group"
    try:
        key, link = await ctx.pf.tg.create_chat(title, str(a.get("about") or ""), channel, members,
                                                str(a.get("username") or ""))
    except TGError as e:
        return fail(str(e))
    return Result(True, f"Создал {'канал' if channel else 'группу'} «{title}»: {link}", evidence=key)


async def tg_post(ctx: Ctx, a: dict) -> Result:
    got = find_chat(ctx, a.get("chat", ""))
    if isinstance(got, str):
        return fail(got)
    key, kind, title = got
    text = str(a.get("text") or "").strip()
    files = []
    if a.get("file"):
        item = _file(str(a["file"]))
        if isinstance(item, str):
            return fail(item)
        files.append(item)
    if not text and not files:
        return fail("нечего публиковать.")
    try:
        ref = await ctx.pf.tg.send(key, text, files)
    except TGError as e:
        return fail(str(e))
    ctx.store.add_msg(ref, key, "jarvis", text, {"kind": "file", "about": files[0][0]} if files else None, task=ctx.itx.id)
    return Result(True, f"Опубликовал в «{title}».", evidence=f"сообщение {ref}")


async def tg_delete_chat(ctx: Ctx, a: dict) -> Result:
    got = find_chat(ctx, a.get("chat", ""))
    if isinstance(got, str):
        return fail(got)
    key, kind, title = got
    try:
        await ctx.pf.tg.delete_chat(key)
    except TGError as e:
        return fail(str(e))
    ctx.store.set_chat_mode(key, "off")
    return Result(True, f"Удалил {'канал' if kind == 'channel' else 'группу'} «{title}».", evidence=key)


async def _target(ctx: Ctx, a: dict) -> tuple[str, str, int, str]:
    """(chat key, person key, message id, error) for tg_admin: the current group by default, the person of the message
    replied to when nobody is named."""
    chat = ctx.itx.chat if not a.get("chat") and ctx.itx.kind == "group" else None
    if chat is None:
        got = find_chat(ctx, a.get("chat", ""))
        if isinstance(got, str):
            return "", "", 0, got
        chat = got[0]
    action = a.get("action")
    ref = str(a.get("ref") or "").strip("[]@ ")
    if not ref and ctx.itx.msgs and ctx.itx.chat == chat:
        ref = ctx.itx.msgs[-1].reply_ref
    msg = ctx.store.msg(ref) if ref else None
    who = ""
    if action in ON_PERSON:
        if a.get("who"):
            person, error = await resolve(ctx, a["who"])
            if person is None:
                return "", "", 0, error
            who = person.key
        elif msg is not None and msg.sender not in ("jarvis",):
            who = msg.sender
        else:
            return "", "", 0, "Ошибка: кого? Назови человека (who) или ответь на его сообщение."
    msg_id = 0
    if action in ON_MESSAGE and action != "unpin" or (action == "unpin" and ref):
        if msg is None or msg.chat != chat:
            return "", "", 0, "Ошибка: какое сообщение? Нужна ссылка ref из этого чата."
        msg_id = int(msg.ref.rpartition("/")[2])
    return chat, who, msg_id, ""


async def tg_admin(ctx: Ctx, a: dict) -> Result:
    action = str(a.get("action") or "")
    if action not in ACTIONS:
        return fail(f"действие одно из: {', '.join(ACTIONS)}.")
    chat, who, msg_id, error = await _target(ctx, a)
    if error:
        return fail(error)
    if who == "owner":
        return fail("против Вана не пойду.")
    try:
        said = await ctx.pf.tg.moderate(chat, action, who, msg_id, str(a.get("text") or ""), int(a.get("minutes") or 0))
    except TGError as e:
        return fail(str(e))
    title = (ctx.store.chat(chat) or ("", "чат", ""))[1]
    whom = f" {ctx.core.name_of(who)}" if who else ""
    if action == "link":
        return Result(True, f"Ссылка-приглашение в «{title}»: {said}", evidence=said)
    return Result(True, f"«{title}»: {DONE.get(action, action)}{whom}.", evidence=f"{action} {who} {msg_id}".strip())


async def tg_profile(ctx: Ctx, a: dict) -> Result:
    photo = str(a.get("photo") or "")
    if photo:
        path = os.path.realpath(os.path.expanduser(photo))
        if not any(path.startswith(os.path.realpath(r) + os.sep) for r in FILE_ROOTS):
            return fail("аватарку беру только из inbox или work.")
        photo = path
    try:
        said = await ctx.pf.tg.set_profile(str(a.get("first_name") or ""), a.get("last_name"), a.get("bio"),
                                           str(a.get("username") or ""), photo)
    except TGError as e:
        return fail(str(e))
    return Result(True, said.capitalize() + ".")


# ---- guards ----------------------------------------------------------------------------------------------------------

async def _guard_create(ctx: Ctx, a: dict):
    for who in a.get("members") or []:
        person, _ = await resolve(ctx, who)
        if person is not None and person.key != "owner" and not authorized(ctx, person):
            return guard.ask(f"Добавить {person.label} в новый чат? Этого человека ты не называл.", f"reach:{person.key}")
    return None


async def _guard_delete(ctx: Ctx, a: dict):
    got = find_chat(ctx, a.get("chat", ""))
    if isinstance(got, str):
        return None  # the tool reports it
    kind = "канал" if got[1] == "channel" else "группу"
    return guard.ask(f"Удалить {kind} «{got[2]}» насовсем, для всех? Вернуть не получится.", f"delete-chat:{got[0]}")


async def _guard_admin(ctx: Ctx, a: dict):
    if ctx.itx.owner:
        return None
    action = str(a.get("action") or "")
    if ctx.itx.kind != "group" or a.get("chat") and a.get("chat") != ctx.itx.chat:
        return guard.deny("группой управляю только по просьбе Вана или админов этой группы")
    if action not in FOR_ADMINS:
        return guard.deny("это может попросить только Ван")
    if not await ctx.pf.tg.is_admin(ctx.itx.chat, ctx.itx.sender):
        return guard.deny("модерировать прошу только админов группы")
    return None


def tools() -> list[Tool]:
    who = {"who": S, "off": {"type": "boolean"}}
    return [
        Tool("tg_join", "Вступить в группу или канал по ссылке t.me/…, приглашению t.me/+… или @имени.", {"link": S},
             ["link"], tg_join, risk=Risk.OWN, light=True, label=lambda a, r: f"вступаю {a.get('link', '')}"),
        Tool("tg_block", "Заблокировать человека в Telegram (off=true — разблокировать). Заблокированному я и не "
             "отвечаю.", who, ["who"], tg_block, risk=Risk.OWN, light=True,
             label=lambda a, r: f"блокирую {a.get('who', '')}"),
        Tool("tg_ignore", "Игнорить человека: его сообщения сохраняются, но я на них не отвечаю ни в личке, ни в группах "
             "(off=true — снова отвечать). Мягче блокировки.", who, ["who"], tg_ignore, risk=Risk.OWN, light=True),
        Tool("tg_create", "Создать канал (kind=channel) или группу (kind=group): название, описание, участники "
             "(members, только те, кого Ван назвал), публичный @username по желанию. Вернёт ссылку.",
             {"kind": {"type": "string", "enum": ["channel", "group"]}, "title": S, "about": S,
              "members": {"type": "array", "items": S}, "username": S}, ["title"], tg_create, risk=Risk.OWN,
             guard=_guard_create, label=lambda a, r: f"создаю «{a.get('title', '')}»"),
        Tool("tg_post", "Опубликовать пост (текст и/или файл из inbox/work) в канале или группе, где я состою.",
             {"chat": S, "text": S, "file": S}, ["chat"], tg_post, risk=Risk.OUT, light=True,
             label=lambda a, r: f"пощу в {a.get('chat', '')}"),
        Tool("tg_delete_chat", "Удалить канал или группу насовсем для всех (только созданные мной; иначе просто выйду). "
             "Охрана спросит Вана.", {"chat": S}, ["chat"], tg_delete_chat, risk=Risk.RED, guard=_guard_delete,
             label=lambda a, r: f"удаляю {a.get('chat', '')}"),
        Tool("tg_admin", "Управлять группой или каналом, где я админ. action: kick выкинуть · ban (minutes — на время) · "
             "unban · mute (minutes) · unmute · admin / unadmin · invite добавить человека · pin / unpin / delete "
             "сообщение по ref · title / about новый текст (text) · link ссылка-приглашение. who — человек; не назван — "
             "автор сообщения, на которое ответили. chat — по умолчанию текущая группа. Админы группы тоже могут "
             "просить модерацию (kick, ban, mute, pin, delete).",
             {"chat": S, "action": {"type": "string", "enum": list(ACTIONS)}, "who": S, "ref": S, "text": S,
              "minutes": I}, ["action"], tg_admin, risk=Risk.OWN, light=True, others=True, guard=_guard_admin,
             label=lambda a, r: f"{a.get('action', '')} {a.get('who', '')}".strip()),
        Tool("tg_profile", "Поменять свой профиль в Telegram: first_name, last_name, bio, username, photo (картинка из "
             "inbox/work на аватарку). Пустое не трогается.",
             {"first_name": S, "last_name": S, "bio": S, "username": S, "photo": S}, [], tg_profile, risk=Risk.OWN,
             light=True, label=lambda a, r: "меняю профиль"),
    ]
