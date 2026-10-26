"""Brings what the old bot knew into Jarvis 4: facts, pending reminders, groups, the people he talks with, today's spend
(so the daily limit counts what was already spent), watchers, slang. Reads the old database read-only; safe to run twice.

    python -m jarvis4.migrate [/opt/jarvis/data/jarvis.db]
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys

from . import clock, config
from .store import Store

log = logging.getLogger("jarvis4.migrate")
DONE = "migrated_from_legacy"


def _rows(old: sqlite3.Connection, sql: str, *args) -> list[tuple]:
    try:
        return old.execute(sql, args).fetchall()
    except sqlite3.OperationalError:  # the old bot had no such table (yet)
        return []


def migrate(store: Store, old_path: str, force: bool = False) -> dict[str, int]:
    if store.meta_get(DONE) and not force:
        return {}
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    counts: dict[str, int] = {}
    known = {text for _, text in store.facts()}
    for _, text in _rows(old, "SELECT id, text FROM facts ORDER BY id"):
        if text not in known:
            store.remember(text)
            counts["facts"] = counts.get("facts", 0) + 1
    for due, text, call in _rows(old, "SELECT due, text, COALESCE(call, 0) FROM reminders WHERE done = 0"):
        store.add_reminder(due, text, bool(call))
        counts["reminders"] = counts.get("reminders", 0) + 1
    for chat, title, mode in _rows(old, "SELECT chat, title, mode FROM groups"):
        key = f"g{abs(chat)}"
        store.chat_seen(key, "group", title or "группа", chat)
        store.set_chat_mode(key, mode or "on")
        counts["groups"] = counts.get("groups", 0) + 1
    names = {}
    for peer, name in _rows(old, "SELECT peer, MAX(name) FROM peers GROUP BY peer"):
        names[peer] = name or f"u{peer}"
    blocked = {peer for (peer,) in _rows(old, "SELECT peer FROM spam WHERE blocked = 1")}
    for peer in set(names) | blocked:
        store.upsert_person(f"u{peer}", names.get(peer, f"u{peer}"), tg_id=peer, dialog=True, blocked=peer in blocked)
        counts["people"] = counts.get("people", 0) + 1
    day = clock.day_start()
    for ts, model, tin, tout, cost in _rows(old, "SELECT ts, model, tokens_in, tokens_out, cost FROM usage WHERE ts >= ?",
                                            day):
        store.db.execute("INSERT INTO usage (ts, model, tokens_in, tokens_out, cost, kind) VALUES (?, ?, ?, ?, ?, 'legacy')",
                         (ts, model, tin, tout, cost))
        counts["spend_rows"] = counts.get("spend_rows", 0) + 1
    for ts, kind, cost in _rows(old, "SELECT ts, kind, cost FROM phone WHERE ts >= ?", day):
        store.db.execute("INSERT INTO phone (ts, kind, cost) VALUES (?, ?, ?)", (ts, kind, cost))
    for _, raw, state, due in _rows(old, "SELECT id, spec, state, due FROM watchers"):
        spec = json.loads(raw)
        spec.setdefault("auto", False)  # what the old bot did by itself is only proposed now
        spec["by"] = "legacy"
        store.add_watcher(spec, due or 0)
        counts["watchers"] = counts.get("watchers", 0) + 1
    slang = _rows(old, "SELECT word, meaning FROM slang")
    if slang:
        store.meta_set("slang_legacy", json.dumps(dict(slang), ensure_ascii=False))
        counts["slang"] = len(slang)
    store.db.commit()
    store.meta_set(DONE, "yes")
    old.close()
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    cfg = config.load()
    old_path = sys.argv[1] if len(sys.argv) > 1 else "/opt/jarvis/data/jarvis.db"
    store = Store(cfg.db_path)
    counts = migrate(store, old_path)
    print("уже переносил раньше" if not counts and store.meta_get(DONE) else "перенёс: " + (
        ", ".join(f"{k} {v}" for k, v in counts.items()) or "нечего переносить"))


if __name__ == "__main__":
    main()
