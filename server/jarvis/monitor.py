"""Monitoring: the owner's sites, this server and his other VPS, checked without AI around the clock.

Jarvis speaks only when something changes: a site goes down or comes back, someone logs in to a server with a
key that is not the owner's, a new key appears in authorized_keys, a disk or the memory fills up, the other VPS
stops answering, a service or container there falls over. Bots knocking on SSH all day are only counted and
summed up once a day (fail2ban bans them meanwhile).

The other VPS is read through a key that can run one thing only: the status script london_setup.sh installs
(«restrict,command=…» in its authorized_keys), so even a confused Jarvis cannot change anything there.
"""
import asyncio
import datetime as dt
import json
import logging
import os
import re
import shutil
import socket
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from .config import TZ, Config
from .memory import Memory

log = logging.getLogger("jarvis.monitor")

TICK = 60
SITE_EVERY = 120
HOST_EVERY = 120
INSIDE_EVERY = 300
RESOURCES_EVERY = 300
CERT_EVERY = 12 * 3600
DOWN_AFTER = 2  # failed checks in a row before a site or a host counts as down
DISK_FULL = 90  # percent
MEM_LOW = 5  # percent of memory still available
CERT_DAYS = 10
DIGEST_AT = "21:00"  # the day's summary of SSH knocking, on the owner's clock
KEY = "/opt/jarvis/data/monitor_key"
KNOWN_HOSTS = "/opt/jarvis/data/monitor_known_hosts"
AUTHORIZED = os.path.expanduser("~/.ssh/authorized_keys")
UA = "Mozilla/5.0 (Jarvis uptime check)"

ACCEPTED = re.compile(r"^(\d+(?:\.\d+)?)\s.*?Accepted (\w+) for (\S+) from (\S+) port \d+\S*(?:\s+ssh2)?"
                      r"(?::\s+\S+\s+(SHA256:\S+))?")
FAILED = re.compile(r"Failed \w+ for (?:invalid user )?\S+ from (\S+)|Invalid user \S* ?from (\S+)")


@dataclass
class Target:
    """One thing that can be up or down."""
    name: str
    fails: int = 0
    down_since: float = 0.0  # when it was reported down (0: not reported)
    last: str = ""  # what the last check saw, for /monitor
    checked: float = 0.0
    extra: dict = field(default_factory=dict)


def fingerprints(path: str) -> dict[str, str]:
    """SHA256 fingerprint → comment, for every key in an authorized_keys file."""
    import subprocess

    if not os.path.exists(path):
        return {}
    out = subprocess.run(["ssh-keygen", "-lf", path], capture_output=True, text=True).stdout
    found = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("SHA256:"):
            found[parts[1]] = " ".join(parts[2:-1]) or "без подписи"
    return found


def parse_logins(lines: list[str]) -> tuple[list[tuple[str, str, str, str, str]], list[str]]:
    """(logins: (timestamp, method, user, ip, fingerprint), failed attempts: ip) from sshd log lines."""
    logins, failed = [], []
    for line in lines:
        m = ACCEPTED.search(line)
        if m:
            logins.append((m.group(1), m.group(2), m.group(3), m.group(4), m.group(5) or ""))
            continue
        f = FAILED.search(line)
        if f:
            failed.append(f.group(1) or f.group(2))
    return logins, failed


def parse_status(text: str) -> dict:
    """What the other VPS's status script printed: host, load, mem, disks, failed units, containers, log lines."""
    st = {"disk": {}, "failed": [], "down": [], "log": []}
    for line in text.splitlines():
        word, _, rest = line.partition(" ")
        if word == "host":
            st["host"] = rest.strip()
        elif word == "load":
            load, _, cpus = rest.partition(" ")
            st["load"], st["cpus"] = float(load or 0), int(cpus or 1)
        elif word == "mem":
            total, _, avail = rest.partition(" ")
            st["mem"] = (int(total), int(avail))
        elif word == "disk":
            mount, _, pct = rest.rpartition(" ")
            if pct.isdigit():
                st["disk"][mount] = int(pct)
        elif word == "failed" and rest.strip():
            st["failed"].append(rest.strip())
        elif word in ("docker", "pm2"):
            name, _, state = rest.rpartition(" ")
            if state not in ("running", "online"):
                st["down"].append(f"{word} {name}: {state}")
        elif word == "log":
            st["log"].append(rest)
        elif word == "fails24" and rest.strip().isdigit():
            st["fails24"] = int(rest)
    return st


class Monitor:
    def __init__(self, cfg: Config, memory: Memory, notify: Callable[[str, bool], Awaitable[None]]):
        self.cfg, self.memory = cfg, memory
        self.notify = notify  # notify(text, urgent): a message, or a call that rings until he answers
        self.sites: dict[str, Target] = {}
        self.hosts: dict[str, Target] = {}
        self.server = Target("этот сервер")
        self.seen_logins: dict[str, float] = {}  # log line timestamp → when seen, so a login is told once
        self.next: dict[str, float] = {}
        self.pending: list[tuple[str, bool]] = []

    # What is watched: from .env plus what the owner added in words

    def site_list(self) -> list[str]:
        added = json.loads(self.memory.meta_get("monitor_sites", "[]"))
        removed = set(json.loads(self.memory.meta_get("monitor_sites_removed", "[]")))
        env = [s.strip() for s in self.cfg.monitor_sites.split(",") if s.strip()]
        return [s for s in dict.fromkeys(env + added) if s not in removed]

    def host_list(self) -> dict[str, str]:
        """Name → user@host of the other VPS."""
        found = {}
        for item in self.cfg.monitor_hosts.split(","):
            name, _, target = item.strip().partition("=")
            if target:
                found[name.strip()] = target.strip()
        return found

    def add_site(self, url: str) -> str:
        url = url.strip()
        if not re.match(r"^https?://[\w.-]+", url):
            url = "https://" + url.lstrip("/")
        added = json.loads(self.memory.meta_get("monitor_sites", "[]"))
        removed = [s for s in json.loads(self.memory.meta_get("monitor_sites_removed", "[]")) if s != url]
        if url not in added:
            added.append(url)
        self.memory.meta_set("monitor_sites", json.dumps(added))
        self.memory.meta_set("monitor_sites_removed", json.dumps(removed))
        self.next.pop("sites", None)  # check it right away
        return f"Слежу за {url}: проверка каждые {SITE_EVERY // 60} мин, скажу, если ляжет."

    def remove_site(self, url: str) -> str:
        wanted = url.strip().rstrip("/")
        hits = [s for s in self.site_list() if s.rstrip("/") == wanted or wanted in s]
        if not hits:
            return f"Ошибка: за {url} я не слежу."
        added = [s for s in json.loads(self.memory.meta_get("monitor_sites", "[]")) if s not in hits]
        removed = list(dict.fromkeys(json.loads(self.memory.meta_get("monitor_sites_removed", "[]")) + hits))
        self.memory.meta_set("monitor_sites", json.dumps(added))
        self.memory.meta_set("monitor_sites_removed", json.dumps(removed))
        for s in hits:
            self.sites.pop(s, None)
        return "Больше не слежу за " + ", ".join(hits) + "."

    # The loop

    async def loop(self) -> None:
        await asyncio.sleep(20)  # let the account come up first
        self._known_keys()
        while True:
            for name, every, check in (("sites", SITE_EVERY, self._check_sites), ("logins", TICK, self._check_logins),
                                       ("resources", RESOURCES_EVERY, self._check_resources),
                                       ("hosts", HOST_EVERY, self._check_hosts), ("certs", CERT_EVERY, self._check_certs),
                                       ("digest", TICK, self._digest)):
                if self.next.get(name, 0) <= time.time():
                    self.next[name] = time.time() + every
                    try:
                        await check()
                    except Exception:
                        log.exception("monitor check %s failed", name)
            await self._flush()
            await asyncio.sleep(TICK)

    def _say(self, text: str, urgent: bool = False) -> None:
        self.pending.append((text, urgent))

    async def _flush(self) -> None:
        """Everything one pass found goes out as one message (Telegram counts every message); urgent ones ring."""
        if not self.pending:
            return
        items, self.pending = self.pending, []
        urgent = [t for t, u in items if u]
        calm = [t for t, u in items if not u]
        if urgent:
            await self.notify("\n\n".join(urgent), True)
        if calm:
            await self.notify("\n".join(calm), False)

    def _went(self, target: Target, ok: bool, what: str, why: str) -> None:
        """Counts failures and says so on the switch: down after DOWN_AFTER fails, up again on the first success."""
        target.checked, target.last = time.time(), why
        if ok:
            if target.down_since:
                minutes = max(1, round((time.time() - target.down_since) / 60))
                self._say(f"🟢 {what} снова работает (лежал ~{minutes} мин).")
            target.fails, target.down_since = 0, 0.0
            return
        target.fails += 1
        if target.fails == DOWN_AFTER:
            target.down_since = time.time()
            self._say(f"🔴 {what} не отвечает: {why}")

    # Sites

    async def _check_sites(self) -> None:
        urls = self.site_list()
        for url in list(self.sites):
            if url not in urls:
                self.sites.pop(url)
        async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": UA}) as client:
            for url in urls:
                target = self.sites.setdefault(url, Target(url))
                started = time.time()
                try:
                    r = await client.get(url)
                    ok, why = r.status_code < 500, f"HTTP {r.status_code}, {time.time() - started:.1f} с"
                except httpx.HTTPError as e:
                    ok, why = False, f"{type(e).__name__} {e}".strip()[:150]
                self._went(target, ok, f"Сайт {url}", why)

    async def _check_certs(self) -> None:
        for url in self.site_list():
            if not url.startswith("https://"):
                continue
            host = re.sub(r"^https://([^/:]+).*$", r"\1", url)
            try:
                days = await asyncio.to_thread(_cert_days, host)
            except (OSError, ssl.SSLError, ValueError) as e:
                log.info("certificate of %s: %s", host, e)
                continue
            target = self.sites.setdefault(url, Target(url))
            target.extra["cert_days"] = days
            if days < CERT_DAYS:
                self._say(f"⚠️ У {host} сертификат HTTPS кончается через {days} дн.")

    # This server: who logs in, disk, memory

    def _known_keys(self) -> dict[str, str]:
        """The keys in authorized_keys; a change since the last look is news (somebody gave himself access)."""
        now = fingerprints(AUTHORIZED)
        before = json.loads(self.memory.meta_get("monitor_keys", "null") or "null")
        if before is not None:
            for fp, comment in now.items():
                if fp not in before:
                    # Every key there is trusted from now on, so a new one is worth a call
                    self._say(f"🚨 На главном сервере в authorized_keys появился новый ключ: {comment} ({fp}). С ним "
                              "можно войти без пароля. Если это не ты — скажи мне, уберу.", urgent=True)
            for fp, comment in before.items():
                if fp not in now:
                    self._say(f"🔑 Из authorized_keys сервера убрали ключ {comment} ({fp}).")
        self.memory.meta_set("monitor_keys", json.dumps(now))
        return now

    async def _check_logins(self) -> None:
        last = float(self.memory.meta_get("monitor_ssh_since", "0") or 0) or time.time() - 120
        # Jarvis's user is in the systemd-journal group: no sudo, so no sudo lines in the log every minute
        out = await _run(["journalctl", "-u", "ssh", "-u", "sshd", "--since", f"@{int(last) - 1}",
                          "-o", "short-unix", "--no-pager"], 30)
        logins, _ = parse_logins(out.splitlines())
        known = self._known_keys()
        for ts, method, user, ip, fp in logins:
            if ts in self.seen_logins:
                continue
            self.seen_logins[ts] = time.time()
            if fp and fp in known:
                continue  # the owner's own machines (the laptop, Jarvis's own tunnel)
            how = f"по ключу {fp}" if fp else f"способом {method}"
            self._say(f"🚨 На главный сервер вошли: пользователь {user}, IP {ip}, {how}. Это не твой ключ. "
                      "Если это не ты — скажи «закрой сервер», и я разберусь.", urgent=True)
        if logins:
            newest = max(float(ts) for ts, *_ in logins)
            self.memory.meta_set("monitor_ssh_since", str(max(newest, last)))
        else:
            self.memory.meta_set("monitor_ssh_since", str(max(last, time.time() - 120)))
        cutoff = time.time() - 1800  # longer than any log window asked for (the other VPS sends 15 minutes)
        self.seen_logins = {ts: seen for ts, seen in self.seen_logins.items() if seen > cutoff}

    async def _check_resources(self) -> None:
        usage = shutil.disk_usage("/")
        disk = round(usage.used / usage.total * 100)
        mem_total, mem_avail = _meminfo()
        mem_free = round(mem_avail / mem_total * 100) if mem_total else 100
        load, cpus = os.getloadavg()[0], os.cpu_count() or 1
        self.server.last = f"диск {disk}%, свободно памяти {mem_free}%, нагрузка {load:.1f} на {cpus} ядра"
        self.server.checked = time.time()
        self._edge(self.server, "disk", disk >= DISK_FULL, f"⚠️ На главном сервере диск заполнен на {disk}%.",
                   "Диск главного сервера снова в норме.")
        self._edge(self.server, "mem", mem_free < MEM_LOW, f"⚠️ На главном сервере кончается память: свободно {mem_free}%.",
                   "Память главного сервера снова в норме.")
        self._edge(self.server, "load", load > cpus * 3, f"⚠️ Главный сервер перегружен: нагрузка {load:.1f} на {cpus} ядра.",
                   "Нагрузка главного сервера спала.")

    def _edge(self, target: Target, key: str, bad: bool, alarm: str, fine: str) -> None:
        """Says so once when a condition turns bad and once when it is over, not on every check in between."""
        was = target.extra.get(key, False)
        if bad and not was:
            self._say(alarm)
        elif was and not bad:
            self._say("✅ " + fine)
        target.extra[key] = bad

    async def _digest(self) -> None:
        """Once a day: how many tried to get into the servers, and how many fail2ban turned away."""
        now = dt.datetime.now(TZ)
        today = now.strftime("%Y-%m-%d")
        if now.strftime("%H:%M") < DIGEST_AT or self.memory.meta_get("monitor_digest") == today:
            return
        self.memory.meta_set("monitor_digest", today)
        out = await _run(["journalctl", "-u", "ssh", "-u", "sshd", "--since", "-24h", "-o", "short-unix", "--no-pager"],
                         60)
        logins, failed = parse_logins(out.splitlines())
        banned = await _run(["sudo", "-n", "fail2ban-client", "status", "sshd"], 20)
        total = re.search(r"Total banned:\s*(\d+)", banned)
        known = self._known_keys()
        strangers = [ip for *_, ip, fp in logins if fp not in known]
        lines = [f"📊 Главный сервер за сутки: {len(failed)} попыток подобрать вход с {len(set(failed))} адресов, "
                 f"входов {len(logins)}" + (f", из них чужих: {len(strangers)} ({', '.join(sorted(set(strangers)))})."
                                           if strangers else ", все твоими ключами.")]
        if total:
            lines.append(f"fail2ban всего заблокировал адресов: {total.group(1)}.")
        for name, target in self.hosts.items():
            if "fails24" in target.extra:
                lines.append(f"{name}: {target.extra['fails24']} попыток подобрать вход за сутки.")
        self._say(" ".join(lines))

    # The other VPS

    async def _check_hosts(self) -> None:
        for name, target_str in self.host_list().items():
            target = self.hosts.setdefault(name, Target(name))
            host = target_str.rpartition("@")[2]
            started = time.time()
            try:
                _, writer = await asyncio.wait_for(asyncio.open_connection(host, 22), 10)
                writer.close()
                ok, why = True, f"SSH отвечает за {time.time() - started:.1f} с"
            except (OSError, asyncio.TimeoutError) as e:
                ok, why = False, f"SSH (порт 22) не отвечает: {type(e).__name__}"
            self._went(target, ok, f"Сервер {name} ({host})", why)
            if ok and time.time() - target.extra.get("inside_at", 0) >= INSIDE_EVERY:
                target.extra["inside_at"] = time.time()
                await self._look_inside(name, target_str, target)

    async def _look_inside(self, name: str, target_str: str, target: Target) -> None:
        if not os.path.exists(KEY):
            target.extra["access"] = "нет ключа мониторинга на этом сервере"
            return
        out = await _run(["ssh", "-i", KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                          "-o", "StrictHostKeyChecking=accept-new", "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
                          target_str], 40)
        if "Permission denied" in out or not out.startswith("host "):
            target.extra["access"] = "нет доступа: ключ Джарвиса там ещё не добавлен (/monitor покажет команду)"
            return
        target.extra["access"] = "есть"
        st = parse_status(out)
        cpus = st.get("cpus", 1)
        total, avail = st.get("mem", (0, 0))
        mem_free = round(avail / total * 100) if total else 100
        full = {m: p for m, p in st["disk"].items() if p >= DISK_FULL}
        target.last = (f"нагрузка {st.get('load', 0):.1f} на {cpus} ядра, свободно памяти {mem_free}%, диски: "
                       + ", ".join(f"{m} {p}%" for m, p in st["disk"].items()))
        self._edge(target, "disk", bool(full), f"⚠️ {name}: диск заполнен: " +
                   ", ".join(f"{m} {p}%" for m, p in full.items()), f"{name}: диски снова в норме.")
        self._edge(target, "mem", mem_free < MEM_LOW, f"⚠️ {name}: кончается память, свободно {mem_free}%.",
                   f"{name}: память снова в норме.")
        broken = sorted(st["failed"] + st["down"])
        before = set(target.extra.get("broken", []))
        for item in broken:
            if item not in before:
                self._say(f"🔴 {name}: упало {item}.")
        for item in before - set(broken):
            self._say(f"🟢 {name}: {item} снова работает.")
        target.extra["broken"] = broken
        logins, _ = parse_logins(st["log"])
        if "fails24" in st:
            target.extra["fails24"] = st["fails24"]
        mine = fingerprints(AUTHORIZED) | fingerprints(KEY + ".pub")  # the owner's machines and this check itself
        for ts, method, user, ip, fp in logins:
            key = f"{name}:{ts}"
            if key in self.seen_logins or (fp and fp in mine):
                continue
            self.seen_logins[key] = time.time()
            how = f"по ключу {fp}" if fp else ("по паролю" if method == "password" else method)
            self._say(f"🔑 Вход на {name}: {user} с IP {ip}, {how}.")

    def status(self) -> str:
        """For /monitor and the monitor_status tool."""
        lines = ["📡 Мониторинг"]
        for url in self.site_list():
            t = self.sites.get(url)
            state = "ещё не проверял" if not t or not t.checked else ("🔴 лежит" if t.down_since else "🟢") + f" {t.last}"
            cert = f", сертификат ещё {t.extra['cert_days']} дн." if t and "cert_days" in t.extra else ""
            lines.append(f"• {url}: {state}{cert}")
        lines.append(f"• Главный сервер: {self.server.last or 'ещё не проверял'}; входы чужими ключами — звоню сразу, "
                     f"сводка попыток взлома — каждый день в {DIGEST_AT}")
        for name, target_str in self.host_list().items():
            t = self.hosts.get(name)
            state = "ещё не проверял" if not t or not t.checked else ("🔴 не отвечает" if t.down_since else "🟢") + \
                f" {t.last}"
            access = t.extra.get("access", "") if t else ""
            lines.append(f"• {name} ({target_str}): {state}" + (f"; изнутри: {access}" if access else ""))
            if access.startswith("нет доступа") or access.startswith("нет ключа"):
                lines.append("  Чтобы я видел, что внутри (сервисы, docker, pm2, диски, входы), один раз выполни на "
                             f"ноутбуке и введи пароль сервера:\n  {setup_command(target_str)}")
        return "\n".join(lines)


def setup_command(target_str: str) -> str:
    return (f'Get-Content D:\\code\\vibe\\jarvis\\server\\monitor\\london_setup.sh -Raw | ssh {target_str} '
            '"tr -d \'\\r\' | sh"')


def _cert_days(host: str) -> int:
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=15) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    expires = dt.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    return (expires - dt.datetime.now(dt.timezone.utc)).days


def _meminfo() -> tuple[int, int]:
    values = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                values[key] = int(rest.split()[0])
    except OSError:
        return 0, 0
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


async def _run(args: list[str], timeout: float) -> str:
    """Output of a command (stdout and stderr together); a timeout or a missing program is just text."""
    try:
        proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        return f"не запустилось: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "таймаут"
    return out.decode(errors="replace")
