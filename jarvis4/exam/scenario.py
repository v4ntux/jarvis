"""Exam scenarios: what happened before, what comes in, and what must and must not happen.

A scenario is a YAML file in scenarios/. It is written in terms of the pretend world (people, chats, windows,
effects), never in terms of one agent's tools, so the old bot and the new one sit the same exam.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from .world import OWNER, World

HERE = os.path.join(os.path.dirname(__file__), "scenarios")

TOP_KEYS = {"id", "title", "source", "tags", "world", "events", "expect", "confirms", "timeout", "reference", "skip"}
WORLD_KEYS = {"people", "groups", "messages", "past_actions", "facts", "laptop", "server", "web", "watchers"}
EVENT_KINDS = {"message", "laptop_online", "new_session"}
EVENT_KEYS = {"kind", "chat", "from", "text", "reply_to", "media", "key", "during", "note"}
DURING_KEYS = {"tool_calls", "replies"}
EXPECT_KEYS = {"effects", "no_effects", "final", "confirms", "replies", "judge", "honest", "allow", "cost_max"}
REPLY_KEYS = {"chat", "max_words", "max_words_each", "max_messages", "min_messages", "must_match", "must_not_match", "no_media",
              "numbers_from", "since_event"}
FINAL_KEYS = {"windows_include", "windows_exclude", "audio_playing", "watchers_count", "server_files_new"}
REFERENCE_OPS = {"say", "send", "forward", "call", "leave", "react", "sticker", "gif", "open_app", "close", "click",
                 "laptop_shell", "server_shell", "monitor", "remember", "lesson", "remove_watcher", "media", "type",
                 "clipboard"}


class ScenarioError(Exception):
    pass


@dataclass
class Event:
    kind: str = "message"
    chat: str = OWNER  # where the message is written
    sender: str = OWNER
    text: str = ""
    reply_to: str | None = None  # key of a world message
    media: dict | None = None
    key: str = ""  # the key this message gets in the world, so later events can reply to it
    during: dict | None = None  # deliver mid-task: {"tool_calls": n} or {"replies": n} after the previous event
    note: str = ""

    def describe(self, world: World) -> str:
        if self.kind == "laptop_online":
            return "[событие] ноутбук вышел на связь"
        if self.kind == "new_session":
            return "[событие] новый день: история разговора очищена, память осталась"
        where = "" if self.chat == OWNER else f" в «{world.name_of(self.chat)}»"
        answer = ""
        if self.reply_to:
            replied = world.message(self.reply_to)
            answer = f" (ответ на «{(replied.text or replied.media or '')!s:.80}»)"
        media = f" [{self.media.get('kind')} {self.media.get('emoji', '')}]" if self.media else ""
        return f"{world.name_of(self.sender)}{where}{answer}:{media} {self.text}"


@dataclass
class Scenario:
    id: str
    title: str
    path: str
    source: str = ""
    tags: list[str] = field(default_factory=list)
    world: dict = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)
    expect: dict = field(default_factory=dict)
    confirms: list[bool] = field(default_factory=list)  # the owner's answers to yes/no questions, in order
    timeout: float = 240.0  # seconds per event for a real agent
    reference: list[list[dict]] = field(default_factory=list)  # a known good run: ops per event
    skip: dict = field(default_factory=dict)  # agent name → why it cannot sit this scenario

    def build_world(self) -> World:
        return World(self.world, list(self.confirms))


def _check_keys(where: str, raw: dict, allowed: set[str]) -> None:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: ожидался словарь")
    unknown = set(raw) - allowed
    if unknown:
        raise ScenarioError(f"{where}: неизвестные ключи {sorted(unknown)}")


def load(path: str) -> Scenario:
    name = os.path.basename(path)
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    _check_keys(name, raw, TOP_KEYS)
    for need in ("id", "title", "events", "expect"):
        if need not in raw:
            raise ScenarioError(f"{name}: нет {need}")
    _check_keys(f"{name} world", raw.get("world") or {}, WORLD_KEYS)
    _check_keys(f"{name} expect", raw["expect"], EXPECT_KEYS)
    events = []
    for i, ev in enumerate(raw["events"]):
        _check_keys(f"{name} events[{i}]", ev, EVENT_KEYS)
        kind = ev.get("kind", "message")
        if kind not in EVENT_KINDS:
            raise ScenarioError(f"{name} events[{i}]: неизвестный kind {kind}")
        if ev.get("during"):
            _check_keys(f"{name} events[{i}].during", ev["during"], DURING_KEYS)
        events.append(Event(kind=kind, chat=ev.get("chat", OWNER), sender=ev.get("from", OWNER),
                            text=str(ev.get("text", "")), reply_to=ev.get("reply_to"), media=ev.get("media"),
                            key=ev.get("key", ""), during=ev.get("during"), note=ev.get("note", "")))
    replies = raw["expect"].get("replies") or []
    for i, r in enumerate(replies if isinstance(replies, list) else [replies]):
        _check_keys(f"{name} expect.replies[{i}]", r, REPLY_KEYS)
    _check_keys(f"{name} expect.final", raw["expect"].get("final") or {}, FINAL_KEYS)
    reference = raw.get("reference") or []
    if reference and len(reference) != len(events):
        raise ScenarioError(f"{name}: reference должен иметь по списку действий на каждое событие ({len(events)})")
    for i, ops in enumerate(reference):
        for op in ops or []:
            if not isinstance(op, dict) or op.get("op") not in REFERENCE_OPS:
                raise ScenarioError(f"{name} reference[{i}]: неизвестное действие {op!r}")
    scenario = Scenario(id=str(raw["id"]), title=str(raw["title"]), path=path, source=str(raw.get("source", "")),
                        tags=list(raw.get("tags") or []), world=raw.get("world") or {}, events=events,
                        expect=raw["expect"], confirms=[bool(c) for c in raw.get("confirms") or []],
                        timeout=float(raw.get("timeout", 240)), reference=[ops or [] for ops in reference],
                        skip=dict(raw.get("skip") or {}))
    _check_world(scenario)
    return scenario


def _check_world(s: Scenario) -> None:
    """Every key a scenario mentions must exist: a typo would otherwise make a check pass or fail for nothing."""
    try:
        world = s.build_world()
    except (TypeError, KeyError, ValueError) as e:
        raise ScenarioError(f"{os.path.basename(s.path)}: мир не собирается: {e}") from e
    chats = set(world.people) | set(world.groups)
    keys = {m.key for m in world.messages} | {e.key for e in s.events if e.key}
    for m in world.messages:
        for ref in [m.reply_to, m.forward_of] + list((m.relay or {}).get("msgs") or []):
            if ref and ref not in keys:
                raise ScenarioError(f"{os.path.basename(s.path)}: сообщение {m.key} ссылается на неизвестное {ref}")
    for i, e in enumerate(s.events):
        if e.kind != "message":
            continue
        if e.chat not in chats:
            raise ScenarioError(f"{os.path.basename(s.path)} events[{i}]: нет чата {e.chat}")
        if e.sender not in world.people:
            raise ScenarioError(f"{os.path.basename(s.path)} events[{i}]: нет человека {e.sender}")
        if e.reply_to and e.reply_to not in keys:
            raise ScenarioError(f"{os.path.basename(s.path)} events[{i}]: reply_to на неизвестное {e.reply_to}")


def load_all(folder: str = HERE, only: str = "") -> list[Scenario]:
    names = sorted(n for n in os.listdir(folder) if n.endswith((".yaml", ".yml")))
    found = [load(os.path.join(folder, n)) for n in names]
    ids = [s.id for s in found]
    if len(ids) != len(set(ids)):
        raise ScenarioError("одинаковые id у сценариев: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    return [s for s in found if not only or only in s.id or only in os.path.basename(s.path)]
