"""Link to the Jarvis 3.0 server brain.

hands.py keeps an SSH tunnel to the server hub open. The voice and the chat panel
ask the server's brain through the same tunnel, so the laptop and Telegram share
one brain and one memory. Local rules still run first and need no server.
"""
import json
import os
import re
import urllib.error
import urllib.request

from core import settings  # noqa: F401  loads .env before the values below are read

TOKEN = os.environ.get("JARVIS_HANDS_TOKEN", "")
# AmneziaVPN blocks Tailscale TCP, so by default the hub is reached through SSH,
# which already works. Empty JARVIS_SSH means connect to JARVIS_HUB directly.
SSH_TARGET = os.environ.get("JARVIS_SSH", "administrator@85.190.96.176")
HUB = os.environ.get("JARVIS_HUB", "100.115.94.31:8765")
LOCAL_PORT = 18765
BASE = "http://127.0.0.1:%d" % LOCAL_PORT if SSH_TARGET else "http://%s" % HUB
# A request can wait for a confirmation button in Telegram (5 minutes) on top of the work
ASK_TIMEOUT = 900

_COST = re.compile(r"\s*\(\$(\d+\.\d+)\)\s*$")
# The tunnel is local: never send it through a system proxy
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ServerUnavailable(RuntimeError):
    pass


def enabled():
    return bool(TOKEN) and os.environ.get("JARVIS_SERVER_BRAIN", "1") != "0"


def _post(path, payload, timeout):
    request = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServerUnavailable(str(getattr(exc, "reason", exc))) from exc


def ask(text):
    """Returns (answer, cost) where cost is the USD footer the brain adds, or None."""
    answer = str(_post("/ask", {"text": text}, ASK_TIMEOUT).get("answer") or "")
    match = _COST.search(answer)
    if not match:
        return answer.strip(), None
    return answer[:match.start()].strip(), float(match.group(1))


def stop():
    try:
        return bool(_post("/stop", {}, 5).get("stopped"))
    except ServerUnavailable:
        return False
