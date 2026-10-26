import json
import os
import sqlite3
import tempfile
import time
import unittest

from jarvis4 import clock
from jarvis4.migrate import migrate
from jarvis4.store import Store


def old_db(path: str) -> None:
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE facts (id INTEGER PRIMARY KEY, ts REAL, text TEXT);
        CREATE TABLE reminders (id INTEGER PRIMARY KEY, due REAL, text TEXT, done INTEGER DEFAULT 0, call INTEGER DEFAULT 0);
        CREATE TABLE groups (chat INTEGER PRIMARY KEY, title TEXT, mode TEXT DEFAULT 'on', ts REAL);
        CREATE TABLE peers (id INTEGER PRIMARY KEY, ts REAL, peer INTEGER, name TEXT, role TEXT, content TEXT);
        CREATE TABLE spam (peer INTEGER PRIMARY KEY, hits INTEGER, ts REAL, blocked INTEGER DEFAULT 0);
        CREATE TABLE usage (id INTEGER PRIMARY KEY, ts REAL, model TEXT, tokens_in INTEGER, tokens_out INTEGER, cost REAL);
        CREATE TABLE phone (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, cost REAL);
        CREATE TABLE watchers (id INTEGER PRIMARY KEY, ts REAL, spec TEXT, state TEXT, due REAL);
        CREATE TABLE slang (word TEXT PRIMARY KEY, meaning TEXT, ts REAL);
    """)
    now = time.time()
    db.execute("INSERT INTO facts (ts, text) VALUES (?, 'Ван в Москве')", (now,))
    db.execute("INSERT INTO reminders (due, text, done, call) VALUES (?, 'созвон', 0, 1)", (now + 3600,))
    db.execute("INSERT INTO reminders (due, text, done) VALUES (?, 'старое', 1)", (now - 3600,))
    db.execute("INSERT INTO groups (chat, title, mode) VALUES (-100123, 'Пацаны', 'quiet')")
    db.execute("INSERT INTO peers (ts, peer, name, role, content) VALUES (?, 1000000002, 'zac', 'user', 'привет')", (now,))
    db.execute("INSERT INTO peers (ts, peer, name, role, content) VALUES (?, 555, 'Спамер', 'user', 'купи')", (now,))
    db.execute("INSERT INTO spam (peer, hits, ts, blocked) VALUES (555, 3, ?, 1)", (now,))
    db.execute("INSERT INTO usage (ts, model, tokens_in, tokens_out, cost) VALUES (?, 'claude-haiku-5-5', 10, 5, 0.25)", (now,))
    db.execute("INSERT INTO usage (ts, model, tokens_in, tokens_out, cost) VALUES (?, 'claude-haiku-5-5', 10, 5, 9.99)",
               (clock.day_start() - 100,))  # yesterday: not counted against today
    db.execute("INSERT INTO phone (ts, kind, cost) VALUES (?, 'sms', 1.5)", (now,))
    db.execute("INSERT INTO watchers (ts, spec, state, due) VALUES (?, ?, '{}', 0)", (now, json.dumps(
        {"what": "ноут", "source": "laptop_online", "notify": "", "task": "залить код", "urgent": False, "once": True})))
    db.execute("INSERT INTO slang (word, meaning, ts) VALUES ('кринж', 'неловко', ?)", (now,))
    db.commit()
    db.close()


class MigrateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = os.path.join(self.dir, "old.db")
        old_db(self.old)
        self.store = Store(":memory:")

    def test_everything_useful_comes_over(self):
        counts = migrate(self.store, self.old)
        self.assertEqual(counts["facts"], 1)
        self.assertEqual(counts["reminders"], 1)  # the done one stays behind
        self.assertEqual(self.store.chat("g100123"), ("group", "Пацаны", "quiet"))
        self.assertTrue(self.store.person("u555").blocked)
        self.assertFalse(self.store.person("u1000000002").blocked)
        self.assertAlmostEqual(self.store.spent_since(clock.day_start()), 0.25)  # only today's money counts
        self.assertEqual(self.store.phone_since(clock.day_start())[0], 1)
        spec = self.store.watchers()[0][1]
        self.assertFalse(spec["auto"])  # what the old bot did by itself is only proposed now
        self.assertEqual(json.loads(self.store.meta_get("slang_legacy")), {"кринж": "неловко"})

    def test_running_it_twice_changes_nothing(self):
        migrate(self.store, self.old)
        self.assertEqual(migrate(self.store, self.old), {})
        self.assertEqual(len(self.store.facts()), 1)

    def test_an_old_database_with_fewer_tables_is_fine(self):
        bare = os.path.join(self.dir, "bare.db")
        db = sqlite3.connect(bare)
        db.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, ts REAL, text TEXT)")
        db.commit()
        db.close()
        self.assertEqual(migrate(self.store, bare), {})


if __name__ == "__main__":
    unittest.main()
