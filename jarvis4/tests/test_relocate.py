"""Moving a project from another server: a pretend old server and a pretend new one, and what is done in which order."""
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from jarvis4.config import Config
from jarvis4.core import Core
from jarvis4.nodes.ssh import Server, SshNodes
from jarvis4.ops import relocate
from jarvis4.ops.relocate import Refused, Relocator
from jarvis4.ports import Platform
from jarvis4.store import Store
from jarvis4.testing import ScriptedLLM
from jarvis4.tools import Ctx, all_tools, move
from jarvis4.types import Interaction

INSPECT = "COMPOSE /opt/nvate/deploy/docker-compose.yml\nSIZEKB 204800\nFILES 1500\nPROCS 0\nENV yes"
CONFIG = {"name": "nvate", "services": {
    "app": {"image": "nvate-app:1", "build": {"context": "."}, "ports": [{"published": "3000", "target": 3000}]},
    "db": {"image": "postgres:18"},
    "cloudflared": {"image": "cloudflare/cloudflared:2026.1.0"}}, "networks": {"default": {"name": "nvate_default"}}}
GOOD_DUMP = b"-- PostgreSQL database cluster dump\nCREATE TABLE x();\n-- PostgreSQL database cluster dump complete\n"


def run(coro):
    return asyncio.run(coro)


async def no_sleep(seconds):
    return None


def boxes(state="running", status="Up 5 minutes (healthy)"):
    return "\n".join(f"{s}|nvate-{s}-1|{state}|{status}" for s in ("app", "db", "cloudflared"))


class World:
    """Both servers share one set of facts, so a test can say «the new one never gets healthy» in one line."""

    def __init__(self):
        self.log = []  # (machine, command) in the order they were sent
        self.inspect, self.config = INSPECT, CONFIG
        self.old_running = True
        self.dump = GOOD_DUMP
        self.stop_fails = False
        self.docker = "plain"
        self.sudo_ok = True
        self.network_ok = True
        self.free_kb = 50_000_000
        self.target_exists = False
        self.listening = "0.0.0.0:22\n127.0.0.1:5037"
        self.leftovers = ""
        self.neighbors = ""
        self.listing = "COUNT 1500\nBAD 0\nOUT 0"
        self.new_up = False
        self.new_status = "Up 5 seconds (healthy)"
        self.up_fails = False
        self.extracted_count = 1500
        self.old, self.here = Old(self), Here(self)

    def commands(self, machine=None):
        return [c for m, c in self.log if machine in (None, m)]

    def index(self, machine, needle):
        for i, (m, c) in enumerate(self.log):
            if m == machine and needle in c:
                return i
        return -1


class Old:
    def __init__(self, w):
        self.w = w

    async def run(self, command, timeout=60):
        w = self.w
        w.log.append(("old", command))
        if "echo MISSING" in command:
            return (0, w.inspect) if w.inspect else (3, "MISSING")
        if "config --no-interpolate" in command:
            return 0, json.dumps(w.config)
        if "working_dir=" in command:
            return 0, "nvate"
        if "--filter network=" in command:
            return 0, w.neighbors
        if "docker compose" in command and " stop" in command:
            if w.stop_fails:
                return 1, "Error response from daemon"
            w.old_running = False
            return 0, "Stopping\n0"
        if "docker compose" in command and " start" in command:
            w.old_running = True
            return 0, ""
        if "ps -q" in command:
            return 0, "3" if w.old_running else "0"
        if "ps -a" in command and "--format" in command:
            return 0, boxes() if w.old_running else boxes("exited", "Exited (0) 1 minute ago")
        if command.startswith("find "):
            return 0, "1500"
        return 0, ""

    async def fetch(self, command, dest, timeout=1800):
        w = self.w
        w.log.append(("old", command))
        if "pg_dumpall" in command:
            with open(dest, "wb") as f:
                f.write(w.dump)
            return 0, ""
        with open(dest, "wb") as f:
            f.write(b"archive")
        return 0, ""


class Here:
    def __init__(self, w):
        self.w = w

    async def run(self, command, timeout=60):
        w = self.w
        w.log.append(("here", command))
        if command == "id -u":
            return 0, "1000"
        if command.startswith("sudo -n true"):
            return (0, "") if w.sudo_ok else (1, "sudo: a password is required")
        if "docker info" in command:
            return 0, w.docker
        if "compose version" in command:
            return 0, "2.40.3"
        if "network inspect" in command:
            return (0, "") if w.network_ok else (1, "")
        if command.startswith("df -Pk"):
            return 0, str(w.free_kb)
        if command.startswith("test -e"):
            return 0, "EXISTS" if w.target_exists else "FREE"
        if command.startswith("ss -ltn"):
            return 0, w.listening
        if "ps -a" in command and "--filter" not in command:
            return 0, w.leftovers
        if "gzip -t" in command:
            return 0, w.listing
        if "tar -xzpf" in command:
            return 0, ""
        if command.startswith("find "):
            return 0, str(w.extracted_count)
        if " up -d" in command:
            if w.up_fails:
                return 1, "pull access denied for nvate-app"
            w.new_up = True
            return 0, ""
        if "docker compose" in command and " stop" in command:
            w.new_up = False
            return 0, ""
        if "ps -a" in command and "--filter" in command:
            return (0, boxes(status=w.new_status)) if w.new_up else (0, "")
        return 0, ""


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.w = World()
        self.notes = []

    def mover(self, project="nvate", **kw):
        async def note(step, ok, text):
            self.notes.append((step, ok, text))

        return Relocator(self.w.old, self.w.here, project, server="london", backups=self.tmp.name, note=note,
                         sleep=no_sleep, **kw)

    def move(self, project="nvate", **kw):
        mover = self.mover(project, **kw)
        facts, check = run(mover.plan())
        return mover, run(mover.move(facts, check))


class PlanTest(Fixture):
    def test_the_project_is_described_from_what_the_old_server_says(self):
        facts, check = run(self.mover().plan())
        self.assertEqual((facts.kind, facts.name, facts.databases), ("compose", "nvate", ["db"]))
        self.assertEqual(facts.ports, [3000])
        self.assertTrue(check.ready, check.blockers)
        text = self.mover().describe(facts, check)
        self.assertIn("200 МБ", text)
        self.assertIn("db (postgres:18)", text)
        self.assertIn("Проверки на этом сервере пройдены", text)

    def test_nothing_is_changed_by_looking(self):
        run(self.mover().plan())
        for command in self.w.commands("old"):
            self.assertNotRegex(command, r"\bstop\b|\bstart\b|\brm\b|\bup\b|tar ")

    def test_names_that_are_not_projects_are_refused(self):
        for name in ("amnezia-awg2", "jarvis", "my-vpn", "../etc", "", "UP PER"):
            with self.subTest(name=name), self.assertRaises(Refused):
                run(self.mover(name).plan())

    def test_a_missing_folder_is_said_plainly(self):
        self.w.inspect = ""
        with self.assertRaises(Refused) as caught:
            run(self.mover().plan())
        self.assertIn("нет папки /opt/nvate", str(caught.exception))

    def test_everything_that_would_break_the_move_is_named_before_the_start(self):
        self.w.docker = "none"
        self.w.sudo_ok = False
        self.w.target_exists = True
        self.w.free_kb = 100
        _, check = run(self.mover().plan())
        text = " | ".join(check.blockers)
        for part in ("нужен sudo", "нет рабочего docker", "docker.io docker-compose-v2", "мало места", "уже есть"):
            self.assertIn(part, text)
        self.assertFalse(check.ready)

    def test_a_network_another_project_creates_and_a_taken_port_block_it(self):
        self.w.config = {**CONFIG, "networks": {"nvate_default": {"external": True, "name": "nvate_default"}}}
        self.w.network_ok = False
        self.w.listening = "0.0.0.0:3000"
        _, check = run(self.mover().plan())
        text = " | ".join(check.blockers)
        self.assertIn("«nvate_default»", text)
        self.assertIn("порт 3000", text)

    def test_leftover_containers_of_the_same_project_block_it(self):
        self.w.leftovers = "nvate|nvate-app-1"
        _, check = run(self.mover().plan())
        self.assertIn("уже есть контейнеры проекта", " ".join(check.blockers))

    def test_data_outside_the_project_folder_blocks_it_and_the_machines_own_binds_do_not(self):
        volume = {"type": "volume", "source": "pgdata", "target": "/var/lib/postgresql"}
        inside = {"type": "bind", "source": "/opt/nvate/deploy/data", "target": "/data"}
        socket = {"type": "bind", "source": "/var/run/docker.sock", "target": "/var/run/docker.sock"}
        certs = {"type": "bind", "source": "/etc/letsencrypt", "target": "/certs"}
        services = {**CONFIG["services"], "app": {**CONFIG["services"]["app"], "volumes": [inside, socket]}}
        self.w.config = {**CONFIG, "services": services}
        self.assertTrue(run(self.mover().plan())[1].ready)
        services = {**services, "db": {"image": "postgres:18", "volumes": [volume]},
                    "cloudflared": {"image": "c:1", "volumes": [certs]}}
        self.w.config = {**CONFIG, "services": services}
        text = " | ".join(run(self.mover().plan())[1].blockers)
        self.assertIn("docker-томах (pgdata (db))", text)
        self.assertIn("/etc/letsencrypt (cloudflared)", text)
        self.assertNotIn("docker.sock", text)

    def test_a_restart_always_policy_and_neighbours_in_the_network_are_warned_about_and_repeated_at_the_end(self):
        services = {**CONFIG["services"], "app": {**CONFIG["services"]["app"], "restart": "always"}}
        self.w.config = {**CONFIG, "services": services}
        self.w.neighbors = "ncoin|ncoin-app-1\nnvate|nvate-app-1"
        _, check = run(self.mover().plan())
        text = " | ".join(check.warnings)
        self.assertIn("restart: always", text)
        self.assertIn("ncoin-app-1", text)
        self.assertNotIn("nvate-app-1", text)
        _, outcome = self.move()
        self.assertTrue(outcome.ok)
        self.assertIn("⚠", outcome.text)
        self.assertIn("ncoin-app-1", outcome.text)

    def test_an_unpinned_database_image_is_a_warning(self):
        self.w.config = {**CONFIG, "services": {**CONFIG["services"], "db": {"image": "postgres:latest"}}}
        _, check = run(self.mover().plan())
        self.assertIn("без точной версии", " ".join(check.warnings))
        self.assertTrue(check.ready)

    def test_a_compose_file_that_cannot_be_read_does_not_leak_its_text(self):
        self.w.config = None  # json.dumps(None) is "null": not a mapping
        with self.assertRaises(Refused) as caught:
            run(self.mover().plan())
        self.assertIn("не смог прочитать compose-файл", str(caught.exception))

    def test_the_parsers(self):
        parsed = relocate.parse_config(json.dumps({"name": "x", "services": {
            "a": {"image": "i", "container_name": "fixed", "ports": [{"published": "8080"}, {"published": "${P}"}]}},
            "networks": {"n": {"external": True}, "m": {}}}))
        self.assertEqual((parsed["externals"], parsed["ports"], parsed["container_names"]), (["n"], [8080], ["fixed"]))
        got = relocate.parse_boxes(boxes() + "\nbroken line")
        self.assertEqual(len(got), 3)
        self.assertTrue(got[0].fine)
        self.assertFalse(relocate.Box("a", "b", "running", "Up 2 seconds (health: starting)").fine)
        self.assertFalse(relocate.Box("a", "b", "restarting", "Restarting (1) 3 seconds ago").fine)
        self.assertTrue(relocate.Box("a", "b", "exited", "Exited (0) 2 minutes ago").fine)
        self.assertFalse(relocate.Box("a", "b", "exited", "Exited (1) 2 minutes ago").fine)
        self.assertEqual(relocate.mask("token 123456789:" + "A" * 35 + " ok"), "token *** ok")


class MoveTest(Fixture):
    def test_the_order_is_dump_then_stop_then_archive_then_unpack_then_start(self):
        _, outcome = self.move()
        self.assertTrue(outcome.ok, outcome.text)
        self.assertTrue(outcome.moved)
        w = self.w
        steps = [w.index("old", "pg_dumpall"), w.index("old", "docker compose -p nvate -f /opt/nvate/deploy/docker-compose.yml stop"),
                 w.index("old", "tar --numeric-owner"), w.index("here", "tar -xzpf"), w.index("here", " up -d")]
        self.assertTrue(all(i >= 0 for i in steps), steps)
        self.assertEqual(steps, sorted(steps))
        self.assertIn("healthy", outcome.text)
        self.assertIn("НЕ удалён", outcome.text)

    def test_the_old_project_is_stopped_and_never_deleted_or_wiped(self):
        self.move()
        everything = " ".join(self.w.commands())
        for forbidden in ("rm ", "rmdir", " down", "volume", "prune", "system ", "cat ", "source ", "printenv"):
            self.assertNotIn(forbidden, everything)
        self.assertFalse(self.w.old_running)

    def test_the_new_project_keeps_the_compose_name_and_is_started_from_its_own_folder(self):
        self.move()
        up = next(c for c in self.w.commands("here") if " up -d" in c)
        self.assertIn("cd /opt/nvate/deploy", up)
        self.assertIn("compose -p nvate", up)

    def test_files_are_unpacked_with_their_owners_and_the_archive_is_private(self):
        mover, _ = self.move()
        unpack = next(c for c in self.w.commands("here") if "tar -xzpf" in c)
        self.assertIn("--numeric-owner", unpack)
        if os.name != "nt":
            self.assertEqual(os.stat(mover.folder).st_mode & 0o077, 0)
        self.assertTrue(os.path.exists(os.path.join(mover.folder, "nvate.tar.gz")))
        self.assertTrue(os.path.exists(os.path.join(mover.folder, "db.sql")))

    def test_a_failed_dump_stops_everything_before_anything_is_changed(self):
        self.w.dump = b"-- PostgreSQL database cluster dump\ncut off"
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("ничего не трогал", outcome.text)
        self.assertEqual(self.w.index("old", " stop"), -1)
        self.assertTrue(self.w.old_running)

    def test_an_old_project_that_will_not_stop_is_started_again_and_nothing_goes_on(self):
        self.w.stop_fails = True
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("не остановилось", outcome.text)
        self.assertEqual(self.w.index("old", "tar --numeric-owner"), -1)
        self.assertEqual(self.w.index("here", " up -d"), -1)

    def test_an_archive_with_paths_outside_the_project_is_never_unpacked(self):
        self.w.listing = "COUNT 1500\nBAD 0\nOUT 3"
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertEqual(self.w.index("here", "tar -xzpf"), -1)
        self.assertTrue(self.w.old_running, "the old project is back")
        self.assertIn("запустил обратно", outcome.text)

    def test_an_archive_that_lost_files_on_the_way_is_not_trusted(self):
        self.w.listing = "COUNT 900\nBAD 0\nOUT 0"
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("не сходится", outcome.text)
        self.assertTrue(self.w.old_running)

    def test_a_new_project_that_never_gets_healthy_hands_the_work_back_to_the_old_one(self):
        self.w.new_status = "Up 2 seconds (unhealthy)"
        mover, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.moved)
        self.assertIn("не поднялось", outcome.text)
        self.assertIn("остановил", outcome.text)
        self.assertIn("запустил обратно", outcome.text)
        self.assertTrue(self.w.old_running)
        self.assertFalse(self.w.new_up)
        with open(os.path.join(mover.folder, "state.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["phase"], "rolled_back")
        self.assertTrue(any(step == "rollback" and ok for step, ok, _ in self.notes))

    def test_a_start_that_fails_at_once_also_rolls_back(self):
        self.w.up_fails = True
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("не запустилось", outcome.text)
        self.assertTrue(self.w.old_running)

    def test_the_old_project_that_cannot_be_started_back_is_reported_not_hidden(self):
        self.w.new_status = "Up 2 seconds (unhealthy)"
        original = self.w.old.run

        async def deaf(command, timeout=60):
            if "docker compose" in command and " start" in command:
                return 1, "no such container"
            return await original(command, timeout)

        self.w.old.run = deaf
        mover, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("НЕ вышло", outcome.text)
        self.assertEqual(mover.state["phase"], "rollback_failed")

    def test_a_stop_from_the_owner_before_the_stop_of_the_old_changes_nothing(self):
        mover, outcome = self.move(aborted=lambda: True)
        self.assertFalse(outcome.ok)
        self.assertEqual(self.w.index("old", " stop"), -1)

    def test_a_blocked_move_does_not_start(self):
        self.w.docker = "none"
        _, outcome = self.move()
        self.assertFalse(outcome.ok)
        self.assertIn("Не переношу", outcome.text)
        self.assertEqual(self.w.index("old", "pg_dumpall"), -1)
        self.assertTrue(self.w.old_running)

    def test_a_secret_in_a_failure_text_is_masked(self):
        self.w.up_fails = True
        original = self.w.here.run

        async def leaky(command, timeout=60):
            if " up -d" in command:
                return 1, "invalid token 123456789:" + "B" * 35
            return await original(command, timeout)

        self.w.here.run = leaky
        _, outcome = self.move()
        self.assertNotIn("B" * 35, outcome.text)
        self.assertNotIn("B" * 35, " ".join(text for _, _, text in self.notes))


@unittest.skipUnless(shutil.which("bash"), "needs bash to check the syntax")
class ShellTest(Fixture):
    def test_every_command_that_is_sent_is_valid_shell(self):
        self.move()  # a whole move, with a pretend failure and the way back
        self.w.new_status = "Up 2 seconds (unhealthy)"
        self.w.old_running = True
        self.move()
        run(self.mover().rollback())
        sent = {c for _, c in self.w.log} | {relocate.inspect_script("/opt/nvate"), relocate.SCAN}
        self.assertGreater(len(sent), 15)
        for command in sent:
            done = subprocess.run(["bash", "-n", "-c", command], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, f"{command}\n{done.stderr}")

    def test_the_look_at_the_project_folder_works_on_a_real_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "p", "deploy"))
            for name in ("docker-compose.yml", ".env"):
                open(os.path.join(tmp, "p", "deploy", name), "w").close()
            path = tmp.replace("\\", "/") + "/p"
            done = subprocess.run(["bash", "-c", relocate.inspect_script(path)], capture_output=True, text=True)
            facts = relocate.parse_inspect("p", path, done.stdout)
            self.assertEqual(facts.compose_file, f"{path}/deploy/docker-compose.yml")
            self.assertTrue(facts.has_env)
            self.assertEqual((facts.procs, facts.units), (0, []))
            self.assertGreaterEqual(facts.files, 3)
            gone = subprocess.run(["bash", "-c", relocate.inspect_script(path + "x")], capture_output=True, text=True)
            self.assertEqual((gone.returncode, gone.stdout.strip()), (3, "MISSING"))


class FilesTest(Fixture):
    def setUp(self):
        super().setUp()
        self.w.inspect = "SIZEKB 2048\nFILES 40\nPROCS 0\nENV yes\nUNIT /etc/systemd/system/photocounter.service photo"
        self.w.listing = "COUNT 40\nBAD 0\nOUT 0"
        self.w.extracted_count = 40
        original = self.w.old.run
        self.units_running = True

        async def old(command, timeout=60):
            if command.startswith("systemctl stop"):
                self.units_running = False
                return 0, "0"
            if command.startswith("systemctl start"):
                self.units_running = True
                return 0, ""
            if "is-active" in command:
                return 0, "1" if self.units_running else "0"
            if command.startswith("find "):
                return 0, "40"
            return await original(command, timeout)

        self.w.old.run = old

    def test_a_plain_folder_is_stopped_copied_and_left_for_the_rest_to_be_done(self):
        _, outcome = self.move("photocounter")
        self.assertTrue(outcome.ok, outcome.text)
        self.assertFalse(outcome.moved)
        self.assertIn("photocounter.service", outcome.text)
        self.assertIn("мне ещё нечем", outcome.text)
        self.assertEqual(self.w.index("here", " up -d"), -1)
        self.assertFalse(self.units_running)

    def test_processes_outside_systemd_block_it(self):
        self.w.inspect = "SIZEKB 2048\nFILES 40\nPROCS 2\nENV yes"
        _, check = run(self.mover("photocounter").plan())
        self.assertIn("не служба systemd", " ".join(check.blockers))


class RollbackTest(Fixture):
    def test_a_finished_move_can_be_taken_back_from_the_state_file(self):
        mover, outcome = self.move()
        self.assertTrue(outcome.ok)
        again = self.mover()
        back = run(again.rollback())
        self.assertTrue(back.ok, back.text)
        self.assertTrue(self.w.old_running)
        self.assertFalse(self.w.new_up)
        self.assertIn("запустил обратно", back.text)
        self.assertEqual(run(self.mover().rollback()).text, "Это уже откатывали: старое запущено на прежнем месте.")

    def test_without_a_record_there_is_nothing_to_roll_back(self):
        back = run(self.mover().rollback())
        self.assertFalse(back.ok)
        self.assertIn("откатывать нечего", back.text)


class ScanTest(unittest.TestCase):
    def test_the_vpn_is_marked_and_projects_are_grouped(self):
        text = relocate.render_scan("london", "DIR nvate 204800\nDIR amnezia 10240\nBOX|nvate|nvate-app-1|nvate-app|Up 2 days\n"
                                              "BOX||amnezia-awg2|amnezia-awg2|Up 9 days")
        self.assertIn("nvate (200 МБ)", text)
        self.assertIn("amnezia (10 МБ, НЕ ТРОГАТЬ)", text)
        self.assertIn("Контейнеры (без compose) (НЕ ТРОГАТЬ)", text)
        self.assertIn("Контейнеры nvate: nvate-app-1", text)


def core_with(nodes, stuff=None):
    store = Store(":memory:")
    cfg = Config(owner_id=1, db_path=":memory:", data_dir=os.path.join(tempfile.gettempdir(), "j4test", "data"))
    core = Core(cfg, store, Platform(tg=None, server=None, web=None, extras={"ssh": nodes}), llm=ScriptedLLM([], store),
                outs=lambda chat: None)

    class Out:
        async def ask(self, q):
            return True

    itx = Interaction(kind="owner", chat="owner", sender="owner", text="x", msgs=[], owner=True, out=Out())
    return core, itx


class ToolTest(unittest.TestCase):
    def setUp(self):
        move._plans.clear()
        move._moves.clear()

    def call(self, nodes, tool, **args):
        core, itx = core_with(nodes)
        return run(all_tools()[tool].run(Ctx(core, itx), args))

    def guard(self, nodes, **args):
        core, itx = core_with(nodes)
        return run(all_tools()["project_move"].guard(Ctx(core, itx), args))

    def test_a_server_with_view_only_access_cannot_be_worked_on_and_the_owner_is_told_how_to_change_it(self):
        nodes = SshNodes([Server("london", "root@1.2.3.4", "/k/m", "status")])
        for tool, args in (("project_move", {"project": "nvate", "plan_only": True}), ("projects_scan", {}),
                           ("project_move_back", {"project": "nvate"})):
            result = self.call(nodes, tool, server="london", **args)
            self.assertFalse(result.ok, tool)
            self.assertIn("london-full", result.text)
            self.assertIn("authorized_keys", result.text)
        self.assertIsNone(self.guard(nodes, server="london", project="nvate"))

    def test_an_unknown_server_names_the_known_ones(self):
        nodes = SshNodes([Server("home", "me@x", "/k", "full")])
        result = self.call(nodes, "project_move", server="nope", project="nvate", plan_only=True)
        self.assertIn("Есть: home", result.text)

    def test_the_real_move_asks_once_and_the_plan_never_does(self):
        w = World()
        nodes = SshNodes([Server("home", "me@x", "/k", "full")])

        async def exec_(name, command, timeout=60):
            return await w.old.run(command, timeout)

        nodes.exec = exec_
        real = move._relocator

        def fake(ctx, nodes_, server, project, **kw):
            return Relocator(w.old, w.here, project, server=server, backups=tempfile.mkdtemp(), sleep=no_sleep, **kw)

        move._relocator = fake
        try:
            self.assertIsNone(self.guard(nodes, server="home", project="nvate", plan_only=True))
            verdict = self.guard(nodes, server="home", project="nvate")
            self.assertEqual(verdict.kind, "ask")
            self.assertIn("не удаляю", verdict.text)
            self.assertEqual(verdict.scope, "move:home:nvate")
            w.docker = "none"  # a move that cannot start is not worth a question
            move._plans.clear()
            self.assertIsNone(self.guard(nodes, server="home", project="nvate"))
            result = self.call(nodes, "project_move", server="home", project="nvate")
            self.assertFalse(result.ok)
            self.assertIn("нет рабочего docker", result.text)
            self.assertEqual(w.index("old", "pg_dumpall"), -1)
        finally:
            move._relocator = real


if __name__ == "__main__":
    unittest.main()
