"""Windows-protected storage for API credentials.

Secrets are encrypted with DPAPI for the current Windows user and persisted in
LOCALAPPDATA.  The module deliberately has no dependency on the rest of Jarvis
so settings can import it during bootstrap without creating a cycle.
"""
import base64
import json
import os
import threading
from pathlib import Path


DATA_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis"
PATH = DATA_DIR / "secrets.json"
_lock = threading.RLock()


def _protect(value):
    try:
        import win32crypt

        return win32crypt.CryptProtectData(
            value.encode("utf-8"), "Jarvis credential", None, None, None, 0
        )
    except Exception as exc:
        raise RuntimeError("Windows DPAPI недоступен: %s" % exc) from exc


def _unprotect(blob):
    try:
        import win32crypt

        _description, clear = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return clear.decode("utf-8")
    except Exception:
        return ""


def _read():
    if not PATH.exists():
        return {}
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, PATH)


def get(name, default=""):
    with _lock:
        encoded = _read().get(name, "")
        if not encoded:
            return default
        try:
            return _unprotect(base64.b64decode(encoded.encode("ascii"))) or default
        except (ValueError, TypeError):
            return default


def set(name, value):
    value = (value or "").strip()
    with _lock:
        data = _read()
        if value:
            data[name] = base64.b64encode(_protect(value)).decode("ascii")
        else:
            data.pop(name, None)
        _write(data)
    return True


def delete(name):
    return set(name, "")


def has(name):
    return bool(get(name))


def names():
    with _lock:
        return sorted(_read())
