"""SQLite operational memory with migration from the legacy learned.json file."""
import contextlib
import datetime as dt
import difflib
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from core import settings as cfg


PATH = cfg.ROOT / "learned.json"          # kept patchable for compatibility/tests
_DEFAULT_LEGACY_PATH = PATH
DB_PATH = cfg.DB_PATH
SIMILARITY = 0.86

_lock = threading.RLock()
_cache = None                              # compatibility with the old test surface
_ready = set()


def _db_path():
    # Tests and portable callers historically patch PATH. Give each patched JSON
    # path its own SQLite sibling so isolation remains automatic.
    if Path(PATH) != Path(_DEFAULT_LEGACY_PATH):
        return Path(PATH).with_suffix(".db")
    return Path(DB_PATH)


@contextlib.contextmanager
def _connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        _ensure_schema(conn, path)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_schema(conn, path):
    marker = str(path.resolve())
    if marker in _ready:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS learned_commands (
            phrase TEXT PRIMARY KEY,
            skill TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'ai',
            used INTEGER NOT NULL DEFAULT 1,
            learned_at TEXT NOT NULL,
            last_used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notes (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            created_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id TEXT PRIMARY KEY,
            text TEXT NOT NULL,
            due_at REAL NOT NULL,
            created_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            repeat_seconds INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status, due_at);
        CREATE TABLE IF NOT EXISTS routines (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            trigger_phrase TEXT NOT NULL DEFAULT '',
            steps_json TEXT NOT NULL DEFAULT '[]',
            stop_on_error INTEGER NOT NULL DEFAULT 1,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS provider_health (
            provider TEXT PRIMARY KEY,
            successes INTEGER NOT NULL DEFAULT 0,
            failures INTEGER NOT NULL DEFAULT 0,
            latency_ms REAL NOT NULL DEFAULT 0,
            cooldown_until REAL NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS permissions (
            skill TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            always_confirm INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS action_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            session_id TEXT NOT NULL DEFAULT '',
            skill TEXT NOT NULL,
            params_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            message TEXT NOT NULL DEFAULT '',
            risk INTEGER NOT NULL DEFAULT 0
        );
        """
    )
    _migrate_legacy(conn)
    _ready.add(marker)


def _migrate_legacy(conn):
    done = conn.execute("SELECT value FROM meta WHERE key='legacy_json_migrated'").fetchone()
    if done:
        return
    legacy = Path(PATH)
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            for item in data.get("commands", []):
                phrase = str(item.get("phrase", "")).strip()
                skill = str(item.get("skill", "")).strip()
                if not phrase or not skill:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO learned_commands
                       (phrase, skill, params_json, source, used, learned_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (phrase, skill, json.dumps(item.get("params") or {}, ensure_ascii=False),
                     item.get("source", "legacy"), int(item.get("used", 1)),
                     item.get("learned", _stamp())),
                )
        except (OSError, ValueError, TypeError):
            pass
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_json_migrated', '1')")


def _stamp():
    return dt.datetime.now().isoformat(timespec="seconds")


def _key(text):
    from core import router

    return router.normalize(text)


def _decode(value, fallback):
    try:
        decoded = json.loads(value)
        return decoded
    except (TypeError, ValueError):
        return fallback


def lookup(text, exact=False):
    phrase = _key(text)
    if not phrase:
        return None, None
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT phrase, skill, params_json FROM learned_commands"
        ).fetchall()
        chosen = next((row for row in rows if row["phrase"] == phrase), None)
        if chosen is None and rows and not exact:
            close = difflib.get_close_matches(
                phrase, [row["phrase"] for row in rows], n=1, cutoff=SIMILARITY
            )
            chosen = next((row for row in rows if close and row["phrase"] == close[0]), None)
        if chosen is None:
            return None, None
        conn.execute(
            "UPDATE learned_commands SET used=used+1, last_used_at=? WHERE phrase=?",
            (_stamp(), chosen["phrase"]),
        )
        return chosen["skill"], _decode(chosen["params_json"], {})


def remember(text, skill, params=None, source="ai"):
    phrase = _key(text)
    if not phrase or not skill:
        return False
    payload = json.dumps(params or {}, ensure_ascii=False)
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO learned_commands
               (phrase, skill, params_json, source, used, learned_at, last_used_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(phrase) DO UPDATE SET
                   skill=excluded.skill,
                   params_json=excluded.params_json,
                   source=excluded.source,
                   used=learned_commands.used+1,
                   last_used_at=excluded.last_used_at""",
            (phrase, skill, payload, source, _stamp(), _stamp()),
        )
    return True


def forget(text):
    phrase = _key(text)
    with _lock, _connect() as conn:
        cursor = conn.execute("DELETE FROM learned_commands WHERE phrase=?", (phrase,))
        return cursor.rowcount > 0


def clear():
    with _lock, _connect() as conn:
        conn.execute("DELETE FROM learned_commands")


def count():
    with _lock, _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM learned_commands").fetchone()[0]


def recent(limit=10):
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT phrase, skill, params_json, source, used, learned_at
               FROM learned_commands ORDER BY COALESCE(last_used_at, learned_at) DESC LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) | {"params": _decode(row["params_json"], {})} for row in rows]


# Notes ---------------------------------------------------------------------
def add_note(text):
    text = (text or "").strip()
    if not text:
        return None
    note_id = uuid.uuid4().hex
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO notes(id, text, created_at) VALUES(?, ?, ?)",
                     (note_id, text, _stamp()))
    return note_id


def list_notes(limit=20, include_archived=False):
    query = "SELECT id, text, created_at, archived FROM notes"
    if not include_archived:
        query += " WHERE archived=0"
    query += " ORDER BY created_at DESC LIMIT ?"
    with _lock, _connect() as conn:
        return [dict(row) for row in conn.execute(query, (max(1, int(limit)),)).fetchall()]


def delete_note(note_id):
    with _lock, _connect() as conn:
        return conn.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount > 0


# Reminders -----------------------------------------------------------------
def add_reminder(text, due_at, repeat_seconds=0):
    reminder_id = uuid.uuid4().hex
    due = float(due_at)
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO reminders(id, text, due_at, created_at, repeat_seconds)
               VALUES(?, ?, ?, ?, ?)""",
            (reminder_id, (text or "Напоминание").strip(), due, time.time(),
             max(0, int(repeat_seconds))),
        )
    return reminder_id


def pending_reminders(limit=50):
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, text, due_at, created_at, status, repeat_seconds
               FROM reminders WHERE status='pending' ORDER BY due_at LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(row) for row in rows]


def due_reminders(now=None, limit=10):
    moment = time.time() if now is None else float(now)
    with _lock, _connect() as conn:
        rows = conn.execute(
            """SELECT id, text, due_at, created_at, status, repeat_seconds
               FROM reminders WHERE status='pending' AND due_at<=?
               ORDER BY due_at LIMIT ?""",
            (moment, max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]


def complete_reminder(reminder_id, now=None):
    moment = time.time() if now is None else float(now)
    with _lock, _connect() as conn:
        row = conn.execute("SELECT repeat_seconds FROM reminders WHERE id=?", (reminder_id,)).fetchone()
        if row is None:
            return False
        if row["repeat_seconds"] > 0:
            conn.execute("UPDATE reminders SET due_at=? WHERE id=?",
                         (moment + row["repeat_seconds"], reminder_id))
        else:
            conn.execute("UPDATE reminders SET status='done' WHERE id=?", (reminder_id,))
        return True


def snooze_reminder(reminder_id, seconds=600):
    with _lock, _connect() as conn:
        cursor = conn.execute(
            "UPDATE reminders SET due_at=?, status='pending' WHERE id=?",
            (time.time() + max(30, int(seconds)), reminder_id),
        )
        return cursor.rowcount > 0


# Routines ------------------------------------------------------------------
def save_routine(name, steps, trigger_phrase="", stop_on_error=True, enabled=True,
                 routine_id=None):
    name = (name or "").strip()
    if not name or not isinstance(steps, list):
        return None
    routine_id = routine_id or uuid.uuid4().hex
    now = _stamp()
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO routines
               (id, name, trigger_phrase, steps_json, stop_on_error, enabled, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                   trigger_phrase=excluded.trigger_phrase, steps_json=excluded.steps_json,
                   stop_on_error=excluded.stop_on_error, enabled=excluded.enabled,
                   updated_at=excluded.updated_at""",
            (routine_id, name, (trigger_phrase or "").strip(),
             json.dumps(steps, ensure_ascii=False), bool(stop_on_error), bool(enabled), now, now),
        )
    return routine_id


def list_routines(enabled_only=False):
    query = "SELECT * FROM routines"
    if enabled_only:
        query += " WHERE enabled=1"
    query += " ORDER BY name"
    with _lock, _connect() as conn:
        rows = conn.execute(query).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["steps"] = _decode(item.pop("steps_json"), [])
            out.append(item)
        return out


def find_routine(name_or_trigger):
    query = _key(name_or_trigger)
    for routine in list_routines(enabled_only=True):
        if query in (_key(routine["name"]), _key(routine["trigger_phrase"])):
            return routine
    return None


def delete_routine(routine_id):
    with _lock, _connect() as conn:
        return conn.execute("DELETE FROM routines WHERE id=?", (routine_id,)).rowcount > 0


# Provider health, permissions and action audit -----------------------------
def provider_status(provider):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT * FROM provider_health WHERE provider=?", (provider,)).fetchone()
        return dict(row) if row else {
            "provider": provider, "successes": 0, "failures": 0,
            "latency_ms": 0.0, "cooldown_until": 0.0, "last_error": "", "updated_at": 0.0,
        }


def record_provider(provider, ok, latency_ms=0.0, error="", cooldown_until=0.0):
    previous = provider_status(provider)
    latency = float(latency_ms or 0)
    average = latency if not previous["latency_ms"] else previous["latency_ms"] * 0.7 + latency * 0.3
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO provider_health
               (provider, successes, failures, latency_ms, cooldown_until, last_error, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(provider) DO UPDATE SET successes=excluded.successes,
                   failures=excluded.failures, latency_ms=excluded.latency_ms,
                   cooldown_until=excluded.cooldown_until, last_error=excluded.last_error,
                   updated_at=excluded.updated_at""",
            (provider, previous["successes"] + int(bool(ok)),
             previous["failures"] + int(not ok), average,
             float(cooldown_until or 0), "" if ok else str(error)[:300], time.time()),
        )


def permission(skill):
    with _lock, _connect() as conn:
        row = conn.execute("SELECT enabled, always_confirm FROM permissions WHERE skill=?", (skill,)).fetchone()
        return {"enabled": True, "always_confirm": False} if row is None else {
            "enabled": bool(row["enabled"]), "always_confirm": bool(row["always_confirm"])
        }


def set_permission(skill, enabled=True, always_confirm=False):
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO permissions(skill, enabled, always_confirm) VALUES(?, ?, ?)
               ON CONFLICT(skill) DO UPDATE SET enabled=excluded.enabled,
                   always_confirm=excluded.always_confirm""",
            (skill, bool(enabled), bool(always_confirm)),
        )


def log_action(skill, params=None, status="ok", message="", risk=0, session_id=""):
    with _lock, _connect() as conn:
        conn.execute(
            """INSERT INTO action_log(created_at, session_id, skill, params_json, status, message, risk)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), session_id or "", skill,
             json.dumps(params or {}, ensure_ascii=False), status, str(message)[:500], int(risk)),
        )


def recent_actions(limit=30):
    with _lock, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM action_log ORDER BY id DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["params"] = _decode(item.pop("params_json"), {})
            result.append(item)
        return result


def cleanup(days=None):
    keep = cfg.ACTION_LOG_DAYS if days is None else max(0, int(days))
    with _lock, _connect() as conn:
        if keep == 0:
            conn.execute("DELETE FROM action_log")
        else:
            conn.execute("DELETE FROM action_log WHERE created_at<?",
                         (time.time() - keep * 86400,))
        conn.execute("DELETE FROM reminders WHERE status='done' AND due_at<?",
                     (time.time() - max(keep, 7) * 86400,))


def database_info():
    path = _db_path()
    return {"path": str(path), "exists": path.exists(),
            "bytes": path.stat().st_size if path.exists() else 0}
