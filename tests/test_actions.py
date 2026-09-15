"""Windows execution regressions. Every mutating OS call is replaced by a mock."""
import ctypes
import os
import unittest
from pathlib import Path
from unittest import mock

from core import actions, autostart, skills


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        permission = mock.patch.object(skills.memory, "permission", return_value={
            "enabled": True, "always_confirm": False})
        permission.start()
        self.addCleanup(permission.stop)

    def test_internal_typeerror_never_reexecutes_action(self):
        action = mock.Mock(side_effect=TypeError("an internal failure"))
        skill = skills.Skill("test_once", "test", {"text": skills._spec("text")}, run=action)
        with mock.patch.dict(skills.SKILLS, {skill.name: skill}), \
                self.assertLogs("core.skills", level="WARNING"):
            result = skills.execute(skill.name, {"text": "requested"})
        self.assertFalse(result.ok)
        action.assert_called_once_with(text="requested")

    def test_action_error_is_structured_failure(self):
        with mock.patch.object(actions, "open_app", side_effect=actions.ActionError("Окно не найдено")):
            result = skills.execute("open_app", {"name": "missing"})
        self.assertFalse(result.ok)
        self.assertEqual(result.message, "Окно не найдено")

    def test_disabled_action_is_never_executed(self):
        with mock.patch.object(skills.memory, "permission", return_value={"enabled": False}), \
                mock.patch.object(actions, "open_app") as launch:
            result = skills.execute("open_app", {"name": "test"})
        self.assertFalse(result.ok)
        launch.assert_not_called()

    def test_invalid_values_are_not_coerced_into_commands(self):
        invalid = [("type_text", {"text": None}), ("type_text", {"text": 17}),
                   ("mute", {"on": "nonsense"}), ("set_volume", {"percent": 12.4}),
                   ("set_volume", {"percent": float("nan")}),
                   ("set_volume", {"percent": float("inf")}),
                   ("set_volume", {"percent": True}),
                   ("open_app", {"name": "test", "unexpected": "value"}),
                   ("media", {"what": "invalid"}), ("scroll", {"direction": "diagonal"})]
        for name, params in invalid:
            with self.subTest(name=name, params=params), self.assertRaises(ValueError):
                skills.validate_params(name, params)

    def test_text_content_and_spacing_are_preserved(self):
        text = '  Привет, мир!\nСледующая строка.  '
        self.assertEqual(skills.validate_params("type_text", {"text": text})["text"], text)

    def test_routine_preflight_checks_later_steps_before_running_first(self):
        routine = {"steps": [{"skill": "time"}, {"skill": "shutdown"}]}
        with mock.patch.object(skills.memory, "find_routine", return_value=routine), \
                mock.patch.object(skills, "execute") as execute:
            result = skills._run_routine("test")
        self.assertFalse(result.ok)
        execute.assert_not_called()

    def test_routine_stops_on_failed_execution(self):
        routine = {"steps": [{"skill": "time"}, {"skill": "date"}], "stop_on_error": True}
        with mock.patch.object(skills.memory, "find_routine", return_value=routine), \
                mock.patch.object(skills, "execute", return_value=skills.ActionResult(False, "failure")) as execute:
            result = skills._run_routine("test")
        self.assertFalse(result.ok)
        execute.assert_called_once()


class InputTests(unittest.TestCase):
    def test_input_structure_matches_windows_abi(self):
        self.assertEqual(ctypes.sizeof(actions._INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)

    def test_unknown_key_does_not_send_partial_hotkey(self):
        with mock.patch.object(actions, "_send") as send:
            with self.assertRaises(actions.ActionError):
                actions.press("ctrl", "not-a-key")
        send.assert_not_called()

    def test_sendinput_rejection_is_not_reported_as_success(self):
        with mock.patch.object(actions.user32, "SendInput", return_value=0):
            with self.assertRaises(actions.ActionError):
                actions._send([actions._key(65), actions._key(65, True)])

    def test_typing_preserves_surrogate_pairs(self):
        events = []
        with mock.patch.object(actions, "_ensure_target", return_value=123), \
                mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions, "_send", side_effect=events.extend):
            actions.type_text("Я😀")
        units = [event.u.ki.wScan for event in events if not event.u.ki.dwFlags & actions._KEYEVENTF_KEYUP]
        self.assertEqual(units, [0x042F, 0xD83D, 0xDE00])

    def test_target_is_restored_once_per_command(self):
        with mock.patch.object(actions, "_activate") as activate, \
                mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions, "window_pid", return_value=os.getpid() + 1):
            with actions.target_window(123):
                actions._ensure_target()
                actions._ensure_target()
        activate.assert_called_once_with(123)

    def test_typing_into_assistant_is_rejected(self):
        with mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions, "window_pid", return_value=os.getpid()), \
                mock.patch.object(actions, "_send") as send:
            with self.assertRaises(actions.ActionError):
                actions.type_text("should not be typed")
        send.assert_not_called()

    def test_stale_target_does_not_send_input(self):
        with mock.patch.object(actions.user32, "IsWindow", return_value=False), \
                mock.patch.object(actions, "_send") as send:
            with actions.target_window(999):
                with self.assertRaises(actions.ActionError):
                    actions.type_text("should not be typed")
        send.assert_not_called()

    def test_launch_timeout_blocks_dependent_typing(self):
        with mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions.time, "monotonic", side_effect=[0, 3]), \
                mock.patch.object(actions, "_send") as send:
            with actions.target_window(123):
                actions._after_launch(123)
                with self.assertRaises(actions.ActionError):
                    actions.type_text("should not be typed")
        send.assert_not_called()

    def test_russian_spoken_hotkeys_are_supported(self):
        with mock.patch.object(actions, "_ensure_target"), \
                mock.patch.object(actions, "_send") as send:
            actions.hotkey("контрол плюс с")
        events = send.call_args.args[0]
        self.assertEqual([event.u.ki.wVk for event in events], [0x11, 0x43, 0x43, 0x11])

    def test_collapsing_jarvis_is_not_mistaken_for_new_app_launch(self):
        with mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions, "window_pid", return_value=os.getpid() + 1), \
                mock.patch.object(actions, "_matches_launch", return_value=False), \
                mock.patch.object(actions.time, "monotonic", side_effect=[0, 0, 3]), \
                mock.patch.object(actions.time, "sleep"), \
                mock.patch.object(actions, "_send") as send:
            with actions.target_window(123):
                actions._after_launch(999, "notepad")
                with self.assertRaises(actions.ActionError):
                    actions.type_text("should not be typed into the old window")
        send.assert_not_called()

    def test_focus_change_during_long_typing_stops_later_batches(self):
        with mock.patch.object(actions, "_ensure_target", return_value=123), \
                mock.patch.object(actions, "foreground_window", side_effect=[123, 456]), \
                mock.patch.object(actions, "_send") as send:
            with self.assertRaises(actions.ActionError):
                actions.type_text("a" * 150)
        send.assert_called_once()

    def test_activating_maximized_window_preserves_its_size(self):
        with mock.patch.object(actions.user32, "IsWindow", return_value=True), \
                mock.patch.object(actions.user32, "IsIconic", return_value=False), \
                mock.patch.object(actions.user32, "ShowWindow") as show, \
                mock.patch.object(actions.user32, "SetForegroundWindow"), \
                mock.patch.object(actions, "foreground_window", return_value=123):
            actions._activate(123)
        show.assert_not_called()

    def test_approval_restores_original_target_and_cursor(self):
        snapshot = {"hwnd": 123, "blocked": "", "cursor": [40, 50], "cursor_window": 123}
        with mock.patch.object(actions, "_activate") as activate, \
                mock.patch.object(actions, "foreground_window", return_value=123), \
                mock.patch.object(actions, "window_pid", return_value=os.getpid() + 1), \
                mock.patch.object(actions, "_cursor_window", return_value=123), \
                mock.patch.object(actions.user32, "SetCursorPos", return_value=True) as move, \
                mock.patch.object(actions, "_send") as send:
            with actions.target_window(999):
                with actions.restore_target(snapshot):
                    actions.click_mouse()
        activate.assert_called_once_with(123)
        move.assert_called_once_with(40, 50)
        send.assert_called_once()

    def test_cursor_on_jarvis_confirmation_button_is_not_clicked(self):
        snapshot = {"hwnd": 123, "blocked": "", "cursor": [40, 50], "cursor_window": 999}
        with mock.patch.object(actions, "_ensure_target", return_value=123), \
                mock.patch.object(actions.user32, "SetCursorPos") as move, \
                mock.patch.object(actions, "_send") as send:
            with actions.restore_target(snapshot):
                with self.assertRaises(actions.ActionError):
                    actions.click_mouse()
        move.assert_not_called()
        send.assert_not_called()

    def test_confirmation_for_closed_target_does_not_click(self):
        snapshot = {"hwnd": 123, "blocked": "", "cursor": [40, 50], "cursor_window": 123}
        with mock.patch.object(actions.user32, "IsWindow", return_value=False), \
                mock.patch.object(actions.user32, "SetCursorPos") as move, \
                mock.patch.object(actions, "_send") as send:
            with actions.restore_target(snapshot):
                with self.assertRaises(actions.ActionError):
                    actions.click_mouse()
        move.assert_not_called()
        send.assert_not_called()

    def test_target_capture_is_serializable_and_keeps_pre_chat_window(self):
        import json
        with mock.patch.object(actions, "_cursor_state", return_value=([40, 50], 123)), \
                mock.patch.object(actions, "foreground_window", return_value=999):
            with actions.target_window(123):
                snapshot = json.loads(json.dumps(actions.capture_target()))
        self.assertEqual(snapshot["hwnd"], 123)
        self.assertEqual(snapshot["cursor"], [40, 50])

    def test_changed_foreground_after_launch_replaces_original_target(self):
        with mock.patch.object(actions, "foreground_window", return_value=456), \
                mock.patch.object(actions, "window_pid", return_value=os.getpid() + 1), \
                mock.patch.object(actions, "_window_title", return_value="New application"), \
                mock.patch.object(actions, "_activate") as activate:
            with actions.target_window(123):
                actions._after_launch(123)
                self.assertEqual(actions._ensure_target(), 456)
        activate.assert_not_called()

    def test_scroll_is_bounded_before_input(self):
        with mock.patch.object(actions, "_send") as send:
            with self.assertRaises(actions.ActionError):
                actions.scroll("down", 1000)
        send.assert_not_called()

    def test_scroll_cannot_scroll_jarvis_instead_of_selected_app(self):
        with mock.patch.object(actions, "_ensure_target", return_value=123), \
                mock.patch.object(actions, "_cursor_state", return_value=([40, 50], 999)), \
                mock.patch.object(actions, "_send") as send:
            with self.assertRaises(actions.ActionError):
                actions.scroll("down", 3)
        send.assert_not_called()


class ApplicationTests(unittest.TestCase):
    def test_known_folder_uses_windows_shell_location(self):
        import winreg
        with mock.patch.object(winreg, "OpenKey"), \
                mock.patch.object(winreg, "QueryValueEx", return_value=("D:/Personal/Downloads", 1)), \
                mock.patch.object(actions, "open_path", return_value="Открываю") as launch:
            actions.open_folder("загрузки")
        launch.assert_called_once_with(Path("D:/Personal/Downloads"))

    def test_resolve_edge_display_name_without_launching(self):
        path = Path("C:/fake/Microsoft Edge.lnk")
        with mock.patch.object(actions, "shortcuts", return_value={"microsoft edge": path}):
            self.assertEqual(actions.resolve_app("эдж"), path)

    def test_resolve_registered_application(self):
        path = Path("C:/fake/Telegram.exe")
        with mock.patch.object(actions, "shortcuts", return_value={}), \
                mock.patch.object(actions.shutil, "which", return_value=None), \
                mock.patch.object(actions, "_registered_app", return_value=path):
            self.assertEqual(actions.resolve_app("телеграм"), path)

    def test_missing_app_is_explicit_error(self):
        with mock.patch.object(actions, "resolve_app", return_value=None), \
                mock.patch.object(actions.os, "startfile") as launch:
            with self.assertRaises(actions.ActionError):
                actions.open_app("missing")
        launch.assert_not_called()

    def test_close_matches_exact_process_name(self):
        proc = mock.Mock(pid=1, info={"name": "telegram.exe"})
        with mock.patch.object(actions.psutil, "process_iter", return_value=[proc]), \
                mock.patch.object(actions, "_windows_of_pid") as windows:
            with self.assertRaises(actions.ActionError):
                actions.close_app("gram")
        windows.assert_not_called()

    def test_browser_rejection_is_error(self):
        with mock.patch.object(actions.webbrowser, "open", return_value=False):
            with self.assertRaises(actions.ActionError):
                actions.open_url("https://example.com")

    def test_unsupported_url_protocol_is_rejected(self):
        with mock.patch.object(actions.webbrowser, "open") as browser:
            for url in ("file:///C:/test", "javascript://alert", "https://user:secret@example.com", " "):
                with self.subTest(url=url), self.assertRaises(actions.ActionError):
                    actions.open_url(url)
        browser.assert_not_called()

    def test_shutdown_failure_is_not_success(self):
        with mock.patch.object(actions.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaises(actions.ActionError):
                actions.power("cancel")

    def test_source_autostart_uses_current_source(self):
        target, arguments, workdir = autostart._target()
        self.assertNotIn("dist", target.lower())
        self.assertIn("jarvis.py", arguments)


class ComAndClipboardTests(unittest.TestCase):
    def test_volume_initializes_and_releases_current_thread_com(self):
        import pythoncom
        from pycaw.pycaw import AudioUtilities
        endpoint = mock.Mock()
        endpoint.GetMasterVolumeLevelScalar.return_value = .4
        endpoint.GetMute.return_value = False
        with mock.patch.object(pythoncom, "CoInitializeEx") as initialize, \
                mock.patch.object(pythoncom, "CoUninitialize") as uninitialize, \
                mock.patch.object(AudioUtilities, "GetSpeakers", return_value=mock.Mock(EndpointVolume=endpoint)):
            result = actions.get_volume()
        self.assertIn("40", result)
        initialize.assert_called_once()
        uninitialize.assert_called_once()

    def test_com_is_released_when_endpoint_operation_fails(self):
        import pythoncom
        from pycaw.pycaw import AudioUtilities
        with mock.patch.object(pythoncom, "CoInitializeEx"), \
                mock.patch.object(pythoncom, "CoUninitialize") as uninitialize, \
                mock.patch.object(AudioUtilities, "GetSpeakers", side_effect=OSError("no audio device")):
            with self.assertRaises(OSError):
                actions.get_volume()
        uninitialize.assert_called_once()

    def test_clipboard_is_closed_after_write_failure(self):
        import win32clipboard
        with mock.patch.object(win32clipboard, "OpenClipboard"), \
                mock.patch.object(win32clipboard, "EmptyClipboard"), \
                mock.patch.object(win32clipboard, "SetClipboardText", side_effect=OSError("failure")), \
                mock.patch.object(win32clipboard, "CloseClipboard") as close:
            with self.assertRaises(OSError):
                actions.clipboard_set("text")
        close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
