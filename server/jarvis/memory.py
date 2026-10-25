import json
import os
import sqlite3
import time


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
            """
        )

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
        self.db.commit()

    def log_usage(self, model: str, tokens_in: int, tokens_out: int, cost: float) -> None:
        self.db.execute(
            "INSERT INTO usage (ts, model, tokens_in, tokens_out, cost) VALUES (?, ?, ?, ?, ?)",
            (time.time(), model, tokens_in, tokens_out, cost),
        )
        self.db.commit()

    def spent_since(self, ts: float) -> float:
        row = self.db.execute("SELECT COALESCE(SUM(cost), 0) FROM usage WHERE ts >= ?", (ts,)).fetchone()
        return row[0]

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

    def add_reminder(self, due: float, text: str) -> int:
        cur = self.db.execute("INSERT INTO reminders (due, text) VALUES (?, ?)", (due, text))
        self.db.commit()
        return cur.lastrowid

    def due_reminders(self) -> list[tuple[int, str]]:
        return self.db.execute(
            "SELECT id, text FROM reminders WHERE done = 0 AND due <= ?", (time.time(),)
        ).fetchall()

    def pending_reminders(self) -> list[tuple[int, float, str]]:
        return self.db.execute(
            "SELECT id, due, text FROM reminders WHERE done = 0 ORDER BY due"
        ).fetchall()

    def finish_reminder(self, reminder_id: int) -> bool:
        cur = self.db.execute("UPDATE reminders SET done = 1 WHERE id = ? AND done = 0", (reminder_id,))
        self.db.commit()
        return cur.rowcount > 0

    # Watchers: spec is what to check, state is what the last check saw
    def add_watcher(self, spec: dict, due: float) -> int:
        cur = self.db.execute("INSERT INTO watchers (ts, spec, state, due) VALUES (?, ?, '{}', ?)",
                              (time.time(), json.dumps(spec, ensure_ascii=False), due))
        self.db.commit()
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
