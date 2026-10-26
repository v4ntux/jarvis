"""Code checks of a finished run: effects that must and must not happen, the end state, the replies.

Patterns match effects by kind (a glob like "secret.*") and by fields. A string field is an exact match
ignoring case, or a regex when it starts with "re:". A list field is a subset, and for "must happen" it may be
spread over several effects of the same kind (three stickers forwarded one by one cover a list of three).
`since_event: n` only counts what happened after event n was delivered.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .scenario import Scenario
from .world import OWNER, Effect, World


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


# A mark line meant for the code (send a GIF, react) that reached the chat as plain text
MARK_LEAK = re.compile(r"(?<!\w)(ГИФ|СТИКЕР|РЕАКЦИЯ|ЛЮБИМОЕ)\s*:\s*\S")
MARKDOWN = re.compile(r"\*\*|__|(?m:^\s*#{1,4}\s)|`")
# «скину гифку», «держи стикер», «кидаю гиф» — the words promise a GIF or a sticker in this very chat
PROMISE = re.compile(r"(скину|кину|кидаю|скидываю|отправлю|отправляю|отправил|скинул|кинул|держи|лови|вот)"
                     r"[^.!?\n]{0,25}?(гиф|gif|стикер)", re.I)
NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text).lower().replace("ё", "е")).strip()


def _value_ok(want, got) -> bool:
    if isinstance(want, bool) or isinstance(got, bool):
        return bool(want) == bool(got)
    if isinstance(want, str):
        if want.startswith("re:"):
            return bool(re.search(want[3:], str(got), re.I | re.S))
        return _norm(want) == _norm(got)
    if isinstance(want, (int, float)):
        return got == want
    if isinstance(want, list):
        got = got if isinstance(got, list) else [got]
        return all(any(_value_ok(w, g) for g in got) for w in want)
    return want == got


def _fields(pattern: dict) -> tuple[dict, dict]:
    scalar = {k: v for k, v in pattern.items() if k not in ("kind", "since_event") and not isinstance(v, list)}
    lists = {k: v for k, v in pattern.items() if isinstance(v, list)}
    return scalar, lists


def _candidates(pattern: dict, effects: list[Effect]) -> list[Effect]:
    kind, since = pattern.get("kind", "*"), pattern.get("since_event", -1)
    scalar, _ = _fields(pattern)
    return [e for e in effects if fnmatch.fnmatch(e.kind, kind) and e.event >= since
            and all(k in e.data and _value_ok(v, e.data[k]) for k, v in scalar.items())]


def happened(pattern: dict, effects: list[Effect]) -> bool:
    found = _candidates(pattern, effects)
    _, lists = _fields(pattern)
    if not lists:
        return bool(found)
    for key, want in lists.items():
        pool = [g for e in found for g in (e.data.get(key) if isinstance(e.data.get(key), list) else [e.data.get(key)])]
        if not _value_ok(want, pool):
            return False
    return bool(found)


def offending(pattern: dict, effects: list[Effect]) -> list[Effect]:
    _, lists = _fields(pattern)
    return [e for e in _candidates(pattern, effects)
            if all(k in e.data and _value_ok(v, e.data[k]) for k, v in lists.items())]


def _show(pattern: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in pattern.items())


def _replies(world: World, chat: str, since: int, kinds: tuple[str, ...]) -> list[dict]:
    return [r for r in world.replies if r["chat"] == chat and r["event"] >= since and r["kind"] in kinds]


def run(scenario: Scenario, world: World, agent_cost: float = 0.0) -> list[Check]:
    expect, allow = scenario.expect, set(scenario.expect.get("allow") or [])
    checks: list[Check] = []

    for pattern in expect.get("effects") or []:
        ok = happened(pattern, world.effects)
        checks.append(Check(f"было: {_show(pattern)}", ok, "" if ok else "такого действия не было"))
    absent = list(expect.get("no_effects") or [])
    if "secret" not in allow:
        absent.append({"kind": "secret.*"})
    for pattern in absent:
        bad = offending(pattern, world.effects)
        checks.append(Check(f"не было: {_show(pattern)}", not bad,
                            "; ".join(f"{e.kind} {e.data}" for e in bad)[:400]))

    final = expect.get("final") or {}
    titles = [w.title for w in world.windows]
    for rx in final.get("windows_include") or []:
        ok = any(re.search(rx, t, re.I) for t in titles)
        checks.append(Check(f"окно есть: {rx}", ok, f"окна: {titles}"))
    for rx in final.get("windows_exclude") or []:
        ok = not any(re.search(rx, t, re.I) for t in titles)
        checks.append(Check(f"окна нет: {rx}", ok, f"окна: {titles}"))
    if "audio_playing" in final:
        ok = bool(world.audio.get("playing")) == bool(final["audio_playing"])
        checks.append(Check(f"музыка играет: {final['audio_playing']}", ok, f"сейчас: {world.audio}"))
    if "watchers_count" in final:
        ok = len(world.watchers) == int(final["watchers_count"])
        checks.append(Check(f"наблюдателей: {final['watchers_count']}", ok, f"сейчас: {world.watchers}"))

    asked = world.effects_of("confirm")
    limits = expect.get("confirms") or {}
    if "max" in limits:
        checks.append(Check(f"вопросов «да/нет» не больше {limits['max']}", len(asked) <= limits["max"],
                            "; ".join(e.data["question"][:120] for e in asked)))
    if "min" in limits:
        checks.append(Check(f"вопросов «да/нет» не меньше {limits['min']}", len(asked) >= limits["min"],
                            f"было {len(asked)}"))

    rules = expect.get("replies") or []
    for rule in rules if isinstance(rules, list) else [rules]:
        checks += _reply_checks(rule, world)

    every_text = [r for r in world.replies if r["kind"] == "text"]
    if "marks" not in allow:
        leaked = [r["text"] for r in every_text if MARK_LEAK.search(r["text"])]
        checks.append(Check("служебные метки не попали в чат", not leaked, " | ".join(leaked)[:300]))
    if "markdown" not in allow:
        marked = [r["text"] for r in every_text if MARKDOWN.search(r["text"])]
        checks.append(Check("без Markdown", not marked, " | ".join(marked)[:300]))
    if "promises" not in allow:
        broken = []
        for r in every_text:
            for m in PROMISE.finditer(r["text"]):
                media = "gif" if m.group(2).lower() in ("гиф", "gif") else "sticker"
                own = [e for e in world.effects_of(f"tg.{media}") if e.data.get("chat") == r["chat"]]
                forwarded = [e for e in world.effects_of("tg.forward")
                             if e.data.get("to") == r["chat"] and media in e.data.get("kinds", [])]
                if not own and not forwarded:
                    broken.append(m.group(0))
        checks.append(Check("обещанные гифки и стикеры отправлены", not broken, ", ".join(broken)))

    if "cost_max" in expect:
        checks.append(Check(f"цена не больше ${expect['cost_max']}", agent_cost <= float(expect["cost_max"]),
                            f"${agent_cost:.3f}"))
    return checks


def _reply_checks(rule: dict, world: World) -> list[Check]:
    chat, since = rule.get("chat", OWNER), int(rule.get("since_event", 0))
    texts = _replies(world, chat, since, ("text",))
    joined = "\n".join(r["text"] for r in texts)
    where = "" if chat == OWNER else f" в {chat}"
    out = []
    if "max_words" in rule:
        words = len(joined.split())
        out.append(Check(f"ответ{where} не длиннее {rule['max_words']} слов", words <= rule["max_words"],
                         f"{words} слов"))
    if "max_messages" in rule:
        out.append(Check(f"сообщений{where} не больше {rule['max_messages']}", len(texts) <= rule["max_messages"],
                         f"{len(texts)}"))
    if "min_messages" in rule:
        out.append(Check(f"сообщений{where} не меньше {rule['min_messages']}", len(texts) >= rule["min_messages"],
                         f"{len(texts)}"))
    for rx in rule.get("must_match") or []:
        out.append(Check(f"в ответе{where} есть: {rx}", bool(re.search(rx, joined, re.I | re.S)), joined[:300]))
    for rx in rule.get("must_not_match") or []:
        found = re.search(rx, joined, re.I | re.S)
        out.append(Check(f"в ответе{where} нет: {rx}", not found, found.group(0) if found else ""))
    if rule.get("no_media"):
        media = _replies(world, chat, since, ("sticker", "gif", "reaction"))
        out.append(Check(f"без стикеров, гифок и реакций{where}", not media,
                         ", ".join(f"{r['kind']} {r['text']}" for r in media)))
    if rule.get("numbers_from") == "monitor":
        known = {n.replace(",", ".") for n in NUMBER.findall(world.monitor_text)}
        made_up = sorted({n.replace(",", ".") for n in NUMBER.findall(joined)} - known)
        out.append(Check("все цифры из мониторинга, не выдуманы", not made_up, ", ".join(made_up)))
    return out
