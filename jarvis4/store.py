"""Everything Jarvis remembers, in one SQLite file: chats, people, what he did, what he learned, what he spent.

Messages carry a ref («m:chat:id» or a scenario's own key): the model names messages by ref in tool calls, and
the platform turns a ref back into the real message. Nothing here knows about Telegram.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from . import names

SCHEMA = """
CREATE TABLE IF NOT EXISTS msgs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE, chat TEXT, sender TEXT, ts REAL, text TEXT,
    media TEXT, reply_ref TEXT, forward_ref TEXT, relay TEXT, task TEXT);
CREATE INDEX IF NOT EXISTS msgs_chat ON msgs (chat, id);
CREATE TABLE IF NOT EXISTS people (
    key TEXT PRIMARY KEY, name TEXT, username TEXT, tg_id INTEGER, aliases TEXT, dialog INTEGER DEFAULT 0,
    blocked INTEGER DEFAULT 0, profile TEXT DEFAULT '', first_seen REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS chats (
    key TEXT PRIMARY KEY, kind TEXT, title TEXT, mode TEXT DEFAULT 'on', tg_id INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY, ts REAL, chat TEXT, kind TEXT, request TEXT, status TEXT, cost REAL DEFAULT 0, answer TEXT);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, task TEXT, tool TEXT, args TEXT, ok INTEGER, result TEXT,
    evidence TEXT, cost REAL, model TEXT);
CREATE INDEX IF NOT EXISTS journal_task ON journal (task);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT, trigger TEXT, source TEXT, active INTEGER DEFAULT 1,
    uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS facts (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, text TEXT);
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL,
    kind TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS watchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, spec TEXT, state TEXT, due REAL);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, due REAL, text TEXT, call INTEGER DEFAULT 0, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS faves (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, doc INTEGER, desc TEXT);
CREATE TABLE IF NOT EXISTS skills (
    name TEXT PRIMARY KEY, description TEXT, status TEXT DEFAULT 'draft', successes INTEGER DEFAULT 0,
    failures INTEGER DEFAULT 0, last_used REAL, body TEXT);
CREATE TABLE IF NOT EXISTS phone (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, cost REAL);
CREATE TABLE IF NOT EXISTS words (chat TEXT, word TEXT, n INTEGER, last REAL, PRIMARY KEY (chat, word));
CREATE TABLE IF NOT EXISTS stickers_seen (
    chat TEXT, doc INTEGER, emoji TEXT, about TEXT, n INTEGER, after TEXT, last REAL, PRIMARY KEY (chat, doc));
CREATE TABLE IF NOT EXISTS slang (
    word TEXT PRIMARY KEY, meaning TEXT DEFAULT '', example TEXT DEFAULT '', source TEXT DEFAULT '', chat TEXT DEFAULT '',
    status TEXT DEFAULT 'ask', asked REAL DEFAULT 0, learned REAL DEFAULT 0, uses INTEGER DEFAULT 0,
    last_used REAL DEFAULT 0);
"""


@dataclass
class Msg:
    ref: str
    chat: str
    sender: str
    ts: float
    text: str = ""
    media: dict | None = None
    reply_ref: str = ""
    forward_ref: str = ""
    relay: dict | None = None
    task: str = ""  # the task whose answer this is: its journal says what was really done


@dataclass
class Person:
    key: str
    name: str
    username: str = ""
    tg_id: int = 0
    aliases: list[str] | None = None
    dialog: bool = False
    blocked: bool = False
    profile: str = ""

    @property
    def label(self) -> str:
        return f"{self.name} (@{self.username})" if self.username else self.name

    def names(self) -> list[str]:
        return [self.name, self.username, *(self.aliases or [])]


def _msg(row) -> Msg:
    return Msg(row[0], row[1], row[2], row[3], row[4] or "", json.loads(row[5]) if row[5] else None, row[6] or "",
               row[7] or "", json.loads(row[8]) if row[8] else None, row[9] or "")


MSG_COLS = "ref, chat, sender, ts, text, media, reply_ref, forward_ref, relay, task"


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- messages ----------------------------------------------------------------------------------------------

    def add_msg(self, ref: str, chat: str, sender: str, text: str = "", media: dict | None = None,
                reply_ref: str = "", forward_ref: str = "", relay: dict | None = None, ts: float | None = None,
                task: str = "") -> Msg:
        ts = time.time() if ts is None else ts
        self.db.execute(
            "INSERT OR REPLACE INTO msgs (ref, chat, sender, ts, text, media, reply_ref, forward_ref, relay, task) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ref, chat, sender, ts, text, json.dumps(media, ensure_ascii=False) if media else "", reply_ref,
             forward_ref, json.dumps(relay, ensure_ascii=False) if relay else "", task))
        self.db.commit()
        return Msg(ref, chat, sender, ts, text, media, reply_ref, forward_ref, relay, task)

    def add_msg_if_new(self, ref: str, *args, **kwargs) -> bool:
        """History brought in from Telegram: never overwrites a message Jarvis already holds (its task, its relay)."""
        if self.msg(ref) is not None:
            return False
        self.add_msg(ref, *args, **kwargs)
        return True

    def set_relay(self, ref: str, relay: dict) -> None:
        """Marks a message as a note that stands for other messages (a stranger's words passed on to the owner)."""
        self.db.execute("UPDATE msgs SET relay = ? WHERE ref = ?", (json.dumps(relay, ensure_ascii=False), ref))
        self.db.commit()

    def msg(self, ref: str) -> Msg | None:
        row = self.db.execute(f"SELECT {MSG_COLS} FROM msgs WHERE ref = ?", (ref,)).fetchone()
        return _msg(row) if row else None

    def recent(self, chat: str, limit: int) -> list[Msg]:
        """The newest `limit` messages of a chat, oldest first, from the chat's last fresh start on."""
        floor = float(self.meta_get(f"fresh:{chat}", "0") or 0)
        rows = self.db.execute(f"SELECT {MSG_COLS} FROM msgs WHERE chat = ? AND ts > ? ORDER BY ts DESC, id DESC LIMIT ?",
                               (chat, floor, limit)).fetchall()
        return [_msg(r) for r in reversed(rows)]

    def chat_msgs(self, chat: str, kind: str = "", limit: int = 50, after_ref: str = "") -> list[Msg]:
        """Messages of a chat (all of them, not just since the fresh start), optionally of one media kind."""
        floor, floor_id = 0.0, 0
        if after_ref:
            row = self.db.execute("SELECT ts, id FROM msgs WHERE ref = ?", (after_ref,)).fetchone()
            floor, floor_id = (row[0], row[1]) if row else (0.0, 0)
        rows = self.db.execute(
            f"SELECT {MSG_COLS} FROM msgs WHERE chat = ? AND (ts > ? OR (ts = ? AND id > ?)) "
            "ORDER BY ts DESC, id DESC LIMIT 500", (chat, floor, floor, floor_id)).fetchall()
        found = [_msg(r) for r in reversed(rows)]
        if kind:
            found = [m for m in found if (m.media or {}).get("kind") == kind]
        return found[-limit:]

    def fresh_start(self, chat: str) -> None:
        """«Новый чат»: the model stops seeing what came before; the rows stay for learning."""
        row = self.db.execute("SELECT COALESCE(MAX(ts), 0) FROM msgs WHERE chat = ?", (chat,)).fetchone()
        self.meta_set(f"fresh:{chat}", repr(row[0]))

    def count_msgs(self, chat: str, sender: str, since: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM msgs WHERE chat = ? AND sender = ? AND ts >= ?",
                               (chat, sender, since)).fetchone()[0]

    # ---- people and chats --------------------------------------------------------------------------------------

    def upsert_person(self, key: str, name: str = "", username: str = "", tg_id: int = 0,
                      aliases: list[str] | None = None, dialog: bool | None = None, blocked: bool | None = None) -> Person:
        now = time.time()
        old = self.person(key)
        if old is None:
            self.db.execute("INSERT INTO people (key, name, username, tg_id, aliases, dialog, blocked, first_seen, "
                            "last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (key, name or key, username, tg_id, json.dumps(aliases or [], ensure_ascii=False),
                             int(bool(dialog)), int(bool(blocked)), now, now))
        else:
            merged = sorted(set((old.aliases or []) + (aliases or [])))
            self.db.execute("UPDATE people SET name = ?, username = ?, tg_id = ?, aliases = ?, dialog = ?, "
                            "blocked = ?, last_seen = ? WHERE key = ?",
                            (name or old.name, username or old.username, tg_id or old.tg_id,
                             json.dumps(merged, ensure_ascii=False), int(old.dialog if dialog is None else dialog),
                             int(old.blocked if blocked is None else blocked), now, key))
        self.db.commit()
        return self.person(key)

    def person(self, key: str) -> Person | None:
        row = self.db.execute("SELECT key, name, username, tg_id, aliases, dialog, blocked, profile FROM people "
                              "WHERE key = ?", (key,)).fetchone()
        return self._person(row) if row else None

    @staticmethod
    def _person(row) -> Person:
        return Person(row[0], row[1], row[2] or "", row[3] or 0, json.loads(row[4] or "[]"), bool(row[5]),
                      bool(row[6]), row[7] or "")

    def people(self) -> list[Person]:
        rows = self.db.execute("SELECT key, name, username, tg_id, aliases, dialog, blocked, profile FROM people "
                               "ORDER BY last_seen DESC").fetchall()
        return [self._person(r) for r in rows]

    def find_people(self, query: str) -> list[Person]:
        """People matching a @username, id, key or name (translit and Russian endings do not matter)."""
        q = query.strip().lstrip("@").lower()
        if not q:
            return []
        everyone = self.people()
        exact = [p for p in everyone if q in (p.key.lower(), p.username.lower(), str(p.tg_id))
                 or q in [a.lower() for a in p.aliases or []]]
        if exact:
            return exact
        return [p for p in everyone if names.matches(query, p.names())]

    def set_profile(self, key: str, profile: str) -> None:
        self.db.execute("UPDATE people SET profile = ? WHERE key = ?", (profile, key))
        self.db.commit()

    def chat_seen(self, key: str, kind: str, title: str, tg_id: int = 0) -> bool:
        """Registers a chat; True when Jarvis had not met it before."""
        cur = self.db.execute("INSERT OR IGNORE INTO chats (key, kind, title, tg_id, ts) VALUES (?, ?, ?, ?, ?)",
                              (key, kind, title, tg_id, time.time()))
        if not cur.rowcount:
            self.db.execute("UPDATE chats SET title = ? WHERE key = ?", (title, key))
        self.db.commit()
        return bool(cur.rowcount)

    def chat_tg_id(self, key: str) -> int:
        row = self.db.execute("SELECT tg_id FROM chats WHERE key = ?", (key,)).fetchone()
        return row[0] if row and row[0] else 0

    def chat(self, key: str) -> tuple[str, str, str] | None:
        """(kind, title, mode) of a chat."""
        return self.db.execute("SELECT kind, title, mode FROM chats WHERE key = ?", (key,)).fetchone()

    def chats(self, kind: str = "") -> list[tuple[str, str, str, str]]:
        sql, args = "SELECT key, kind, title, mode FROM chats", ()
        if kind:
            sql, args = sql + " WHERE kind = ?", (kind,)
        return self.db.execute(sql + " ORDER BY ts", args).fetchall()

    def set_chat_mode(self, key: str, mode: str) -> None:
        self.db.execute("UPDATE chats SET mode = ? WHERE key = ?", (mode, key))
        self.db.commit()

    # ---- tasks and the journal ---------------------------------------------------------------------------------

    def task_start(self, task: str, chat: str, kind: str, request: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO tasks (id, ts, chat, kind, request, status) VALUES (?, ?, ?, ?, ?, 'run')",
                        (task, time.time(), chat, kind, request[:2000]))
        self.db.commit()

    def task_end(self, task: str, status: str, cost: float, answer: str) -> None:
        self.db.execute("UPDATE tasks SET status = ?, cost = ?, answer = ? WHERE id = ?",
                        (status, cost, answer[:4000], task))
        self.db.commit()

    def journal_add(self, task: str, tool: str, args: dict, ok: bool, result: str, evidence: str = "",
                    cost: float = 0.0, model: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO journal (ts, task, tool, args, ok, result, evidence, cost, model) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), task, tool, json.dumps(args, ensure_ascii=False, default=str), int(ok), result[:4000],
             evidence[:1000], cost, model))
        self.db.commit()
        return cur.lastrowid

    def journal(self, limit: int = 12, task: str = "", query: str = "") -> list[dict]:
        sql, args = "SELECT id, ts, task, tool, args, ok, result, evidence FROM journal", []
        where = []
        if task:
            where.append("task = ?")
            args.append(task)
        if query:
            where.append("(tool LIKE ? OR args LIKE ? OR result LIKE ?)")
            args += [f"%{query}%"] * 3
        if where:
            sql += " WHERE " + " AND ".join(where)
        rows = self.db.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [{"id": r[0], "ts": r[1], "task": r[2], "tool": r[3], "args": json.loads(r[4] or "{}"), "ok": bool(r[5]),
                 "result": r[6], "evidence": r[7]} for r in reversed(rows)]

    # ---- facts and lessons -------------------------------------------------------------------------------------

    def remember(self, text: str) -> int:
        cur = self.db.execute("INSERT INTO facts (ts, text) VALUES (?, ?)", (time.time(), text))
        self.db.commit()
        return cur.lastrowid

    def forget(self, fact_id: int) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.db.commit()
        return bool(cur.rowcount)

    def facts(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT id, text FROM facts ORDER BY id").fetchall()

    def add_lesson(self, text: str, trigger: str = "", source: str = "") -> int:
        cur = self.db.execute("INSERT INTO lessons (ts, text, trigger, source) VALUES (?, ?, ?, ?)",
                              (time.time(), text, trigger, source))
        self.db.commit()
        return cur.lastrowid

    def lessons(self, active_only: bool = True) -> list[tuple[int, str, str]]:
        sql = "SELECT id, text, trigger FROM lessons" + (" WHERE active = 1" if active_only else "") + " ORDER BY id"
        return self.db.execute(sql).fetchall()

    def drop_lesson(self, lesson_id: int) -> bool:
        cur = self.db.execute("UPDATE lessons SET active = 0 WHERE id = ?", (lesson_id,))
        self.db.commit()
        return bool(cur.rowcount)

    def lesson_used(self, lesson_id: int) -> None:
        self.db.execute("UPDATE lessons SET uses = uses + 1 WHERE id = ?", (lesson_id,))
        self.db.commit()

    # ---- money -------------------------------------------------------------------------------------------------

    def log_usage(self, model: str, tokens_in: int, tokens_out: int, cost: float, kind: str = "") -> None:
        self.db.execute("INSERT INTO usage (ts, model, tokens_in, tokens_out, cost, kind) VALUES (?, ?, ?, ?, ?, ?)",
                        (time.time(), model, tokens_in, tokens_out, cost, kind))
        self.db.commit()

    def spent_since(self, ts: float, kind: str = "", exclude: str = "") -> float:
        sql, args = "SELECT COALESCE(SUM(cost), 0) FROM usage WHERE ts >= ?", [ts]
        if kind:
            sql, args = sql + " AND kind LIKE ?", args + [kind + "%"]
        if exclude:
            sql, args = sql + " AND kind NOT LIKE ?", args + [exclude + "%"]
        return self.db.execute(sql, args).fetchone()[0]

    def spent_by_kind(self, ts: float) -> list[tuple[str, float, int]]:
        return self.db.execute("SELECT COALESCE(NULLIF(kind, ''), model), SUM(cost), COUNT(*) FROM usage WHERE ts >= ? "
                               "GROUP BY 1 ORDER BY 2 DESC", (ts,)).fetchall()

    # ---- skills: ways of doing things that worked ---------------------------------------------------------------

    @staticmethod
    def skill_status(successes: int, failures: int) -> str:
        """draft (never replayed), verified (worked once more), trusted (three times), broken (fails more than works)."""
        if failures >= 2 and failures > successes:
            return "broken"
        return "trusted" if successes >= 3 else "verified" if successes >= 1 else "draft"

    def skill_save(self, name: str, description: str, body: str) -> bool:
        """Saves or rewrites a skill; a rewrite keeps the record of how it did but is a draft until it works again."""
        old = self.skill(name)
        if old:
            self.db.execute("UPDATE skills SET description = ?, body = ?, status = 'draft', successes = 0, failures = 0 "
                            "WHERE name = ?", (description, body, name))
        else:
            self.db.execute("INSERT INTO skills (name, description, body, status, last_used) VALUES (?, ?, ?, 'draft', ?)",
                            (name, description, body, time.time()))
        self.db.commit()
        return old is None

    def skill(self, name: str) -> dict | None:
        row = self.db.execute("SELECT name, description, status, successes, failures, last_used, body FROM skills "
                              "WHERE name = ?", (name,)).fetchone()
        return dict(zip(("name", "description", "status", "successes", "failures", "last_used", "body"), row)) if row else None

    def skills(self) -> list[dict]:
        rows = self.db.execute("SELECT name, description, status, successes, failures, last_used, body FROM skills "
                               "ORDER BY successes DESC, name").fetchall()
        return [dict(zip(("name", "description", "status", "successes", "failures", "last_used", "body"), r)) for r in rows]

    def skill_outcome(self, name: str, ok: bool) -> str:
        """A skill was followed: it worked or it did not. Returns its new status."""
        row = self.skill(name)
        if row is None:
            return ""
        successes, failures = row["successes"] + ok, row["failures"] + (not ok)
        status = self.skill_status(successes, failures)
        self.db.execute("UPDATE skills SET successes = ?, failures = ?, status = ?, last_used = ? WHERE name = ?",
                        (successes, failures, status, time.time(), name))
        self.db.commit()
        return status

    def skill_forget(self, name: str) -> bool:
        cur = self.db.execute("DELETE FROM skills WHERE name = ?", (name,))
        self.db.commit()
        return bool(cur.rowcount)

    # ---- what a chat says and sends: the words and stickers its people use ---------------------------------------

    def count_words(self, chat: str, words: list[str]) -> None:
        now = time.time()
        for word in words:
            self.db.execute("INSERT INTO words (chat, word, n, last) VALUES (?, ?, 1, ?) "
                            "ON CONFLICT (chat, word) DO UPDATE SET n = n + 1, last = excluded.last", (chat, word, now))
        self.db.commit()

    def top_words(self, chat: str, limit: int = 12, min_n: int = 3, within_days: int = 45) -> list[tuple[str, int]]:
        return self.db.execute("SELECT word, n FROM words WHERE chat = ? AND n >= ? AND last >= ? "
                               "ORDER BY n DESC, last DESC LIMIT ?",
                               (chat, min_n, time.time() - within_days * 86400, limit)).fetchall()

    def sticker_seen(self, chat: str, doc: int, emoji: str, about: str = "", after: str = "") -> None:
        """A sticker somebody sent in a chat: how often, and what it answered (a thing to say it back to)."""
        self.db.execute("INSERT INTO stickers_seen (chat, doc, emoji, about, n, after, last) VALUES (?, ?, ?, ?, 1, ?, ?) "
                        "ON CONFLICT (chat, doc) DO UPDATE SET n = n + 1, last = excluded.last, "
                        "about = CASE WHEN excluded.about != '' THEN excluded.about ELSE about END, "
                        "after = CASE WHEN excluded.after != '' THEN excluded.after ELSE after END",
                        (chat, doc, emoji, about, after[:120], time.time()))
        self.db.commit()

    def top_stickers(self, chat: str, limit: int = 5) -> list[tuple[str, int]]:
        """(emoji, times) of the stickers this chat sends most."""
        return self.db.execute("SELECT emoji, SUM(n) FROM stickers_seen WHERE chat = ? AND emoji != '' "
                               "GROUP BY emoji ORDER BY SUM(n) DESC LIMIT ?", (chat, limit)).fetchall()

    # ---- his own dictionary: words people taught him -------------------------------------------------------------
    # status: known (has a meaning) · ask (he does not get it: ask people) · asked · plain (an ordinary word, skip)

    def slang_learn(self, word: str, meaning: str, example: str = "", source: str = "", chat: str = "") -> bool:
        """A word and what it means; True when it is new to the dictionary."""
        word = word.strip().lower()
        old = self.slang(word)
        self.db.execute("INSERT INTO slang (word, meaning, example, source, chat, status, learned) "
                        "VALUES (?, ?, ?, ?, ?, 'known', ?) "
                        "ON CONFLICT (word) DO UPDATE SET meaning = excluded.meaning, learned = excluded.learned, "
                        "status = 'known', source = excluded.source, "
                        "example = CASE WHEN excluded.example != '' THEN excluded.example ELSE example END, "
                        "chat = CASE WHEN chat = '' THEN excluded.chat ELSE chat END",
                        (word, meaning.strip()[:300], example.strip()[:200], source[:80], chat, time.time()))
        self.db.commit()
        return not (old and old["status"] == "known")

    def slang_mark(self, word: str, status: str, chat: str = "") -> None:
        """ask, asked or plain for a word without a meaning (a known word keeps its meaning)."""
        self.db.execute("INSERT INTO slang (word, chat, status, asked) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT (word) DO UPDATE SET status = CASE WHEN status = 'known' THEN status "
                        "ELSE excluded.status END, asked = CASE WHEN excluded.status = 'asked' THEN excluded.asked "
                        "ELSE asked END", (word.strip().lower(), chat, status, time.time()))
        self.db.commit()

    def slang(self, word: str) -> dict | None:
        keys = ("word", "meaning", "example", "source", "chat", "status", "asked", "uses")
        row = self.db.execute(f"SELECT {', '.join(keys)} FROM slang WHERE word = ?", (word.strip().lower(),)).fetchone()
        return dict(zip(keys, row)) if row else None

    def slang_words(self, limit: int = 200) -> list[tuple[str, str, str, str, int]]:
        """(word, meaning, example, chat, uses) of every word he knows, newest first."""
        return self.db.execute("SELECT word, meaning, example, chat, uses FROM slang WHERE status = 'known' "
                               "ORDER BY learned DESC LIMIT ?", (limit,)).fetchall()

    def slang_forget(self, word: str) -> bool:
        cur = self.db.execute("DELETE FROM slang WHERE word = ?", (word.strip().lower(),))
        self.db.commit()
        return cur.rowcount > 0

    def slang_used(self, words: list[str]) -> int:
        """He said these words himself: the dictionary's ones are counted (so he varies them)."""
        now, n = time.time(), 0
        for word in set(words):
            n += self.db.execute("UPDATE slang SET uses = uses + 1, last_used = ? WHERE word = ? AND status = 'known'",
                                 (now, word)).rowcount
        self.db.commit()
        return n

    def slang_to_ask(self, chat: str, limit: int = 2, quiet_days: int = 2) -> list[str]:
        """Words he does not get that this chat says, not asked about lately: «ask», or «asked» long ago."""
        return [r[0] for r in self.db.execute(
            "SELECT s.word FROM slang s JOIN words w ON w.word = s.word AND w.chat = ? WHERE s.status = 'ask' "
            "OR (s.status = 'asked' AND s.asked < ?) ORDER BY w.n DESC LIMIT ?",
            (chat, time.time() - quiet_days * 86400, limit)).fetchall()]

    def slang_candidates(self, limit: int = 25, min_n: int = 3) -> list[str]:
        """Words people say often that the dictionary has never sorted: for the night review."""
        return [r[0] for r in self.db.execute(
            "SELECT w.word FROM words w LEFT JOIN slang s ON s.word = w.word WHERE s.word IS NULL AND w.last >= ? "
            "GROUP BY w.word HAVING SUM(w.n) >= ? ORDER BY SUM(w.n) DESC LIMIT ?",
            (time.time() - 30 * 86400, min_n, limit)).fetchall()]

    def said_with(self, word: str, since: float, limit: int = 3) -> list[str]:
        """Messages of people (not Jarvis) with this word, newest first: how it is used."""
        return [r[0] for r in self.db.execute(
            "SELECT text FROM msgs WHERE ts >= ? AND sender != 'jarvis' AND (text LIKE ? OR text LIKE ?) "
            "ORDER BY id DESC LIMIT ?",  # LIKE folds only Latin case: the Cyrillic capital is asked for itself
            (since, f"%{word}%", f"%{word.capitalize()}%", limit)).fetchall()]

    # ---- people Jarvis does not answer --------------------------------------------------------------------------

    def ignored(self) -> set[str]:
        return set(json.loads(self.meta_get("ignored") or "[]"))

    def set_ignored(self, key: str, on: bool = True) -> None:
        keys = self.ignored()
        keys = keys | {key} if on else keys - {key}
        self.meta_set("ignored", json.dumps(sorted(keys)))

    # ---- meta, watchers, reminders, favorites ------------------------------------------------------------------

    def meta_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def add_watcher(self, spec: dict, due: float) -> int:
        cur = self.db.execute("INSERT INTO watchers (ts, spec, state, due) VALUES (?, ?, '{}', ?)",
                              (time.time(), json.dumps(spec, ensure_ascii=False), due))
        self.db.commit()
        return cur.lastrowid

    def watchers(self) -> list[tuple[int, dict, dict, float]]:
        rows = self.db.execute("SELECT id, spec, state, due FROM watchers ORDER BY id").fetchall()
        return [(r[0], json.loads(r[1]), json.loads(r[2] or "{}"), r[3]) for r in rows]

    def update_watcher(self, watcher_id: int, state: dict, due: float) -> None:
        self.db.execute("UPDATE watchers SET state = ?, due = ? WHERE id = ?",
                        (json.dumps(state, ensure_ascii=False), due, watcher_id))
        self.db.commit()

    def remove_watcher(self, watcher_id: int) -> bool:
        cur = self.db.execute("DELETE FROM watchers WHERE id = ?", (watcher_id,))
        self.db.commit()
        return bool(cur.rowcount)

    def add_reminder(self, due: float, text: str, call: bool = False) -> int:
        cur = self.db.execute("INSERT INTO reminders (due, text, call) VALUES (?, ?, ?)", (due, text, int(call)))
        self.db.commit()
        return cur.lastrowid

    def due_reminders(self, now: float | None = None) -> list[tuple[int, str, bool]]:
        rows = self.db.execute("SELECT id, text, call FROM reminders WHERE done = 0 AND due <= ? ORDER BY due",
                               (time.time() if now is None else now,)).fetchall()
        return [(r[0], r[1], bool(r[2])) for r in rows]

    def pending_reminders(self) -> list[tuple[int, float, str, bool]]:
        rows = self.db.execute("SELECT id, due, text, call FROM reminders WHERE done = 0 ORDER BY due").fetchall()
        return [(r[0], r[1], r[2], bool(r[3])) for r in rows]

    def finish_reminder(self, reminder_id: int) -> None:
        self.db.execute("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))
        self.db.commit()

    def log_phone(self, kind: str, cost: float) -> None:
        """An SMS or a robot call to the owner's phone, for the daily limit and the bill."""
        self.db.execute("INSERT INTO phone (ts, kind, cost) VALUES (?, ?, ?)", (time.time(), kind, cost))
        self.db.commit()

    def phone_since(self, ts: float) -> tuple[int, float]:
        row = self.db.execute("SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM phone WHERE ts >= ?", (ts,)).fetchone()
        return row[0], row[1]

    def fave_add(self, kind: str, doc: int, desc: str) -> int:
        row = self.db.execute("SELECT id FROM faves WHERE kind = ? AND doc = ?", (kind, doc)).fetchone()
        if row:
            return row[0]
        cur = self.db.execute("INSERT INTO faves (kind, doc, desc) VALUES (?, ?, ?)", (kind, doc, desc))
        self.db.commit()
        return cur.lastrowid

    def faves(self, kind: str, limit: int = 20) -> list[tuple[int, str]]:
        return self.db.execute("SELECT id, desc FROM faves WHERE kind = ? ORDER BY id DESC LIMIT ?",
                               (kind, limit)).fetchall()

    def fave_doc(self, fave_id: int) -> int | None:
        row = self.db.execute("SELECT doc FROM faves WHERE id = ?", (fave_id,)).fetchone()
        return row[0] if row else None
