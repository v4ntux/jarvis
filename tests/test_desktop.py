"""Grounding and concurrency regressions; no operation touches a real application."""
import copy
import json
import os
import threading
import time
import unittest
from unittest import mock

from core import desktop, desktop_lock, skills


class FakeNative:
    def __init__(self):
        self.hwnd = 100
        self.window_info = {"hwnd": 100, "pid": os.getpid() + 100, "title": "Test window",
                            "rect": (0, 0, 800, 600), "created": 123}
        self.desktop_info = {"left": -800, "top": 0, "width": 1600, "height": 600}
        self.data = {
            "root": self.item(1, "window", "Test window", (0, 0, 800, 600)),
            "button": self.item(2, "button", "Continue", (20, 20, 120, 60)),
            "edit": self.item(3, "edit", "Name", (20, 80, 220, 120), editable=True, value="initial"),
            "password": self.item(4, "edit", "private-name", (20, 140, 220, 180),
                                  editable=True, value="private-secret", password=True),
            "secret-child": self.item(5, "text", "hidden-child-secret", (25, 145, 215, 175)),
        }
        self.child_map = {"root": ["button", "edit", "password"], "password": ["secret-child"]}
        self.calls = []

    @staticmethod
    def item(runtime, role, name, rect, **extra):
        return {"runtime_id": (42, runtime), "role": role, "name": name, "rect": rect,
                "enabled": True, "offscreen": False, "password": False, "editable": False,
                "value": "", **extra}

    def foreground(self):
        return self.hwnd

    def window(self, hwnd):
        return copy.deepcopy(self.window_info) if hwnd == self.window_info["hwnd"] else None

    def desktop(self):
        return dict(self.desktop_info)

    def root(self, hwnd):
        return "root"

    def describe(self, element):
        return copy.deepcopy(self.data[element])

    def refresh(self, element):
        return element

    def children(self, element, limit, deadline, stop_flag):
        return iter(self.child_map.get(element, [])[:limit])

    def may_have_children(self, element):
        return bool(self.child_map.get(element))

    def screenshot(self, rect, passwords):
        self.calls.append(("screenshot", rect, passwords))
        return {"width": 400, "height": 300, "base64": "synthetic-image", "mime_type": "image/png",
                "origin": {"x": rect[0], "y": rect[1]}, "original_width": 800, "original_height": 600}

    def click(self, element, hwnd, rect, button, count, stop_flag):
        desktop._check_stop(stop_flag)
        self.calls.append(("click", element, hwnd, rect, button, count))

    def click_at(self, hwnd, point, button, count, stop_flag):
        desktop._check_stop(stop_flag)
        self.calls.append(("click_at", hwnd, point, button, count))

    def set_value(self, element, text, stop_flag):
        desktop._check_stop(stop_flag)
        self.calls.append(("set_value", element, text))
        self.data[element]["value"] = text

    def scroll(self, element, hwnd, rect, direction, amount, stop_flag):
        desktop._check_stop(stop_flag)
        self.calls.append(("scroll", element, direction, amount))

    def press(self, keys):
        self.calls.append(("press", keys))


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.native = FakeNative()
        self.engine = desktop.DesktopEngine(self.native)

    def observe(self, screenshot=False):
        self.snapshot = self.engine.observe(screenshot)
        self.ids = {item["name"]: item["id"] for item in self.snapshot["elements"]}
        return self.snapshot

    def execute(self, action="click_element", **params):
        return self.engine.execute(action, params or {"element_id": self.ids["Continue"]},
                                   self.snapshot["snapshot_id"])

    def test_observation_is_json_and_does_not_capture_screen_by_default(self):
        snapshot = self.observe()
        json.dumps(snapshot)
        self.assertIsNone(snapshot["screenshot"])
        self.assertEqual(self.native.calls, [])

    def test_password_metadata_and_descendants_are_omitted(self):
        text = json.dumps(self.observe())
        self.assertNotIn("private-secret", text)
        self.assertNotIn("private-name", text)
        self.assertNotIn("hidden-child-secret", text)
        self.assertTrue(any(item["password"] for item in self.snapshot["elements"]))

    def test_explicit_screenshot_uses_window_bounds_and_masks_passwords(self):
        self.observe(True)
        self.assertEqual(self.native.calls, [("screenshot", (0, 0, 800, 600), [(20, 140, 220, 180)])])

    def test_snapshot_ids_change_but_element_ids_stay_stable(self):
        first = self.observe()
        second = self.observe()
        self.assertNotEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["elements"], second["elements"])

    def test_own_assistant_window_is_rejected(self):
        self.native.window_info["pid"] = os.getpid()
        with self.assertRaises(desktop.DesktopError):
            self.observe()

    def test_element_budget_is_enforced(self):
        self.engine.max_elements = 2
        snapshot = self.observe()
        self.assertEqual(len(snapshot["elements"]), 2)
        self.assertTrue(snapshot["truncated"])

    def test_offscreen_elements_are_not_actionable(self):
        self.native.data["button"]["offscreen"] = True
        self.observe()
        self.assertNotIn("Continue", self.ids)

    def test_stale_snapshot_cannot_act(self):
        first = self.observe()
        self.observe()
        result = self.engine.execute("press_key", {"keys": "enter"}, first["snapshot_id"])
        self.assertFalse(result.ok)
        self.assertEqual(self.native.calls, [])

    def test_changed_foreground_cannot_act(self):
        self.observe()
        self.native.hwnd = 999
        self.assertFalse(self.execute().ok)
        self.assertEqual(self.native.calls, [])

    def test_changed_window_title_cannot_act(self):
        self.observe()
        self.native.window_info["title"] = "Different document"
        self.assertFalse(self.execute().ok)
        self.assertEqual(self.native.calls, [])

    def test_reused_window_handle_with_different_process_creation_is_rejected(self):
        self.observe()
        self.native.window_info["created"] = 456
        self.assertFalse(self.execute().ok)
        self.assertEqual(self.native.calls, [])

    def test_changed_element_bounds_are_rejected(self):
        self.observe()
        self.native.data["button"]["rect"] = (200, 200, 300, 240)
        self.assertFalse(self.execute().ok)
        self.assertEqual(self.native.calls, [])

    def test_changed_element_runtime_identity_is_rejected(self):
        self.observe()
        self.native.data["button"]["runtime_id"] = (999, 888)
        self.assertFalse(self.execute().ok)
        self.assertEqual(self.native.calls, [])

    def test_changed_edit_value_is_not_overwritten_from_old_observation(self):
        self.observe()
        self.native.data["edit"]["value"] = "new user input"
        result = self.execute("type_into_element", element_id=self.ids["Name"], text="replacement")
        self.assertFalse(result.ok)
        self.assertEqual(self.native.calls, [])

    def test_password_element_cannot_be_clicked(self):
        self.observe()
        result = self.execute("click_element", element_id=self.ids[""])
        self.assertFalse(result.ok)
        self.assertEqual(self.native.calls, [])

    def test_unknown_element_is_not_guessed(self):
        self.observe()
        self.assertFalse(self.execute("click_element", element_id="invented").ok)
        self.assertEqual(self.native.calls, [])

    def test_click_is_once_then_snapshot_is_invalidated(self):
        self.observe()
        self.assertTrue(self.execute().ok)
        self.assertFalse(self.execute().ok)
        self.assertEqual(len(self.native.calls), 1)

    def test_right_and_double_clicks_are_explicit(self):
        self.observe()
        self.assertTrue(self.execute("click_element", element_id=self.ids["Continue"], button="right", count=2).ok)
        self.assertEqual(self.native.calls[0][-2:], ("right", 2))

    def test_typing_updates_observed_edit_field(self):
        self.observe()
        result = self.execute("type_into_element", element_id=self.ids["Name"], text="Привет!")
        self.assertTrue(result.ok)
        self.assertEqual(self.native.calls, [("set_value", "edit", "Привет!")])
        self.observe()
        self.assertEqual(next(item["value"] for item in self.snapshot["elements"] if item["name"] == "Name"), "Привет!")

    def test_cancellation_prevents_any_input(self):
        self.observe()
        result = self.engine.execute("click_element", {"element_id": self.ids["Continue"]},
                                     self.snapshot["snapshot_id"], stop_flag=lambda: True)
        self.assertFalse(result.ok)
        self.assertEqual(self.native.calls, [])

    def test_scroll_amount_is_bounded(self):
        self.observe()
        self.assertFalse(self.execute("scroll", element_id=self.ids["Continue"], amount=100).ok)
        self.assertEqual(self.native.calls, [])

    def test_coordinates_require_explicit_screenshot(self):
        self.observe()
        self.assertFalse(self.execute("click_at", x=100, y=100).ok)
        self.assertEqual(self.native.calls, [])

    def test_image_coordinates_scale_to_original_window(self):
        self.observe(True)
        self.native.calls.clear()
        self.assertTrue(self.execute("click_at", x=100, y=100).ok)
        self.assertEqual(self.native.calls, [("click_at", 100, (201, 201), "left", 1)])

    def test_returned_snapshot_cannot_change_internal_coordinate_mapping(self):
        self.observe(True)
        self.snapshot["screenshot"]["width"] = 800
        self.native.calls.clear()
        self.assertTrue(self.execute("click_at", x=100, y=100).ok)
        self.assertEqual(self.native.calls[0][2], (201, 201))

    def test_negative_virtual_desktop_origin_is_supported(self):
        self.native.window_info["rect"] = (-800, 0, 0, 600)
        for item in self.native.data.values():
            left, top, right, bottom = item["rect"]
            item["rect"] = (left - 800, top, right - 800, bottom)
        self.observe(True)
        self.native.calls.clear()
        self.assertTrue(self.execute("click_at", x=100, y=100).ok)
        self.assertEqual(self.native.calls[0][2], (-599, 201))

    def test_coordinates_outside_screenshot_are_rejected(self):
        for x, y in ((-1, 0), (400, 10), (10, 300), (True, 10), (1.2, 10)):
            with self.subTest(x=x, y=y):
                self.observe(True)
                self.native.calls.clear()
                self.assertFalse(self.execute("click_at", x=x, y=y).ok)
                self.assertEqual(self.native.calls, [])

    def test_coordinates_cannot_target_masked_password_control(self):
        self.observe(True)
        self.native.calls.clear()
        self.assertFalse(self.execute("click_at", x=20, y=80).ok)
        self.assertEqual(self.native.calls, [])


class LeaseTests(unittest.TestCase):
    def test_same_thread_lease_is_reentrant(self):
        with desktop_lock.acquire() as first:
            with desktop_lock.acquire() as second:
                self.assertTrue(first and second)

    def test_other_thread_fails_fast_while_desktop_is_leased(self):
        results = []
        def probe():
            with desktop_lock.acquire() as acquired:
                results.append(acquired)
        with desktop_lock.acquire() as acquired:
            worker = threading.Thread(target=probe)
            worker.start()
            worker.join(1)
        self.assertTrue(acquired)
        self.assertEqual(results, [False])

    def test_lingering_provider_call_blocks_new_desktop_actions(self):
        desktop_lock.IN_FLIGHT.set()
        try:
            with desktop_lock.acquire() as acquired:
                self.assertFalse(acquired)
        finally:
            desktop_lock.IN_FLIGHT.clear()

    def test_volume_can_run_while_desktop_provider_is_busy(self):
        desktop_lock.IN_FLIGHT.set()
        try:
            with mock.patch.object(skills, "_execute_unlocked", return_value=skills.ActionResult(True, "done")) as execute:
                self.assertTrue(skills.execute("get_volume").ok)
                self.assertFalse(skills.execute("open_app", {"name": "test"}).ok)
            execute.assert_called_once_with("get_volume", None)
        finally:
            desktop_lock.IN_FLIGHT.clear()


class WorkerTests(unittest.TestCase):
    def test_native_adapter_is_created_on_dedicated_worker(self):
        created_on = []
        def factory():
            created_on.append(threading.get_ident())
            return FakeNative()
        worker = desktop.DesktopWorker(factory)
        snapshot = worker.call("observe", False)
        self.assertTrue(snapshot["snapshot_id"])
        self.assertNotEqual(created_on, [threading.get_ident()])

    def test_timed_out_provider_holds_busy_state_and_cannot_act_later(self):
        entered, release = threading.Event(), threading.Event()
        native = FakeNative()
        def factory():
            entered.set()
            release.wait(2)
            return native
        worker = desktop.DesktopWorker(factory)
        try:
            with self.assertRaises(desktop.DesktopError):
                worker.call("observe", False, timeout=.05)
            self.assertTrue(entered.is_set())
            self.assertTrue(desktop_lock.IN_FLIGHT.is_set())
            with desktop_lock.acquire() as acquired:
                self.assertFalse(acquired)
        finally:
            release.set()
            deadline = time.monotonic() + 2
            while desktop_lock.IN_FLIGHT.is_set() and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(desktop_lock.IN_FLIGHT.is_set())
        self.assertEqual(native.calls, [])

    def test_cancelled_request_is_not_enqueued(self):
        created = []
        def factory():
            created.append(True)
            return FakeNative()
        worker = desktop.DesktopWorker(factory)
        with self.assertRaises(desktop.DesktopError):
            worker.call("observe", False, stop_flag=lambda: True)
        self.assertEqual(created, [])


if __name__ == "__main__":
    unittest.main()
