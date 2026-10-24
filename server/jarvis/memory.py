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
