"""Client for the local screen parser (vision/parse_server.py, OmniParser V2 on the GPU).

The parser lives in its own environment (.vision-venv: torch, YOLO, Florence-2) so the
main app stays light. It is started on the first request and exits by itself when idle.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

from core import settings

ROOT = str(settings.ROOT)
PYTHON = os.path.join(ROOT, ".vision-venv", "Scripts", "pythonw.exe")
SERVER = os.path.join(ROOT, "vision", "parse_server.py")
URL = "http://127.0.0.1:18766"
START_TIMEOUT = 120  # first start loads three models onto the GPU
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class VisionError(RuntimeError):
    pass


def installed():
    return os.path.isfile(PYTHON) and os.path.isfile(SERVER)


def _get(path, timeout):
    with _opener.open(URL + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _running():
    try:
        return bool(_get("/health", 2).get("ok"))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _ensure():
    if _running():
        return
    if not installed():
        raise VisionError("локальное зрение не установлено")
    subprocess.Popen([PYTHON, SERVER], cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(1.5)
        if _running():
            return
    raise VisionError("локальное зрение не запустилось за %d с" % START_TIMEOUT)


def parse(image_b64):
    """Elements on a screenshot: [{"id", "type": button|icon|text, "content", "center": [x, y]}]."""
    _ensure()
    request = urllib.request.Request(URL + "/parse", data=json.dumps({"image": image_b64}).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
    try:
        with _opener.open(request, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise VisionError("ошибка разбора: %s" % exc.read().decode("utf-8", "replace")[:200]) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise VisionError("зрение не ответило: %s" % exc) from exc
    return data["elements"], data.get("ms", 0)


KIND = {"button": "кнопка", "icon": "иконка", "text": "текст"}


def describe(elements, ms, max_chars=4600):
    """Whole lines only, within what the brain reads of one tool result."""
    lines = ["Локальное зрение (%d мс), координаты для click_at:" % ms]
    size = len(lines[0])
    for n, e in enumerate(elements):
        line = "%s %s «%s» @%d,%d" % (e["id"], KIND.get(e["type"], e["type"]), e["content"][:50], *e["center"])
        if size + len(line) + 1 > max_chars:
            lines.append("(ещё %d элементов не показано)" % (len(elements) - n))
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)
