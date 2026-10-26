"""This server (Jarvis's home) as the agent sees it: commands, background jobs, the monitor, and the free internet."""
from __future__ import annotations

from typing import Awaitable, Callable

from ..ops import shell, web


class ServerNode:
    def __init__(self, monitor=None):
        self.monitor_ = monitor  # ops.monitor.Monitor, set by the app once it exists

    async def shell(self, command: str, timeout: int = 60) -> str:
        return await shell.run(command, timeout)

    async def monitor(self) -> str:
        return self.monitor_.status() if self.monitor_ else "Мониторинг не запущен."

    async def job(self, command: str, what: str, done: Callable[[int, int, str], Awaitable[None]]) -> int:
        return await shell.start_job(command, what, done)

    def jobs(self) -> list[tuple[int, str]]:
        return shell.jobs()

    def stop_job(self, job_id: int) -> bool:
        return shell.stop_job(job_id)


class WebNode:
    async def search(self, query: str, news: bool = False) -> str:
        return await web.search(query, 6, news)

    async def fetch(self, url: str) -> str:
        return await web.fetch(url)
