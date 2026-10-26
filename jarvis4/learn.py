"""Learning from what happens: the words a chat uses, and a nightly look back at the day.

Words are counted by code for free (every message of every chat) and shown back to the model as «словечки этого чата», so
Jarvis talks the way each company talks. The night review is one model call a day: it reads the failures, the owner's
corrections and what people wrote, and comes back with lessons (they apply at once and the owner is told in the morning,
each can be dropped with /unlesson), people's profiles, and a short note. A day with no failures and nothing new costs
nothing: the call is skipped.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time

from . import clock, persona
from .budget import OutOfMoney, Tier
from .llm import LLMError

log = logging.getLogger("jarvis4.learn")

WORD = re.compile(r"[а-яёa-z][а-яёa-z\-]{3,}", re.I)
URL = re.compile(r"https?://\S+|@\w+|\[[^\]]*\]")
STOP = set("""это как что так там тут вот все всё его она они оно или если когда потом тоже даже уже ещё еще только можно надо
нужно будет была были было есть нет для при про над под без через после перед между очень просто нормально можешь могу
хочу хочет хотел точно вообще короче типа вроде значит конечно сейчас сегодня завтра вчера почему потому зачем чтобы
который которая которые этот эта эти того этом этой этих мой моя мои твой твоя твои наш наша наши свой своя свои
себя меня тебя тебе мне нас вас вам нам ним ней них там чего чем кто кому кого какой какая какие какое
the and for are but not you your with that this have from they will would there their what when where which about
just like then than them been were was has had can could should also into some more very much only okay
ahaha haha ahah хах""".split())
DAY_REVIEW_AT = 4  # hour on the owner's clock
MORNING_AT = 9
LOOP_EVERY = 20 * 60
MAX_NEW_LESSONS = 5

SYSTEM = """Ты разбираешь день ИИ-помощника Джарвиса, чтобы завтра он делал меньше ошибок и лучше понимал людей. Тебе дают: \
что у него не вышло (журнал), как Ван его поправлял, уже записанные уроки и сообщения людей, с которыми он говорил.
Верни:
- summary: 1–3 коротких фразы по-русски для Вана, что было не так и чему Джарвис научился. Без воды.
- lessons: до {n} новых правил «в такой ситуации делай так, а так не делай» (text), и когда применять (when, 2–4 слова). Только то, \
что видно из данных, не повторяй записанные. Пусто, если нечему учиться.
- profiles: для людей с достаточным числом сообщений коротко (до 40 слов): как человек пишет, что любит, как с ним лучше \
говорить, о чём шутят. Только по их сообщениям, ничего не выдумывай.
Не придумывай ошибок, которых нет в данных."""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "lessons": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "when": {"type": "string"}}, "required": ["text", "when"],
            "additionalProperties": False}},
        "profiles": {"type": "array", "items": {"type": "object", "properties": {
            "key": {"type": "string"}, "profile": {"type": "string"}}, "required": ["key", "profile"],
            "additionalProperties": False}},
    },
    "required": ["summary", "lessons", "profiles"],
    "additionalProperties": False,
}


def tokens(text: str) -> list[str]:
    """Words worth counting: letters only, 4+ characters, not the filler everybody says."""
    found = WORD.findall(URL.sub(" ", (text or "").lower()))
    return [w.strip("-") for w in found if w.strip("-") not in STOP and len(w.strip("-")) >= 4]


def chat_style(store, chat: str) -> str:
    """What this chat says and sends a lot, for the model to talk the same way."""
    words, stickers = store.top_words(chat, 12), store.top_stickers(chat, 5)
    lines = []
    if words:
        lines.append("Словечки этого чата (их тут часто говорят): " + ", ".join(w for w, _ in words) +
                     ". Вставляй их к месту, как свои, но не в каждой фразе.")
    if stickers:
        lines.append("Стикеры, которые тут кидают чаще всего: " + " ".join(f"{e}×{n}" for e, n in stickers) +
                     ". Если шлёшь стикер, бери похожие по духу.")
    return "\n".join(lines)


class Review:
    def __init__(self, core, alerts):
        self.core, self.alerts, self.store = core, alerts, core.store

    async def loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("review pass failed")
            await asyncio.sleep(LOOP_EVERY)

    async def tick(self, now: dt.datetime | None = None) -> None:
        now = now or clock.now()
        today = now.strftime("%Y-%m-%d")
        if now.hour >= DAY_REVIEW_AT and self.store.meta_get("review_day") != today:
            self.store.meta_set("review_day", today)
            await self.run()
        note = self.store.meta_get("morning_note")
        if note and now.hour >= MORNING_AT and not self.core.frozen():
            self.store.meta_set("morning_note", "")
            await self.alerts.say(note)

    # ---- what the day held --------------------------------------------------------------------------------------

    def gather(self, since: float) -> dict:
        st = self.store
        rows = st.db.execute("SELECT ts, task, tool, args, result FROM journal WHERE ok = 0 AND ts >= ? "
                             "AND tool NOT LIKE 'screen:%' ORDER BY id DESC LIMIT 25", (since,)).fetchall()
        fails = [f"{clock.human(ts)} {tool}({args[:160]}) → {result[:160]}" for ts, _, tool, args, result in rows]
        stopped = st.db.execute("SELECT COUNT(*) FROM tasks WHERE ts >= ? AND status IN ('error', 'stopped')",
                                (since,)).fetchone()[0]
        corrections = []
        for m in st.chat_msgs("owner", limit=200):
            if m.ts >= since and m.sender == "owner" and persona.is_correction(m.text):
                before = [x for x in st.recent("owner", 60) if x.ts < m.ts and x.sender == "jarvis"][-1:]
                corrections.append((before[0].text[:200] + " ← " if before else "") + m.text[:200])
        people = []
        for p in st.people():
            if p.key == "owner" or p.blocked:
                continue
            said = [m.text for m in st.chat_msgs(p.key, limit=40) if m.ts >= since and m.sender == p.key and m.text]
            for chat, _, title, _ in st.chats("group"):
                said += [m.text for m in st.chat_msgs(chat, limit=80) if m.ts >= since and m.sender == p.key and m.text]
            if len(said) >= 6:
                people.append({"key": p.key, "name": p.name, "profile": p.profile, "said": [t[:160] for t in said[-15:]]})
        return {"fails": fails, "stopped": stopped, "corrections": corrections[-15:], "people": people[:8]}

    async def run(self) -> str:
        core, st = self.core, self.store
        if not core.llm.ready:
            return ""
        since = time.time() - 26 * 3600
        data = self.gather(since)
        if not (data["fails"] or data["corrections"] or data["people"]):
            return ""  # nothing to learn from: no call, no cost
        lessons = [t for _, t, _ in st.lessons()]
        prompt = (f"Не вышло за сутки ({len(data['fails'])}, прерванных задач {data['stopped']}):\n" + "\n".join(data["fails"])
                  + "\n\nКак Ван поправлял Джарвиса:\n" + "\n".join(data["corrections"])
                  + "\n\nУже записанные уроки:\n" + "\n".join(f"- {t}" for t in lessons)
                  + "\n\nЛюди и их сообщения:\n" + json.dumps(data["people"], ensure_ascii=False))
        try:
            model = core.budget.pick(Tier.BRAIN, owner=True)
            reply = await core.llm.complete(model, SYSTEM.format(n=MAX_NEW_LESSONS), [{"role": "user", "content": prompt}],
                                            max_tokens=2500, effort="medium", kind="review", schema=SCHEMA)
            verdict = json.loads(reply.text)
        except (OutOfMoney, LLMError, ValueError) as e:
            log.warning("night review skipped: %s", e)
            return ""
        added = []
        known = [set(_words(t)) for t in lessons]
        for item in verdict.get("lessons", [])[:MAX_NEW_LESSONS]:
            text = str(item.get("text", "")).strip()
            mine = set(_words(text))
            if len(text) < 20 or any(mine and len(mine & old) / len(mine) > 0.6 for old in known):
                continue
            added.append((st.add_lesson(text, str(item.get("when", "")), source="ночной разбор"), text))
            known.append(mine)
        keys = {p["key"] for p in data["people"]}
        for item in verdict.get("profiles", [])[:8]:
            if item.get("key") in keys and str(item.get("profile", "")).strip():
                st.set_profile(item["key"], str(item["profile"]).strip()[:400])
        summary = str(verdict.get("summary", "")).strip()
        if summary or added:
            note = "🌙 " + (summary or "разобрал вчерашний день")
            if added:
                note += "\nвыучил:\n" + "\n".join(f"#{i} {t}" for i, t in added) + "\nкакой-то урок не нужен — /unlesson номер"
            st.meta_set("morning_note", note)
        log.info("night review: %d lessons, %d profiles, $%.3f", len(added), len(verdict.get("profiles", [])), reply.cost)
        return st.meta_get("morning_note")


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[а-яёa-z]{4,}", text.lower())]
