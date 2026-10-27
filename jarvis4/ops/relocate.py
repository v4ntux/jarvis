"""Moving a project from another server onto this one: look, check, back up, stop the old, copy, start the new, verify.

The order is the safety. Nothing on the old server changes before every check here has passed and the database dump is on
disk. The old project is stopped (never deleted) before the new one starts: two bots polling one token fight, and two
cloudflared connectors split the traffic. The archive of the whole folder is taken cold, after the stop, and kept. If the
new project does not come up healthy, the old one is started again. Nothing here prints or reads a secret: .env files are
only packed, and what docker prints is parsed in memory and dropped.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

PROJECT = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
# The laptop's VPN and Jarvis himself are never moved
PROTECTED = ("amnezia", "awg", "wireguard", "openvpn", "vpn", "jarvis")
COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
SECRET = re.compile(r"(?:\d{6,}:)?[A-Za-z0-9+/_=-]{32,}")  # bot tokens, keys and hashes in a log line
BOX_FORMAT = '{{.Label "com.docker.compose.service"}}|{{.Names}}|{{.State}}|{{.Status}}'
DOCKER_HINT = "sudo apt-get install -y docker.io docker-compose-v2 (Ubuntu); в группу docker добавлять не нужно, я работаю через sudo"
# Binds that are the machine's own, not the project's data
SYSTEM_BINDS = ("/var/run/docker.sock", "/run/docker.sock", "/etc/localtime", "/etc/timezone", "/proc", "/sys", "/dev")
SETTLE = 10  # seconds the new project must keep running after it first looks healthy: a crash loop shows here


class Old(Protocol):
    """The server the project lives on now: real commands, and files pulled to this machine."""

    async def run(self, command: str, timeout: float = 60) -> tuple[int, str]: ...
    async def fetch(self, command: str, dest: str, timeout: float = 1800) -> tuple[int, str]: ...


class Here(Protocol):
    """This server."""

    async def run(self, command: str, timeout: float = 60) -> tuple[int, str]: ...


class Refused(Exception):
    """The move cannot even be looked at: the text says why."""


@dataclass
class Box:
    service: str
    name: str
    state: str
    status: str

    @property
    def fine(self) -> bool:
        low = self.status.lower()
        if self.state == "running":
            return not any(word in low for word in ("unhealthy", "health: starting", "restarting"))
        return self.state == "exited" and low.startswith("exited (0)")  # a one-off job that finished


@dataclass
class Facts:
    project: str
    path: str
    size_kb: int = 0
    files: int = 0
    compose_file: str = ""
    has_env: bool = False
    procs: int = 0  # processes whose command line mentions the folder
    units: list[tuple[str, str]] = field(default_factory=list)  # (systemd unit file, its user) that mention the folder
    name: str = ""  # the compose project name: it names the networks, so it must stay the same
    services: dict = field(default_factory=dict)  # service -> {"image", "build", "profiles"}
    externals: list[str] = field(default_factory=list)  # networks that must already exist
    ports: list[int] = field(default_factory=list)  # published on the host
    container_names: list[str] = field(default_factory=list)
    own_networks: list[tuple[str, str]] = field(default_factory=list)  # (key, explicit name) of the networks it creates
    neighbors: list[str] = field(default_factory=list)  # containers of OTHER projects attached to its networks
    boxes: list[Box] = field(default_factory=list)  # what runs there now

    @property
    def kind(self) -> str:
        return "compose" if self.compose_file else "files"

    @property
    def compose_dir(self) -> str:
        return os.path.dirname(self.compose_file)

    @property
    def wanted(self) -> list[str]:
        """Services that must be up: those without a profile."""
        return [n for n, s in self.services.items() if not s.get("profiles")]

    @property
    def databases(self) -> list[str]:
        return [n for n, s in self.services.items() if re.search(r"(^|/)postgres(:|$)", s.get("image", ""))]


@dataclass
class Check:
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    docker: str = "docker"
    sudo: str = ""

    @property
    def ready(self) -> bool:
        return not self.blockers


@dataclass
class Outcome:
    ok: bool
    text: str
    moved: bool = False  # the project now runs here
    evidence: str = ""


def mask(text: str) -> str:
    return SECRET.sub("***", text)


def q(value: str) -> str:
    return shlex.quote(value)


def parse_boxes(text: str) -> list[Box]:
    boxes = []
    for line in text.splitlines():
        parts = [p.strip() for p in line.split("|", 3)]
        if len(parts) == 4 and parts[1]:
            boxes.append(Box(*parts))
    return boxes


def parse_config(text: str) -> dict:
    """What matters from `docker compose config --format json`; raises ValueError when it is not that."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("not a mapping")
    services, ports, names = {}, [], []
    for name, spec in (data.get("services") or {}).items():
        spec = spec or {}
        volumes = [(str(v.get("type") or ""), str(v.get("source") or "")) for v in spec.get("volumes") or []
                   if isinstance(v, dict)]
        services[name] = {"image": str(spec.get("image") or ""), "build": "build" in spec,
                          "profiles": bool(spec.get("profiles")), "restart": str(spec.get("restart") or ""),
                          "volumes": volumes}
        for port in spec.get("ports") or []:
            published = port.get("published") if isinstance(port, dict) else None
            if published is not None and str(published).isdigit():
                ports.append(int(published))
        if spec.get("container_name"):
            names.append(str(spec["container_name"]))
    externals, own = [], []
    for key, net in (data.get("networks") or {}).items():
        net = net if isinstance(net, dict) else {}
        if net.get("external"):
            externals.append(str(net.get("name") or key))
        else:
            own.append((str(key), str(net.get("name") or "")))
    return {"name": str(data.get("name") or ""), "services": services, "externals": externals, "ports": ports,
            "container_names": names, "own_networks": own}


def inspect_script(path: str) -> str:
    """One look at the project folder on the old server: what is in it and which files describe how it runs."""
    d = q(path)
    compose = " ".join(q(f"{path}/{n}") for n in COMPOSE_NAMES) + " " + " ".join(q(f"{path}/deploy/{n}") for n in COMPOSE_NAMES)
    return "\n".join([
        f"d={d}",
        'test -d "$d" || { echo MISSING; exit 3; }',
        f'for f in {compose}; do if [ -f "$f" ]; then echo "COMPOSE $f"; break; fi; done',
        'echo "SIZEKB $(du -sk "$d" | cut -f1)"',
        'echo "FILES $(find "$d" | wc -l)"',
        f'echo "PROCS $(pgrep -af -- {q("[/]" + path[1:])} | grep -vc pgrep)"',
        'if [ -f "$d/.env" ] || [ -f "$d/deploy/.env" ]; then echo ENV yes; fi',
        'for u in $(grep -ls -- "$d" /etc/systemd/system/*.service /lib/systemd/system/*.service 2>/dev/null); do '
        'echo "UNIT $u $(grep -h \'^User=\' "$u" | head -1 | cut -d= -f2)"; done',
    ])


def parse_inspect(project: str, path: str, text: str) -> Facts:
    facts = Facts(project, path)
    for line in text.splitlines():
        key, _, rest = line.strip().partition(" ")
        if key == "COMPOSE":
            facts.compose_file = rest.strip()
        elif key == "SIZEKB" and rest.strip().isdigit():
            facts.size_kb = int(rest.strip())
        elif key == "FILES" and rest.strip().isdigit():
            facts.files = int(rest.strip())
        elif key == "PROCS" and rest.strip().isdigit():
            facts.procs = int(rest.strip())
        elif key == "ENV":
            facts.has_env = True
        elif key == "UNIT":
            unit, _, user = rest.partition(" ")
            facts.units.append((unit.strip(), user.strip() or "root"))
    return facts


class Relocator:
    def __init__(self, old: Old, here: Here, project: str, *, server: str = "", base: str = "/opt",
                 backups: str = "/opt/jarvis/backup/migrate", note=None, aborted: Callable[[], bool] | None = None,
                 sleep=asyncio.sleep, now=time.time):
        self.old, self.here = old, here
        self.project, self.server = project, server or "старый сервер"
        self.base, self.backups = base, backups
        self.note: Callable[[str, bool, str], Awaitable[None]] = note or _no_note
        self.aborted = aborted or (lambda: False)
        self.sleep, self.now = sleep, now
        self.folder = ""  # where this move keeps its dump, archive and state
        self.state: dict = {}

    @property
    def path(self) -> str:
        return f"{self.base}/{self.project}"

    # ---- looking --------------------------------------------------------------------------------------------------

    async def inspect(self) -> Facts:
        if not PROJECT.fullmatch(self.project or ""):
            raise Refused(f"имя проекта «{self.project}» не годится: нужна папка в {self.base}, строчные буквы, цифры, - _ .")
        if any(word in self.project for word in PROTECTED):
            raise Refused(f"«{self.project}» я не переношу: это VPN ноутбука или я сам. Такое пусть двигает Ван руками.")
        code, text = await self.old.run(inspect_script(self.path), 120)
        if code == 3 or text.startswith("MISSING"):
            raise Refused(f"на {self.server} нет папки {self.path}")
        if code != 0:
            raise Refused(f"не смог осмотреть {self.path} на {self.server}: {mask(text)[-200:]}")
        facts = parse_inspect(self.project, self.path, text)
        if facts.compose_file:
            await self._read_compose(facts)
        return facts

    async def _read_compose(self, facts: Facts) -> None:
        d, f = q(facts.compose_dir), q(facts.compose_file)
        base = f"docker compose -f {f} config --no-interpolate"
        code, text = await self.old.run(f"cd {d} && {{ {base} --no-env-resolution --format json 2>/dev/null || "
                                        f"{base} --format json 2>/dev/null; }}", 60)
        try:
            if code != 0:
                raise ValueError(f"код {code}")
            parsed = parse_config(text)
        except ValueError as e:  # never quote the text: it may hold the project's variables
            raise Refused(f"не смог прочитать compose-файл {facts.compose_file} на {self.server} ({e})") from None
        facts.services, facts.externals = parsed["services"], parsed["externals"]
        facts.ports, facts.container_names = parsed["ports"], parsed["container_names"]
        facts.own_networks = parsed["own_networks"]
        code, text = await self.old.run(
            f"docker ps -a --filter label=com.docker.compose.project.working_dir={q(facts.compose_dir)} "
            f"--format '{{{{.Label \"com.docker.compose.project\"}}}}' | sort -u | head -1", 30)
        facts.name = text.strip().splitlines()[0].strip() if code == 0 and text.strip() else parsed["name"]
        facts.name = facts.name or os.path.basename(facts.compose_dir.rstrip("/"))
        facts.boxes = await self._boxes(self.old, facts.name, "docker")
        await self._neighbors(facts)

    async def _neighbors(self, facts: Facts) -> None:
        """Containers of other projects that sit in this project's networks: they lose it when it moves."""
        for key, explicit in facts.own_networks or [("default", "")]:
            net = explicit or f"{facts.name}_{key}"
            code, text = await self.old.run(
                f"docker ps -a --filter network={q(net)} "
                f"--format '{{{{.Label \"com.docker.compose.project\"}}}}|{{{{.Names}}}}'", 30)
            for line in text.splitlines() if code == 0 else []:
                project, _, container = line.partition("|")
                if container and project != facts.name and container not in facts.neighbors:
                    facts.neighbors.append(container)

    async def _boxes(self, where: Old | Here, name: str, docker: str) -> list[Box]:
        code, text = await where.run(f"{docker} ps -a --filter label=com.docker.compose.project={q(name)} "
                                     f"--format {q(BOX_FORMAT)}", 30)
        return parse_boxes(text) if code == 0 else []

    async def preflight(self, facts: Facts) -> Check:
        """Everything that can be known about this server before the old project is touched."""
        check = Check()
        _, uid = await self.here.run("id -u", 10)
        if uid.strip() != "0":
            code, _ = await self.here.run("sudo -n true", 10)
            check.sudo = "sudo -n "
            if code != 0:
                check.blockers.append("нужен sudo без пароля: распаковать так, чтобы сохранились владельцы файлов "
                                      "(иначе базе не хватит прав на свои файлы)")
        if facts.kind == "compose":
            code, kind = await self.here.run("docker info >/dev/null 2>&1 && echo plain || "
                                             "{ sudo -n docker info >/dev/null 2>&1 && echo sudo || echo none; }", 60)
            kind = kind.strip().splitlines()[-1] if kind.strip() else "none"
            if code != 0 or kind == "none":
                check.blockers.append(f"на этом сервере нет рабочего docker. Поставить: {DOCKER_HINT}")
            else:
                check.docker = "docker" if kind == "plain" else "sudo -n docker"
                code, _ = await self.here.run(f"{check.docker} compose version --short", 30)
                if code != 0:
                    check.blockers.append(f"нет плагина docker compose. Поставить: {DOCKER_HINT}")
                else:
                    await self._preflight_docker(facts, check)
        os.makedirs(self.backups, exist_ok=True)
        need = facts.size_kb * 2 + 1024 * 1024
        for where, why in ((self.base, "для проекта"), (self.backups, "для бэкапа")):
            code, free = await self.here.run(f"df -Pk {q(where)} | awk 'NR==2{{print $4}}'", 10)
            if code == 0 and free.strip().isdigit() and int(free) < need:
                check.blockers.append(f"мало места {why} ({where}): свободно {int(free) // 1024} МБ, нужно {need // 1024} МБ")
        code, there = await self.here.run(f"test -e {q(self.path)} && echo EXISTS || echo FREE", 10)
        if "EXISTS" in there:
            check.blockers.append(f"{self.path} здесь уже есть: поверх я ничего не раскладываю")
        if facts.kind == "compose":
            self._compose_checks(facts, check)
        if facts.kind == "files" and facts.procs and not facts.units:
            check.blockers.append(f"в {facts.path} работают процессы ({facts.procs}), и это не служба systemd: остановить "
                                  "их безопасно я не умею, останови сам (bot, screen, cron) и повтори")
        if facts.kind == "files":
            check.warnings.append("это не docker compose: файлы и бэкап сделаю сам, а запустить здесь (пользователь, "
                                  "окружение, служба systemd) придётся отдельными шагами, и я их назову в конце")
        if not facts.has_env:
            check.warnings.append("файла .env у проекта нет: если он живёт в другом месте, перенеси его отдельно")
        for name in facts.databases:
            image = facts.services[name]["image"]
            if ":" not in image or image.endswith(":latest"):
                check.warnings.append(f"образ базы {name} ({image}) без точной версии: здесь может подтянуться другая "
                                      "версия, а файлы базы чужую не откроют")
        return check

    def _compose_checks(self, facts: Facts, check: Check) -> None:
        named, outside = set(), set()
        for service, spec in facts.services.items():
            for kind, source in spec.get("volumes", []):
                if kind == "volume" and source:
                    named.add(f"{source} ({service})")
                elif kind == "bind" and source and not (source == facts.path or source.startswith(facts.path + "/")) \
                        and not source.startswith(SYSTEM_BINDS):
                    outside.add(f"{source} ({service})")
        if named:
            check.blockers.append("данные лежат в docker-томах (" + ", ".join(sorted(named)) + "): они вне папки проекта, "
                                  "переносить тома я не умею, и без них проект здесь стартует пустым")
        if outside:
            check.blockers.append("часть файлов лежит вне папки проекта (" + ", ".join(sorted(outside)) + "): с папкой "
                                  "они не поедут")
        always = [n for n, spec in facts.services.items() if spec.get("restart") == "always"]
        if always:
            check.warnings.append(f"у сервисов {', '.join(always)} стоит restart: always: после перезагрузки {self.server} "
                                  "старое поднимется само. Сменить на unless-stopped или убрать старое, когда всё проверено")
        if facts.neighbors:
            check.warnings.append("к сетям проекта подключены контейнеры других проектов (" + ", ".join(facts.neighbors) +
                                  f"): они остаются на {self.server} и потеряют связь с ним. Их переноси сразу следом")

    async def _preflight_docker(self, facts: Facts, check: Check) -> None:
        d = check.docker
        for net in facts.externals:
            code, _ = await self.here.run(f"{d} network inspect {q(net)} >/dev/null 2>&1", 30)
            if code != 0:
                check.blockers.append(f"здесь нет docker-сети «{net}»: её создаёт другой проект, его нужно перенести раньше")
        code, listening = await self.here.run("ss -ltn | awk 'NR>1{print $4}'", 10)
        taken = {int(p.rsplit(":", 1)[1]) for p in listening.split() if p.rsplit(":", 1)[-1].isdigit()} if code == 0 else set()
        for port in sorted(set(facts.ports) & taken):
            check.blockers.append(f"порт {port} на этом сервере уже занят")
        code, text = await self.here.run(f"{d} ps -a --format '{{{{.Label \"com.docker.compose.project\"}}}}|{{{{.Names}}}}'", 30)
        for line in text.splitlines() if code == 0 else []:
            project, _, container = line.partition("|")
            if project == facts.name:
                check.blockers.append(f"здесь уже есть контейнеры проекта «{facts.name}» ({container})")
                break
            if container in facts.container_names:
                check.blockers.append(f"здесь уже есть контейнер с именем «{container}»")

    async def plan(self) -> tuple[Facts, Check]:
        facts = await self.inspect()
        return facts, await self.preflight(facts)

    def describe(self, facts: Facts, check: Check) -> str:
        lines = [f"Проект «{facts.project}» на {self.server}: {facts.path}, {facts.size_kb // 1024} МБ, {facts.files} файлов, "
                 + ("docker compose" if facts.kind == "compose" else "обычная папка (не docker compose)")]
        if facts.kind == "compose":
            lines.append("Сервисы: " + ", ".join(f"{n} ({s['image'] or 'сборка'})" for n, s in facts.services.items()))
            running = [b for b in facts.boxes if b.state == "running"]
            lines.append(f"Сейчас там работает: {len(running)} из {len(facts.boxes)} контейнеров"
                         + (f" (compose-проект «{facts.name}»)" if facts.name else ""))
            if facts.externals:
                lines.append("Внешние сети: " + ", ".join(facts.externals))
        for unit, user in facts.units:
            lines.append(f"Служба systemd: {unit} (запускается от {user})")
        for text in check.warnings:
            lines.append("⚠ " + text)
        if check.blockers:
            lines.append("Не могу переносить, пока не исправлено:")
            lines += ["✗ " + text for text in check.blockers]
        else:
            lines.append("Проверки на этом сервере пройдены. Порядок: дамп базы → остановить старое → архив папки "
                         "→ проверка архива → распаковать здесь → запустить → проверить, что всё работает. "
                         "Старое не удаляю; не поднимется — запускаю старое обратно.")
        return "\n".join(lines)

    # ---- moving ---------------------------------------------------------------------------------------------------

    async def move(self, facts: Facts, check: Check) -> Outcome:
        if not check.ready:
            return Outcome(False, "Не переношу: " + "; ".join(check.blockers))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        self.folder = os.path.join(self.backups, f"{self.project}-{stamp}")
        os.makedirs(self.folder, mode=0o700, exist_ok=True)  # the archive holds the project's .env
        self.state = {"server": self.server, "project": self.project, "base": self.base, "kind": facts.kind,
                      "compose_file": facts.compose_file, "compose_dir": facts.compose_dir, "name": facts.name,
                      "units": [u for u, _ in facts.units], "docker": check.docker, "sudo": check.sudo,
                      "phase": "checked", "old_touched": False, "new_touched": False, "extracted": False}
        self._save()
        dumps = await self._dump(facts)
        if dumps is not None:
            return dumps
        if self.aborted():
            return Outcome(False, "Остановился до того, как что-то менять: на старом сервере всё как было.")

        stopped = await self._stop_old(facts)
        if stopped is not None:
            return stopped
        try:
            return await self._copy_and_start(facts, check)
        except asyncio.CancelledError:
            await self._back("прервано")
            raise
        except Exception as e:  # a bug here must not leave the old project stopped
            return await self._fail(facts, f"сбой внутри переноса ({type(e).__name__}): {mask(str(e))[:200]}")

    async def _dump(self, facts: Facts) -> Outcome | None:
        """The databases are dumped while the project still runs; a dump that fails stops the move before any change."""
        if facts.kind != "compose":
            return None
        running = {b.service for b in facts.boxes if b.state == "running"}
        for service in facts.databases:
            if service not in running:
                await self.note("dump", True, f"база {service} не запущена, дамп невозможен: останется копия папки")
                continue
            dest = os.path.join(self.folder, f"{service}.sql")
            command = (f"cd {q(facts.compose_dir)} && docker compose -p {q(facts.name)} -f {q(facts.compose_file)} exec -T "
                       f"{q(service)} sh -c 'pg_dumpall -U \"${{POSTGRES_USER:-postgres}}\"'")
            code, err = await self.old.fetch(command, dest, 1200)
            tail = _tail(dest)
            if code != 0 or "PostgreSQL database cluster dump complete" not in tail:
                await self.note("dump", False, f"дамп {service} не получился: {mask(err)[-160:] or 'файл неполный'}")
                return Outcome(False, f"Дамп базы {service} не получился, поэтому я ничего не трогал: старое работает как "
                                      f"работало. {mask(err)[-160:]}".strip())
            await self.note("dump", True, f"дамп базы {service}: {os.path.getsize(dest) // 1024} КБ, цел")
        return None

    async def _stop_old(self, facts: Facts) -> Outcome | None:
        self.state["phase"] = "stopping_old"
        self.state["old_touched"] = True
        self._save()
        if facts.kind == "compose":
            command = (f"cd {q(facts.compose_dir)} && docker compose -p {q(facts.name)} -f {q(facts.compose_file)} stop && "
                       f"docker ps -q --filter label=com.docker.compose.project={q(facts.name)} | wc -l")
            timeout = 300
        else:
            units = " ".join(q(os.path.basename(u)) for u, _ in facts.units)
            command = (f"{'systemctl stop ' + units + ' && ' if units else ''}"
                       f"pgrep -af -- {q('[/]' + self.path[1:])} | grep -vc pgrep || true")
            timeout = 120
        code, text = await self.old.run(command, timeout)
        left = text.strip().splitlines()[-1].strip() if text.strip() else ""
        if code != 0 or left != "0":
            await self._back("старое не остановилось")
            return Outcome(False, f"Старое на {self.server} не остановилось ({mask(text)[-160:]}). Запустил его обратно "
                                  "и ничего дальше не делал.")
        self.state["phase"] = "old_stopped"
        self._save()
        await self.note("stop", True, f"остановил {self.project} на {self.server} (не удалял)")
        return None

    async def _copy_and_start(self, facts: Facts, check: Check) -> Outcome:
        if self.aborted():
            return await self._fail(facts, "получена команда «стоп»")
        code, count = await self.old.run(f"find {q(self.path)} | wc -l", 120)
        remote = _last_int(count) if code == 0 else 0
        archive = os.path.join(self.folder, f"{self.project}.tar.gz")
        code, err = await self.old.fetch(f"tar --numeric-owner -czf - -C {q(self.base)} {q(self.project)}", archive, 3600)
        if code != 0:
            return await self._fail(facts, f"архив не снялся (код {code}: {mask(err)[-160:]})")
        self.state["archive"] = archive
        self.state["phase"] = "archived"
        self._save()
        listing = os.path.join(self.folder, "files.txt")
        pattern = self.project.replace(".", "[.]")
        code, text = await self.here.run(
            f"gzip -t {q(archive)} && tar -tzf {q(archive)} > {q(listing)} && echo \"COUNT $(wc -l < {q(listing)})\" && "
            f"echo \"BAD $(grep -cE '(^|/)\\.\\.(/|$)|^/' {q(listing)})\" && "
            f"echo \"OUT $(grep -vcE '^{pattern}(/|$)' {q(listing)})\"", 1800)
        got = dict(line.split(" ", 1) for line in text.splitlines() if line.startswith(("COUNT ", "BAD ", "OUT ")))
        try:
            entries, bad, out = int(got["COUNT"]), int(got["BAD"]), int(got["OUT"])
        except (KeyError, ValueError):
            return await self._fail(facts, f"архив не прошёл проверку (gzip/tar: {mask(text)[-160:]})")
        if code != 0 or bad or out:
            return await self._fail(facts, "в архиве пути вне папки проекта или с «..»: ничего не распаковываю")
        if remote and abs(entries - remote) > max(3, remote // 100):
            return await self._fail(facts, f"в архиве {entries} записей, в папке на {self.server} {remote}: не сходится")
        await self.note("archive", True, f"архив {self.project}.tar.gz: {os.path.getsize(archive) // 1024} КБ, {entries} записей, цел")

        code, text = await self.here.run(f"{check.sudo}tar -xzpf {q(archive)} --numeric-owner -C {q(self.base)}", 3600)
        if code != 0:
            return await self._fail(facts, f"не распаковалось (код {code}: {mask(text)[-160:]})")
        self.state["phase"] = "extracted"
        self.state["extracted"] = True
        self._save()
        code, found = await self.here.run(f"find {q(self.path)} | wc -l", 120)
        extracted = _last_int(found)
        if code != 0 or abs(extracted - entries) > max(3, entries // 100):
            return await self._fail(facts, f"после распаковки здесь {extracted} записей вместо {entries}")
        await self.note("extract", True, f"распаковал в {self.path}: {entries} записей")

        if facts.kind != "compose":
            self.state["phase"] = "copied"
            self._save()
            return Outcome(True, self._files_report(facts), moved=False, evidence=f"copied {self.project}")
        return await self._start(facts, check)

    async def _start(self, facts: Facts, check: Check) -> Outcome:
        self.state["phase"] = "starting_new"
        self.state["new_touched"] = True
        self._save()
        command = f"cd {q(facts.compose_dir)} && {check.docker} compose -p {q(facts.name)} -f {q(facts.compose_file)} up -d"
        code, text = await self.here.run(command, 1500)
        if code != 0:
            return await self._fail(facts, f"здесь не запустилось (код {code}): {mask(text)[-300:]}")
        await self.note("start", True, "запустил здесь, жду, пока всё встанет")
        problem = await self._wait_up(facts, check)
        if problem:
            return await self._fail(facts, problem)
        self.state["phase"] = "done"
        self._save()
        boxes = await self._boxes(self.here, facts.name, check.docker)
        listing = ", ".join(f"{b.service} ({b.status})" for b in boxes)
        text = (f"Перенёс «{self.project}» с {self.server} на этот сервер, здесь работает: {listing}. На {self.server} проект "
                f"остановлен, но НЕ удалён ({self.path} цел): так можно вернуть. Бэкап: {self.folder}. DNS и маршруты "
                "Cloudflare я не трогал: если токен туннеля переехал вместе с проектом, менять там ничего не надо.")
        for warning in check.warnings:
            if "restart: always" in warning or "подключены контейнеры" in warning:
                text += " ⚠ " + warning + "."
        return Outcome(True, text, moved=True, evidence=f"{len(boxes)} контейнеров работают: {self.project}")

    async def _wait_up(self, facts: Facts, check: Check) -> str:
        """'' when every wanted service runs and keeps running; else what is wrong."""
        wanted, settled = set(facts.wanted), False
        boxes: list[Box] = []
        for _ in range(36):  # up to 3 minutes
            if self.aborted():
                return "получена команда «стоп»"
            boxes = await self._boxes(self.here, facts.name, check.docker)
            have = {b.service for b in boxes}
            if wanted <= have and all(b.fine for b in boxes):
                if settled:
                    return ""
                settled = True
                await self.sleep(SETTLE)
                continue
            settled = False
            await self.sleep(5)
        bad = [f"{b.service}: {b.status}" for b in boxes if not b.fine] or [f"нет сервисов: {', '.join(sorted(wanted - {b.service for b in boxes}))}"]
        return "здесь не поднялось: " + "; ".join(bad)

    # ---- coming back ----------------------------------------------------------------------------------------------

    async def _fail(self, facts: Facts, why: str) -> Outcome:
        await self.note("fail", False, why)
        back = await self._back(why)
        return Outcome(False, f"Перенос не удался: {why}. {back}")

    async def _back(self, why: str) -> str:
        """The old project runs again (and the new one, if it was started, is stopped). Says honestly what it managed."""
        st = self.state
        parts, failed = [], False
        if st.get("new_touched") and st.get("kind") == "compose":
            code, text = await self.here.run(f"cd {q(st['compose_dir'])} && {st['docker']} compose -p {q(st['name'])} "
                                             f"-f {q(st['compose_file'])} stop", 300)
            parts.append("здесь остановил" if code == 0 else f"здесь остановить не вышло ({mask(text)[-100:]})")
            failed = failed or code != 0
        if st.get("old_touched"):
            if st.get("kind") == "compose":
                command = f"cd {q(st['compose_dir'])} && docker compose -p {q(st['name'])} -f {q(st['compose_file'])} start"
                after = f"docker ps -q --filter label=com.docker.compose.project={q(st['name'])} | wc -l"
            else:
                units = " ".join(q(os.path.basename(u)) for u in st.get("units", []))
                command, after = ((f"systemctl start {units}", f"systemctl is-active {units} | grep -c '^active' || true")
                                  if units else ("true", "echo 1"))
            code, text = await self.old.run(command, 300)
            _, count = await self.old.run(after, 60)
            alive = _last_int(count)
            if code == 0 and alive:
                parts.append(f"на {self.server} запустил обратно ({alive} запущено)")
            else:
                parts.append(f"на {self.server} запустить обратно НЕ вышло ({mask(text)[-120:]}): нужно смотреть руками")
                failed = True
        else:
            parts.append(f"на {self.server} я ещё ничего не менял")
        if st.get("extracted"):
            parts.append(f"папка {self.path} здесь осталась: перед повторным переносом её надо убрать")
        st["phase"] = "rollback_failed" if failed else "rolled_back"
        self._save()
        await self.note("rollback", not failed, "; ".join(parts))
        joined = "; ".join(parts)
        return joined[:1].upper() + joined[1:] + "."

    async def rollback(self) -> Outcome:
        """Brings the old project back from the newest move of it: after a restart of Jarvis, or when the owner asks."""
        newest = self._newest()
        if newest is None:
            return Outcome(False, f"Переноса «{self.project}» с записью о состоянии не нашёл: откатывать нечего.")
        self.folder, self.state = newest
        if self.state.get("phase") == "rolled_back":
            return Outcome(True, "Это уже откатывали: старое запущено на прежнем месте.")
        text = await self._back("откат по просьбе")
        ok = self.state.get("phase") == "rolled_back"
        return Outcome(ok, text, evidence=f"rollback {self.project}")

    # ---- the state file -------------------------------------------------------------------------------------------

    def _save(self) -> None:
        if self.folder:
            with open(os.path.join(self.folder, "state.json"), "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)

    def _newest(self) -> tuple[str, dict] | None:
        try:
            folders = sorted(d for d in os.listdir(self.backups) if d.startswith(self.project + "-"))
        except OSError:
            return None
        for name in reversed(folders):
            try:
                with open(os.path.join(self.backups, name, "state.json"), encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                continue
            if state.get("project") == self.project:
                return os.path.join(self.backups, name), state
        return None

    def _files_report(self, facts: Facts) -> str:
        todo = ["создать пользователя и вернуть права на файлы (владельцы в архиве сохранены)",
                "собрать окружение (venv, зависимости) и проверить .env"]
        todo += [f"поставить службу по образцу {unit} (в ней пользователь {user}; пути в ней вести на {self.path})"
                 for unit, user in facts.units]
        return (f"Файлы «{self.project}» с {self.server} лежат здесь: {self.path}. Старое остановлено, не удалено. Бэкап: "
                f"{self.folder}. Запустить мне ещё нечем, осталось: " + "; ".join(todo) + ". Пока это не сделано, проект не работает "
                "нигде: старое можно вернуть командой отката.")


async def _no_note(step: str, ok: bool, text: str) -> None:
    return None


SCAN = "\n".join([
    'for d in /opt/*/; do n=$(basename "$d"); echo "DIR $n $(du -sk "$d" 2>/dev/null | cut -f1)"; done',
    "docker ps -a --format 'BOX|{{.Label \"com.docker.compose.project\"}}|{{.Names}}|{{.Image}}|{{.Status}}' 2>/dev/null",
])


def render_scan(server: str, text: str) -> str:
    """What lives on the old server, for choosing what to move; the protected ones are marked."""
    dirs, groups = [], {}
    for line in text.splitlines():
        if line.startswith("DIR "):
            _, name, size = (line.split(" ", 2) + ["0"])[:3]
            dirs.append((name, int(size) // 1024 if size.strip().isdigit() else 0))
        elif line.startswith("BOX|"):
            _, project, name, image, status = (line.split("|", 4) + [""] * 5)[:5]
            groups.setdefault(project or "(без compose)", []).append(f"{name} ({image}, {status})")
    out = [f"Папки в /opt на {server}: " + (", ".join(
        f"{n} ({mb} МБ{', НЕ ТРОГАТЬ' if any(w in n for w in PROTECTED) else ''})" for n, mb in dirs) or "нет")]
    for project, boxes in groups.items():
        protected = any(w in project.lower() or any(w in b.lower() for b in boxes) for w in PROTECTED)
        out.append(f"Контейнеры {project}{' (НЕ ТРОГАТЬ)' if protected else ''}: " + "; ".join(boxes))
    return "\n".join(out)


def _last_int(text: str) -> int:
    lines = text.strip().splitlines()
    return int(lines[-1].strip()) if lines and lines[-1].strip().isdigit() else 0


def _tail(path: str, size: int = 400) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - size))
            return f.read().decode(errors="replace")
    except OSError:
        return ""
