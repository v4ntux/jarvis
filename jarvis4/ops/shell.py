"""Commands on the server itself: short ones with a timeout, long ones as background jobs.

The server is on 24/7, so anything that does not need the laptop (downloads, scripts,
file processing) runs here and keeps going while the laptop sleeps.
"""
import asyncio
import itertools
import logging
import os
import signal
from typing import Awaitable, Callable

log = logging.getLogger("jarvis4.shell")

WORK = "/opt/jarvis/data/work"
JOBS = "/opt/jarvis/data/jobs"
# Jarvis's own venv: pip install there freely, the bot's venv stays untouched
VENV = "/opt/jarvis/data/venv"
TAIL = 1500

_job_ids = itertools.count(1)
_running: dict[int, tuple[str, asyncio.subprocess.Process]] = {}
_waiters: set[asyncio.Task] = set()


def _env() -> dict:
    env = dict(os.environ)
    # The bot's secrets are not for scripts Jarvis writes
    for key in ("ANTHROPIC_API_KEY", "TG_BOT_TOKEN", "HANDS_TOKEN", "TG_API_ID", "TG_API_HASH"):
        env.pop(key, None)
    env["PATH"] = f"{VENV}/bin:{env.get('PATH', '/usr/bin:/bin')}"
    env["VIRTUAL_ENV"] = VENV
    return env


def _kill(proc: asyncio.subprocess.Process) -> None:
    # Commands run in their own process group, so pipelines and children die too
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


TIMED_OUT = -1000


async def exec(command: str, timeout: int) -> tuple[int, str]:
    """(exit code, output) for code that decides by the code; TIMED_OUT when the command did not finish."""
    os.makedirs(WORK, exist_ok=True)
    proc = await asyncio.create_subprocess_shell(
        command, cwd=WORK, env=_env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill(proc)
        return TIMED_OUT, ""
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def run(command: str, timeout: int) -> str:
    code, text = await exec(command, timeout)
    if code == TIMED_OUT:
        return f"Ошибка: команда не уложилась в {timeout} с. Для долгого есть server_job."
    return f"(код {code})\n{text}" if code else text or "(пусто, код 0)"


async def start_job(command: str, what: str, done: Callable[[int, int, str], Awaitable[None]]) -> int:
    """Starts the command in the background; done(job_id, exit_code, log_tail) runs when it ends."""
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(JOBS, exist_ok=True)
    job_id = next(_job_ids)
    path = os.path.join(JOBS, f"{job_id}.log")
    with open(path, "wb") as out:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=WORK, env=_env(), stdout=out, stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL, start_new_session=True)
    _running[job_id] = (what, proc)

    async def wait() -> None:
        code = await proc.wait()
        _running.pop(job_id, None)
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL))
            tail = f.read().decode(errors="replace").strip()
        try:
            await done(job_id, code, tail)
        except Exception:
            log.exception("job %s callback failed", job_id)

    task = asyncio.create_task(wait())
    _waiters.add(task)
    task.add_done_callback(_waiters.discard)
    return job_id


def jobs() -> list[tuple[int, str]]:
    return [(i, what) for i, (what, _) in _running.items()]


def stop_job(job_id: int) -> bool:
    entry = _running.get(job_id)
    if not entry:
        return False
    _kill(entry[1])
    return True
