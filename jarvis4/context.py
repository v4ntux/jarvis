"""What the model sees: the prompt (who he is, what he knows, what he learned) and the conversation, with every
message named by a ref and every reply, forward and relayed note spelled out. This is where «скинь этот стикер»
stops being a guess: the model reads which sticker is meant.
"""
from __future__ import annotations

import re
import time

from . import clock, learn, names, persona
from .store import Msg, Store
from .types import Interaction, Mode

KIND_RU = {"sticker": "стикер", "gif": "гифка", "photo": "фото", "video": "видео", "voice": "голосовое",
           "circle": "кружок", "file": "файл"}
MAX_LESSONS = 8
LESSON_SHOWN_ALWAYS = 4  # the newest lessons are always there; the rest only when the words overlap


def media_label(m: Msg) -> str:
    if not m.media:
        return ""
    what = KIND_RU.get(m.media.get("kind"), "вложение")
    detail = m.media.get("emoji") or m.media.get("about") or ""
    return f"[{what}{' ' + detail if detail else ''}]"


def brief(core, m: Msg, width: int = 90) -> str:
    text = " ".join(x for x in (media_label(m), m.text.replace("\n", " ")) if x)
    return f"{core.name_of(m.sender)}: {text[:width]}"


def render_msg(core, m: Msg, with_ref: bool = True) -> str:
    """One message as the model reads it: [ref] text, with what it answers and what it stands for."""
    parts = [x for x in (media_label(m), m.text) if x]
    line = ("[" + m.ref + "] " if with_ref else "") + " ".join(parts)
    extra = []
    if m.reply_ref:
        target = core.store.msg(m.reply_ref)
        extra.append(f"в ответ на [{m.reply_ref}] «{brief(core, target)}»" if target else f"в ответ на [{m.reply_ref}]")
    if m.forward_ref:
        target = core.store.msg(m.forward_ref)
        extra.append(f"пересланное из [{m.forward_ref}]" + (f" «{brief(core, target)}»" if target else ""))
    if m.relay:
        source = m.relay.get("chat", "")
        stuff = []
        for ref in m.relay.get("msgs") or []:
            src = core.store.msg(ref)
            stuff.append(f"[{ref}] {media_label(src) or 'текст'}" if src else f"[{ref}]")
        extra.append(f"записка передаёт сообщения из чата «{core.name_of(source)}»: " + "; ".join(stuff))
    if extra:
        line += "\n(" + "; ".join(extra) + ")"
    return line


def actions_of(core, task: str) -> str:
    """What was really done for an answer: tools with ok/failed, from the journal."""
    rows = core.store.journal(12, task=task)
    if not rows:
        return ""
    return "[действия: " + ", ".join(f"{r['tool']}{'' if r['ok'] else ' ✗'}" for r in rows) + "]"


def history(core, itx: Interaction) -> list[dict]:
    """The chat as alternating turns: what others said (user) and what Jarvis said (assistant)."""
    msgs = core.store.recent(itx.chat, core.cfg.history)
    turns: list[tuple[str, str]] = []
    for m in msgs:
        mine = m.sender == "jarvis"
        text = render_msg(core, m, with_ref=not mine or bool(m.relay))
        if not mine and itx.kind == "group":
            text = f"{core.name_of(m.sender)}: {text}"
        if mine and m.task:
            done = actions_of(core, m.task)
            if done:
                text += "\n" + done
        role = "assistant" if mine else "user"
        if mine and not turns:  # the API wants the talk to open with a person: his own opening note becomes context
            role, text = "user", "(раньше ты сам написал) " + text
        if turns and turns[-1][0] == role:
            turns[-1] = (role, turns[-1][1] + "\n" + text)
        else:
            turns.append((role, text))
    return [{"role": r, "content": t} for r, t in turns]


def tokens(text: str) -> set[str]:
    return {w for w in names.words(text) if len(w) >= 3}


def pick_lessons(store: Store, text: str) -> list[tuple[int, str]]:
    rows = store.lessons()
    if not rows:
        return []
    now = tokens(text)
    scored = []
    for i, (lid, lesson, trigger) in enumerate(rows):
        overlap = len(now & tokens(lesson + " " + trigger))
        recent = i >= len(rows) - LESSON_SHOWN_ALWAYS
        if overlap or recent:
            scored.append((overlap * 10 + i, lid, lesson))
    scored.sort(reverse=True)
    return [(lid, lesson) for _, lid, lesson in scored[:MAX_LESSONS]]


def system(core, itx: Interaction, role: str, goal: str = "") -> list[dict]:
    """Two blocks: the steady part (cached between calls) and the live part (time, laptop, money, recent actions)."""
    st, cfg = core.store, core.cfg
    parts = [persona.IDENTITY]
    if itx.owner:
        parts += [persona.TALK, persona.WORK]
    else:
        person = st.person(itx.sender)
        who = person.label if person else "незнакомец"
        profile = f"Что ты знаешь об этом человеке: {person.profile}" if person and person.profile else ""
        parts += [persona.TALK, persona.OTHERS.format(who=who, profile=profile)]
    if itx.kind == "group":
        title = (st.chat(itx.chat) or ("", "группа", ""))[1]
        how = persona.GROUP_SELF if itx.spontaneous else persona.GROUP_CALLED
        parts.append(persona.GROUP.format(title=title, how=how))
    style = learn.chat_style(st, itx.chat)  # what this company says and sends, learned from the chat itself
    if style:
        parts.append(style)
    if itx.owner:
        parts.append(persona.HOME)
        facts = st.facts()
        if facts:
            parts.append("Что ты знаешь о Ване:\n" + "\n".join(f"#{i}: {t}" for i, t in facts))
        lessons = pick_lessons(st, itx.text)
        if lessons:
            for lid, _ in lessons:
                st.lesson_used(lid)
            parts.append("Уроки (Ван тебя этому научил, нарушать нельзя):\n" + "\n".join(f"- {t}" for _, t in lessons))
        skills = core.skills_text(itx.text) if hasattr(core, "skills_text") else ""
        if skills:
            parts.append(skills)
    steady = "\n\n".join(parts)

    clock_now = clock.now()
    offset = clock_now.strftime("%z")
    live = [f"Сейчас {clock_now:%Y-%m-%d %H:%M, %A} по времени Вана (UTC{offset[:3]}:{offset[3:]})."]
    if itx.owner:
        lap = core.platform.laptop
        live.append("Ноутбук на связи." if lap is not None and lap.online else "Ноутбук сейчас НЕ подключён: его инструменты не работают.")
        live.append(f"Потрачено сегодня ${core.budget.spent_today():.2f} из ${cfg.daily_limit:.2f}.")
        recent = actions_lines(core)
        if recent:
            live.append("Твои последние действия (для вопросов «что ты делал», точные тексты через инструмент journal):\n"
                        + recent)
    live.append(persona.MODES[itx.mode])  # last in the prompt: the mood must not be lost among the rules
    if itx.note:
        live.append(itx.note)
    if goal:
        live.append(f"ЗАДАЧА (передана тебе, выполни её; слова Вана — в переписке выше): {goal}")
    return [{"type": "text", "text": steady, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "\n".join(live)}]


def actions_lines(core) -> str:
    rows = core.store.journal(core.cfg.journal_lines)
    lines = []
    for r in rows:
        args = ", ".join(f"{k}={str(v)[:300]}" for k, v in r["args"].items())
        lines.append(f"- {clock.human(r['ts'])} {r['tool']}({args[:420]}) → {'ок' if r['ok'] else 'ошибка'}: "
                     f"{r['result'][:120].replace(chr(10), ' ')}")
    return "\n".join(lines)


def messages(core, itx: Interaction, goal: str = "") -> list[dict]:
    """The conversation for the model. For events with no message of their own, the event is the last turn."""
    if itx.kind == "group":
        return group_messages(core, itx)
    turns = history(core, itx)
    last_user = bool(turns) and turns[-1]["role"] == "user"
    if not last_user:
        turns.append({"role": "user", "content": itx.text or "(событие)"})
    if itx.images:
        last = turns[-1]
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": _media_type(b), "data": b}}
                  for b in itx.images]
        turns[-1] = {"role": "user", "content": blocks + [{"type": "text", "text": last["content"]}]}
    return turns


def group_messages(core, itx: Interaction) -> list[dict]:
    """A group is one transcript, newest at the bottom, and a question about what to do with it."""
    title = (core.store.chat(itx.chat) or ("", "группа", ""))[1]
    lines = []
    for m in core.store.recent(itx.chat, core.cfg.history):
        who = "Джарвис (ты)" if m.sender == "jarvis" else core.name_of(m.sender)
        lines.append(f"{who}: {render_msg(core, m)}")
    tail = "Ответь на последнее сообщение." if not itx.spontaneous else "Скажи что-то, только если есть что сказать."
    return [{"role": "user", "content": f"Группа «{title}». Переписка (внизу самое свежее):\n" + "\n".join(lines)
             + f"\n\n{tail}"}]


def _media_type(b64: str) -> str:
    return {"iVBOR": "image/png", "UklGR": "image/webp", "R0lGO": "image/gif"}.get(b64[:5], "image/jpeg")
