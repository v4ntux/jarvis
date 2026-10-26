"""Other servers Jarvis may reach over SSH, each with its own key and its own rights.

Two kinds: «status» (a key whose authorized_keys line is `restrict,command=…`: it can only run one status script, so
whatever Jarvis sends is ignored and cannot change anything: this is how the London VPS is watched), and «full» (an
ordinary key the owner added himself: commands run for real, behind the same red-list guard as any shell). A server is
never added by Jarvis: the owner puts a key on it, then names it in JARVIS_NODES.

    JARVIS_NODES='{"servers": {"london": {"target": "root@46.226.162.170", "key": "/opt/jarvis/data/monitor_key",
                                           "mode": "status"}}, "android": {"target": "100.64.0.9:5555"}}'
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger("jarvis4.ssh")

KNOWN_HOSTS = "/opt/jarvis/data/ssh_known_hosts"
MONITOR_KEY = "/opt/jarvis/data/monitor_key"
MODES = ("status", "full")


@dataclass
class Server:
    name: str
    target: str
    key: str
    mode: str = "full"

    @property
    def host(self) -> str:
        return self.target.rpartition("@")[2]


def parse_nodes(raw: str) -> dict:
    """The JARVIS_NODES setting as a dict; a broken setting is an empty one (and a line in the log), never a crash."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        log.error("JARVIS_NODES is not valid JSON, ignored")
        return {}


class SshNodes:
    def __init__(self, servers: list[Server]):
        self.servers = {s.name: s for s in servers}

    @classmethod
    def from_config(cls, nodes: dict, monitor_hosts: str = "", key_exists=os.path.exists) -> "SshNodes":
        found: dict[str, Server] = {}
        for item in monitor_hosts.split(","):  # the hosts the monitor already watches, read-only through its own key
            name, _, target = item.strip().partition("=")
            if target and key_exists(MONITOR_KEY):
                found[name.strip().lower()] = Server(name.strip().lower(), target.strip(), MONITOR_KEY, "status")
        for name, spec in (nodes.get("servers") or {}).items():
            if not isinstance(spec, dict) or not spec.get("target") or not spec.get("key"):
                log.error("server %s in JARVIS_NODES needs target and key", name)
                continue
            mode = spec.get("mode", "full")
            found[name.lower()] = Server(name.lower(), spec["target"], spec["key"], mode if mode in MODES else "status")
        return cls(list(found.values()))

    def names(self) -> list[str]:
        return sorted(self.servers)

    def describe(self) -> str:
        if not self.servers:
            return "Других серверов не подключено."
        return "\n".join(f"{s.name}: {s.target}, " + ("только смотреть состояние" if s.mode == "status" else "полный доступ")
                         for s in self.servers.values())

    async def run(self, name: str, command: str, timeout: float = 60) -> tuple[str, str]:
        """(text, mode) of what the server answered."""
        server = self.servers.get((name or "").strip().lower())
        if server is None:
            raise KeyError(f"сервера «{name}» нет. Есть: {', '.join(self.names()) or 'никаких'}")
        args = ["ssh", "-i", server.key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", server.target]
        if server.mode == "full":
            args.append(command)
        try:
            proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        except FileNotFoundError:
            return "Ошибка: на этом сервере нет ssh.", server.mode
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Ошибка: {server.name} не ответил за {int(timeout)} с.", server.mode
        text = out.decode(errors="replace").strip()
        if proc.returncode == 255 or "Permission denied" in text:
            return f"Ошибка: не вошёл на {server.name}: {text[-200:]}", server.mode
        return (f"(код {proc.returncode})\n{text}" if proc.returncode else text or "(пусто, код 0)"), server.mode
