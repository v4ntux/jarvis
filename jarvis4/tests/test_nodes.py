"""The phone and the other servers, with a pretend adb and a pretend ssh: what is sent, what is refused."""
import asyncio
import struct
import unittest

from jarvis4.config import Config
from jarvis4.core import Core
from jarvis4.nodes import android as android_mod
from jarvis4.nodes import ssh as ssh_mod
from jarvis4.nodes.android import AndroidError, AndroidNode, png_size
from jarvis4.nodes.ssh import Server, SshNodes, parse_nodes
from jarvis4.ports import Offline, Platform
from jarvis4.store import Store
from jarvis4.testing import ScriptedLLM
from jarvis4.tools import Ctx, all_tools
from jarvis4.types import Interaction


def run(coro):
    return asyncio.run(coro)


def png(width, height):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height) + b"\x08\x02\x00\x00\x00"


class FakeAdb(AndroidNode):
    """AndroidNode whose adb is a dictionary: the commands it was given, and what each one answers."""

    def __init__(self, answers=None, target="100.64.0.9:5555"):
        super().__init__(target)
        self.answers, self.sent = answers or {}, []
        self._online = True

    async def _run(self, *args, timeout=20, binary=False, serial=True):
        self.sent.append(args)
        line = " ".join(args)
        for key, answer in self.answers.items():
            if key in line:
                return answer if isinstance(answer, tuple) else (0, answer if isinstance(answer, bytes) else answer.encode())
        return 0, b""

    async def _scale(self, data, width, height, crop=None):
        self.scaled = (width, height, crop)
        return b"scaled"


class AndroidTest(unittest.TestCase):
    def phone(self, **answers):
        node = FakeAdb({"screencap": png(1080, 2400), **answers})
        run(node.screenshot())
        node.sent.clear()
        return node

    def test_the_picture_is_scaled_to_the_models_limit_and_clicks_map_back(self):
        node = FakeAdb({"screencap": png(1080, 2400)})
        self.assertEqual(run(node.screenshot()), b"scaled")
        self.assertEqual(node.scaled[:2], (576, 1280))
        node.sent.clear()
        self.assertEqual(run(node.do("left_click", {"coordinate": [288, 640]})), ("OK", None))
        self.assertEqual(node.sent[-1], ("shell", "input", "tap", "540", "1200"))  # the middle of the real screen

    def test_a_click_before_any_screenshot_is_refused(self):
        node = FakeAdb()
        with self.assertRaises(AndroidError):
            run(node.do("left_click", {"coordinate": [1, 1]}))

    def test_double_tap_long_press_drag_and_scroll(self):
        node = self.phone()
        run(node.do("double_click", {"coordinate": [100, 100]}))
        self.assertEqual([a[2] for a in node.sent], ["tap", "tap"])
        node.sent.clear()
        run(node.do("right_click", {"coordinate": [100, 100]}))
        self.assertEqual(node.sent[-1][2], "swipe")
        self.assertEqual(node.sent[-1][-1], "700")  # held: a long press
        node.sent.clear()
        run(node.do("scroll", {"scroll_direction": "down", "scroll_amount": 3}))
        _, _, kind, x0, y0, x1, y1, ms = node.sent[-1]
        self.assertEqual((kind, x0, x1), ("swipe", "540", "540"))
        self.assertGreater(int(y0), int(y1))  # the finger goes up so the page goes down

    def test_keys(self):
        node = self.phone()
        run(node.do("key", {"text": "Return", "repeat": 2}))
        self.assertEqual([a[-1] for a in node.sent], ["66", "66"])
        with self.assertRaises(AndroidError) as caught:
            run(node.do("key", {"text": "ctrl+shift+q"}))
        self.assertIn("Есть:", str(caught.exception))

    def test_a_mouse_only_action_tells_the_model_what_a_phone_has(self):
        node = self.phone()
        with self.assertRaises(AndroidError) as caught:
            run(node.do("mouse_move", {"coordinate": [1, 1]}))
        self.assertIn("нет мыши", str(caught.exception))

    def test_latin_text_is_typed_and_russian_needs_the_keyboard_app(self):
        node = self.phone()
        run(node.do("type", {"text": "hello world"}))
        self.assertEqual(node.sent[-1][-2:], ("text", "hello%sworld"))
        with self.assertRaises(AndroidError) as caught:
            run(node.do("type", {"text": "привет"}))
        self.assertIn("ADBKeyBoard", str(caught.exception))
        with_ime = self.phone(**{"ime list": "com.android.adbkeyboard/.AdbIME\ncom.google.android.inputmethod.latin/.X"})
        run(with_ime.do("type", {"text": "привет"}))
        self.assertTrue(any("ADB_INPUT_B64" in " ".join(a) for a in with_ime.sent))

    def test_offline_phone_raises_offline(self):
        node = FakeAdb({"get-state": (1, b"error: device not found")})
        node._online = False
        with self.assertRaises(Offline):
            run(node.shell("ls"))

    def test_a_dropped_network_phone_is_reconnected(self):
        node = FakeAdb({"get-state": (1, b"error")})
        node._online = False
        run(node.refresh())
        self.assertTrue(any(a[0] == "connect" for a in node.sent))

    def test_notifications_are_read_from_dumpsys(self):
        dump = """  NotificationRecord(0x1: pkg=org.telegram.messenger user=UserHandle{0} id=1)
      android.title=String (Камрон)
      android.text=String (ты где?)
  NotificationRecord(0x2: pkg=com.bank.app user=UserHandle{0} id=2)
      android.title=String (Списание)
      android.text=String (150 руб, магазин)"""
        node = FakeAdb({"dumpsys notification": dump})
        text = run(node.notifications())
        self.assertIn("org.telegram.messenger: Камрон — ты где?", text)
        self.assertIn("com.bank.app: Списание — 150 руб", text)

    def test_an_odd_package_name_is_refused(self):
        with self.assertRaises(AndroidError):
            run(FakeAdb().open_app("x; rm -rf /sdcard"))

    def test_png_size_reads_the_header(self):
        self.assertEqual(png_size(png(720, 1600)), (720, 1600))
        with self.assertRaises(AndroidError):
            png_size(b"not a picture at all......")


class FakeProc:
    def __init__(self, out=b"ok", code=0):
        self.out, self.returncode = out, code

    async def communicate(self):
        return self.out, b""

    def kill(self):
        pass


class SshTest(unittest.TestCase):
    def test_a_broken_setting_is_ignored_not_fatal(self):
        self.assertEqual(parse_nodes("{not json"), {})
        self.assertEqual(parse_nodes(""), {})
        self.assertEqual(parse_nodes("[1]"), {})

    def test_the_monitored_vps_is_read_only_and_a_named_one_is_full(self):
        nodes = SshNodes.from_config(
            {"servers": {"Home": {"target": "me@10.0.0.2", "key": "/k/home"}, "bad": {"target": "x"}}},
            "London=root@46.226.162.170", key_exists=lambda path: True)
        self.assertEqual(nodes.names(), ["home", "london"])
        self.assertEqual(nodes.servers["london"].mode, "status")
        self.assertEqual(nodes.servers["home"].mode, "full")
        self.assertIn("только смотреть", nodes.describe())

    def test_no_monitor_key_means_no_london(self):
        self.assertEqual(SshNodes.from_config({}, "London=root@1.2.3.4", key_exists=lambda p: False).names(), [])

    def capture(self, nodes, name, command, out=b"ok", code=0):
        seen = {}

        async def fake(*args, **kw):
            seen["args"] = args
            return FakeProc(out, code)

        real = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fake
        try:
            text, mode = run(nodes.run(name, command))
        finally:
            asyncio.create_subprocess_exec = real
        return seen["args"], text, mode

    def test_a_full_server_gets_the_command_a_status_server_does_not(self):
        nodes = SshNodes([Server("home", "me@10.0.0.2", "/k/home", "full"), Server("london", "root@1.2.3.4", "/k/m", "status")])
        args, text, mode = self.capture(nodes, "home", "uptime")
        self.assertEqual(args[-1], "uptime")
        self.assertIn("BatchMode=yes", args)
        self.assertEqual(args[args.index("-i") + 1], "/k/home")
        args, text, mode = self.capture(nodes, "London", "rm -rf /")  # a name is not case sensitive; the command is not sent
        self.assertEqual(args[-1], "root@1.2.3.4")
        self.assertNotIn("rm -rf /", " ".join(args))
        self.assertEqual(mode, "status")

    def test_a_failed_login_is_an_error_and_an_exit_code_is_shown(self):
        nodes = SshNodes([Server("home", "me@10.0.0.2", "/k/home", "full")])
        _, text, _ = self.capture(nodes, "home", "uptime", out=b"Permission denied (publickey).", code=255)
        self.assertTrue(text.startswith("Ошибка: не вошёл"))
        _, text, _ = self.capture(nodes, "home", "false", out=b"", code=1)
        self.assertTrue(text.startswith("(код 1)"))

    def test_an_unknown_server_names_the_known_ones(self):
        with self.assertRaises(KeyError) as caught:
            run(SshNodes([Server("home", "me@x", "/k", "full")]).run("nope", "ls"))
        self.assertIn("Есть: home", caught.exception.args[0])


def core_with(extras):
    store = Store(":memory:")
    cfg = Config(owner_id=1, db_path=":memory:")
    core = Core(cfg, store, Platform(tg=None, server=None, web=None, extras=extras), llm=ScriptedLLM([], store),
                outs=lambda chat: None)

    class Out:
        async def ask(self, q):
            return True

    itx = Interaction(kind="owner", chat="owner", sender="owner", text="x", msgs=[], owner=True, out=Out())
    return core, itx


class ToolsTest(unittest.TestCase):
    def call(self, extras, tool, **args):
        core, itx = core_with(extras)
        return run(all_tools()[tool].run(Ctx(core, itx), args))

    def test_no_phone_configured_says_how_to_set_it_up(self):
        result = self.call({}, "android_status")
        self.assertFalse(result.ok)
        self.assertIn("настроить", result.text)

    def test_the_phone_tools_report_what_the_node_says(self):
        node = FakeAdb({"getprop ro.product.model": "Pixel 8", "dumpsys battery": "level: 83",
                        "getprop ro.build.version.release": "15", "dumpsys power": "mWakefulness=Awake",
                        "dumpsys window": "mCurrentFocus=Window{abc u0 org.telegram.messenger/.Main}"})
        result = self.call({"android": node}, "android_status")
        self.assertIn("батарея: 83%", result.text)
        self.assertIn("модель: Pixel 8", result.text)

    def test_ssh_run_on_a_status_server_says_the_command_did_not_run(self):
        nodes = SshNodes([Server("london", "root@1.2.3.4", "/k/m", "status")])

        async def fake(*args, **kw):
            return FakeProc(b"host vps\nload 0.1 2")

        real = asyncio.create_subprocess_exec
        asyncio.create_subprocess_exec = fake
        try:
            result = self.call({"ssh": nodes}, "ssh_run", server="london", command="reboot")
        finally:
            asyncio.create_subprocess_exec = real
        self.assertTrue(result.ok)
        self.assertIn("команду я не выполнял", result.text)

    def test_ssh_without_servers_is_explained(self):
        result = self.call({"ssh": SshNodes([])}, "ssh_run", server="x", command="ls")
        self.assertFalse(result.ok)
        self.assertIn("Ван добавляет сервер сам", result.text)


if __name__ == "__main__":
    unittest.main()
