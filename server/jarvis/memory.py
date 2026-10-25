import json
import logging
import os
import sqlite3
import time

log = logging.getLogger("jarvis.memory")


class Memory:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, ts REAL, role TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY, ts REAL, model TEXT,
                tokens_in INTEGER, tokens_out INTEGER, cost REAL);
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY, ts REAL, text TEXT);
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY, due REAL, text TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS watchers (
                id INTEGER PRIMARY KEY, ts REAL, spec TEXT, state TEXT, due REAL);
            CREATE TABLE IF NOT EXISTS phone (
                id INTEGER PRIMARY KEY, ts REAL, kind TEXT, cost REAL);
            CREATE TABLE IF NOT EXISTS peers (
                id INTEGER PRIMARY KEY, ts REAL, peer INTEGER, name TEXT, role TEXT, content TEXT);
            CREATE INDEX IF NOT EXISTS peers_by_peer ON peers (peer, id);
            CREATE TABLE IF NOT EXISTS summary (id INTEGER PRIMARY KEY, upto INTEGER, text TEXT);
            CREATE TABLE IF NOT EXISTS slang (word TEXT PRIMARY KEY, meaning TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS spam (peer INTEGER PRIMARY KEY, hits INTEGER, ts REAL, blocked INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS groups (chat INTEGER PRIMARY KEY, title TEXT, mode TEXT DEFAULT 'on', ts REAL);
            CREATE TABLE IF NOT EXISTS group_log (
                id INTEGER PRIMARY KEY, chat INTEGER, msg INTEGER, ts REAL, sender INTEGER, name TEXT, text TEXT,
                media INTEGER DEFAULT 0, kind TEXT DEFAULT '');
            CREATE UNIQUE INDEX IF NOT EXISTS group_log_msg ON group_log (chat, msg);
            CREATE TABLE IF NOT EXISTS faves (id INTEGER PRIMARY KEY, kind TEXT, doc INTEGER, desc TEXT, ts REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS faves_doc ON faves (kind, doc);
            """
        )
        try:  # reminders that ring the owner instead of writing to him
            self.db.execute("ALTER TABLE reminders ADD COLUMN call INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # the column is already there

    def add(self, role: str, content: str) -> None:
        self.db.execute(
            "INSERT INTO messages (ts, role, content) VALUES (?, ?, ?)",
            (time.time(), role, content),
        )
        self.db.commit()

    def recent(self, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        msgs = [{"role": r, "content": c} for r, c in reversed(rows)]
        # The API wants the conversation to start with the user
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        return msgs

    def clear(self) -> None:
        self.db.execute("DELETE FROM messages")
        self.db.execute("DELETE FROM summary")
        self.db.commit()

    # Older talk is folded into one rolling summary, so the window can stay small and nothing is forgotten
    def window_start(self, limit: int) -> int:
        """Id of the oldest message among the last `limit`; everything before it is outside the window."""
        row = self.db.execute("SELECT MIN(id) FROM (SELECT id FROM messages ORDER BY id DESC LIMIT ?)",
                              (limit,)).fetchone()
        return row[0] or 0

    def summary(self) -> tuple[int, str]:
        row = self.db.execute("SELECT upto, text FROM summary WHERE id = 1").fetchone()
        return (row[0], row[1]) if row else (0, "")

    def set_summary(self, upto: int, text: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO summary (id, upto, text) VALUES (1, ?, ?)", (upto, text))
        self.db.commit()

    def unsummarized(self, before_id: int, limit: int) -> list[tuple[int, str, str]]:
        """Messages that are neither in the summary nor in the window: (id, role, text), oldest first."""
        upto, _ = self.summary()
        return self.db.execute("SELECT id, role, content FROM messages WHERE id > ? AND id < ? ORDER BY id LIMIT ?",
                               (upto, before_id, limit)).fetchall()

    def log_usage(self, model: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        self.db.execute(
            "INSERT INTO usage (ts, model, tokens_in, tokens_out, cost) VALUES (?, ?, ?, ?, ?)",
            (time.time(), model, tokens_in, tokens_out, cost),
        )
        self.db.commit()

    def spent_since(self, ts: float, prefix: str = "") -> float:
        """Dollars spent since ts; prefix narrows it to models logged under that name (e.g. «chat:»)."""
        row = self.db.execute("SELECT COALESCE(SUM(cost), 0) FROM usage WHERE ts >= ? AND model LIKE ?",
                              (ts, prefix + "%")).fetchone()
        return row[0]

    def drop_last(self, role: str, content: str) -> None:
        """Takes back the newest message if it is this one: a request another model then answers is stored once."""
        row = self.db.execute("SELECT id, role, content FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        if row and row[1] == role and row[2] == content:
            self.db.execute("DELETE FROM messages WHERE id = ?", (row[0],))
            self.db.commit()

    # Long-term facts about the user; all of them go into the system prompt
    def remember(self, text: str) -> int:
        cur = self.db.execute("INSERT INTO facts (ts, text) VALUES (?, ?)", (time.time(), text))
        self.db.commit()
        return cur.lastrowid

    def forget(self, fact_id: int) -> bool:
        cur = self.db.execute("DELETE FROM facts WHERE id = ?", (fact_id,))
        self.db.commit()
        return cur.rowcount > 0

    def facts(self) -> list[tuple[int, str]]:
        return self.db.execute("SELECT id, text FROM facts ORDER BY id").fetchall()

    def add_reminder(self, due: float, text: str, call: bool = False) -> int:
        cur = self.db.execute("INSERT INTO reminders (due, text, call) VALUES (?, ?, ?)", (due, text, int(call)))
        self.db.commit()
        return cur.lastrowid

    def due_reminders(self) -> list[tuple[int, str, bool]]:
        rows = self.db.execute(
            "SELECT id, text, call FROM reminders WHERE done = 0 AND due <= ?", (time.time(),)).fetchall()
        return [(i, text, bool(call)) for i, text, call in rows]

    def pending_reminders(self) -> list[tuple[int, float, str, bool]]:
        rows = self.db.execute("SELECT id, due, text, call FROM reminders WHERE done = 0 ORDER BY due").fetchall()
        return [(i, due, text, bool(call)) for i, due, text, call in rows]

    def finish_reminder(self, reminder_id: int) -> bool:
        cur = self.db.execute("UPDATE reminders SET done = 1 WHERE id = ? AND done = 0", (reminder_id,))
        self.db.commit()
        return cur.rowcount > 0

    # Watchers: spec is what to check, state is what the last check saw
    def add_watcher(self, spec: dict, due: float) -> int:
        cur = self.db.execute("INSERT INTO watchers (ts, spec, state, due) VALUES (?, ?, '{}', ?)",
                              (time.time(), json.dumps(spec, ensure_ascii=False), due))
        self.db.commit()
        log.info("watcher %s added, first check in %d s: %s", cur.lastrowid, due - time.time(), spec)
        return cur.lastrowid

    def watchers(self) -> list[tuple[int, dict, dict, float]]:
        rows = self.db.execute("SELECT id, spec, state, due FROM watchers ORDER BY id").fetchall()
        return [(i, json.loads(spec), json.loads(state), due) for i, spec, state, due in rows]

    def update_watcher(self, watcher_id: int, state: dict, due: float) -> None:
        self.db.execute("UPDATE watchers SET state = ?, due = ? WHERE id = ?",
                        (json.dumps(state, ensure_ascii=False), due, watcher_id))
        self.db.commit()

    def remove_watcher(self, watcher_id: int) -> bool:
        cur = self.db.execute("DELETE FROM watchers WHERE id = ?", (watcher_id,))
        self.db.commit()
        log.info("watcher %s removed (%s)", watcher_id, "was there" if cur.rowcount else "not found")
        return cur.rowcount > 0

    # SMS and calls cost rubles, counted apart from the AI spend
    def log_phone(self, kind: str, cost: float) -> None:
        self.db.execute("INSERT INTO phone (ts, kind, cost) VALUES (?, ?, ?)", (time.time(), kind, cost))
        self.db.commit()

    def phone_since(self, ts: float) -> tuple[int, float]:
        return self.db.execute(
            "SELECT COUNT(*), COALESCE(SUM(cost), 0) FROM phone WHERE ts >= ?", (ts,)).fetchone()

    # Conversations of Jarvis's own Telegram account with people other than the owner
    def add_peer(self, peer: int, name: str, role: str, content: str) -> None:
        self.db.execute("INSERT INTO peers (ts, peer, name, role, content) VALUES (?, ?, ?, ?, ?)",
                        (time.time(), peer, name, role, content))
        self.db.commit()

    def peer_history(self, peer: int, limit: int) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT role, content FROM peers WHERE peer = ? ORDER BY id DESC LIMIT ?",
                               (peer, limit)).fetchall()
        return rows[::-1]

    def peer_replies_since(self, ts: float, peer: int | None = None) -> int:
        if peer is None:
            row = self.db.execute("SELECT COUNT(*) FROM peers WHERE role = 'model' AND ts >= ?", (ts,)).fetchone()
        else:
            row = self.db.execute("SELECT COUNT(*) FROM peers WHERE role = 'model' AND ts >= ? AND peer = ?",
                                  (ts, peer)).fetchone()
        return row[0]

    def peer_chats(self, limit: int) -> list[tuple[int, str, float, int]]:
        """Most recent conversations first: (peer, name, last time, message count)."""
        return self.db.execute(
            "SELECT peer, MAX(name), MAX(ts), COUNT(*) FROM peers GROUP BY peer ORDER BY MAX(ts) DESC LIMIT ?",
            (limit,)).fetchall()

    # Today's slang that Jarvis learned: (word, meaning), newest last
    def learn_slang(self, word: str, meaning: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO slang (word, meaning, ts) VALUES (?, ?, ?)",
                        (word.strip().lower(), meaning.strip(), time.time()))
        self.db.commit()

    def slang_words(self, limit: int) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT word, meaning FROM slang ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return rows[::-1]

    # Strangers who only send junk: counted, then blocked
    def spam_hit(self, peer: int) -> int:
        self.db.execute("INSERT INTO spam (peer, hits, ts) VALUES (?, 1, ?) "
                        "ON CONFLICT(peer) DO UPDATE SET hits = hits + 1, ts = excluded.ts", (peer, time.time()))
        self.db.commit()
        return self.db.execute("SELECT hits FROM spam WHERE peer = ?", (peer,)).fetchone()[0]

    def block(self, peer: int) -> None:
        self.db.execute("INSERT INTO spam (peer, hits, ts, blocked) VALUES (?, 0, ?, 1) "
                        "ON CONFLICT(peer) DO UPDATE SET blocked = 1", (peer, time.time()))
        self.db.commit()

    def is_blocked(self, peer: int) -> bool:
        row = self.db.execute("SELECT blocked FROM spam WHERE peer = ?", (peer,)).fetchone()
        return bool(row and row[0])

    def drop_last_peer(self, peer: int) -> None:
        """Forget the stranger's last message: junk must not sit in the context of a real conversation."""
        self.db.execute("DELETE FROM peers WHERE id = (SELECT MAX(id) FROM peers WHERE peer = ?)", (peer,))
        self.db.commit()

    def meta_get(self, key: str, default: str = "") -> str:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    # Group chats: where Jarvis may talk, what was said lately, how much he already said himself
    GROUP_LOG_KEEP = 300  # other people's messages kept per group; Jarvis's own rows stay for the daily counts

    def group_seen(self, chat: int, title: str) -> bool:
        """Registers the group (and its current title); True when Jarvis had not met it before."""
        cur = self.db.execute("INSERT OR IGNORE INTO groups (chat, title, ts) VALUES (?, ?, ?)",
                              (chat, title, time.time()))
        if not cur.rowcount:
            self.db.execute("UPDATE groups SET title = ? WHERE chat = ?", (title, chat))
        self.db.commit()
        return bool(cur.rowcount)

    def group_mode(self, chat: int) -> str:
        row = self.db.execute("SELECT mode FROM groups WHERE chat = ?", (chat,)).fetchone()
        return row[0] if row else "on"

    def set_group_mode(self, chat: int, mode: str) -> None:
        self.db.execute("UPDATE groups SET mode = ? WHERE chat = ?", (mode, chat))
        self.db.commit()

    def groups(self) -> list[tuple[int, str, str]]:
        return self.db.execute("SELECT chat, title, mode FROM groups ORDER BY ts").fetchall()

    def group_add(self, chat: int, msg: int, sender: int, name: str, text: str, media: bool = False,
                  kind: str = "") -> None:
        """kind is '' for what people said, 'reply' / 'spont' for what Jarvis said (answered / on his own)."""
        self.db.execute("INSERT OR IGNORE INTO group_log (chat, msg, ts, sender, name, text, media, kind) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chat, msg, time.time(), sender, name, text, int(media), kind))
        self.db.execute("DELETE FROM group_log WHERE chat = ? AND kind = '' AND id <= "
                        "(SELECT id FROM group_log WHERE chat = ? AND kind = '' ORDER BY id DESC LIMIT 1 OFFSET ?)",
                        (chat, chat, self.GROUP_LOG_KEEP))
        self.db.commit()

    def group_recent(self, chat: int, limit: int) -> list[tuple[int, str, str]]:
        """The last messages of the group, oldest first: (sender, name, text). Reactions are not messages."""
        rows = self.db.execute("SELECT sender, name, text FROM group_log WHERE chat = ? AND msg > 0 "
                               "ORDER BY id DESC LIMIT ?", (chat, limit)).fetchall()
        return rows[::-1]

    def group_media_since(self, chat: int, ts: float) -> int | None:
        """Id of the newest picture, sticker or video that people (not Jarvis) posted since ts."""
        row = self.db.execute("SELECT msg FROM group_log WHERE chat = ? AND media = 1 AND kind = '' AND ts >= ? "
                              "AND msg > 0 ORDER BY id DESC LIMIT 1", (chat, ts)).fetchone()
        return row[0] if row else None

    def group_count(self, chat: int | None, since: float, kinds: tuple[str, ...] = ("reply", "spont")) -> int:
        """How many times Jarvis spoke or reacted since ts, in one group or (chat None) in all of them."""
        marks = ",".join("?" * len(kinds))
        if chat is None:
            row = self.db.execute(f"SELECT COUNT(*) FROM group_log WHERE kind IN ({marks}) AND ts >= ?",
                                  (*kinds, since)).fetchone()
        else:
            row = self.db.execute(f"SELECT COUNT(*) FROM group_log WHERE chat = ? AND kind IN ({marks}) AND ts >= ?",
                                  (chat, *kinds, since)).fetchone()
        return row[0]

    # Jarvis's own taste: GIFs he liked enough to keep (the file itself lives in Telegram's saved GIFs)
    FAVES_KEEP = 60

    def fave_add(self, kind: str, doc: int, desc: str) -> int:
        self.db.execute("INSERT OR REPLACE INTO faves (kind, doc, desc, ts) VALUES (?, ?, ?, ?)",
                        (kind, doc, desc.strip(), time.time()))
        self.db.execute("DELETE FROM faves WHERE kind = ? AND id NOT IN "
                        "(SELECT id FROM faves WHERE kind = ? ORDER BY ts DESC LIMIT ?)", (kind, kind, self.FAVES_KEEP))
        self.db.commit()
        return self.db.execute("SELECT id FROM faves WHERE kind = ? AND doc = ?", (kind, doc)).fetchone()[0]

    def faves(self, kind: str, limit: int) -> list[tuple[int, str]]:
        """Newest last: (number to refer to it by, description)."""
        rows = self.db.execute("SELECT id, desc FROM faves WHERE kind = ? ORDER BY ts DESC LIMIT ?",
                               (kind, limit)).fetchall()
        return rows[::-1]

    def fave_doc(self, fave_id: int) -> int | None:
        row = self.db.execute("SELECT doc FROM faves WHERE id = ?", (fave_id,)).fetchone()
        return row[0] if row else None
