"""core/probe.py: the laptop's facts (windows, programs, sound) and its mouse. Windows only; nothing here clicks."""
import sys
import unittest

if sys.platform != "win32":
    raise unittest.SkipTest("Windows only")

from core import probe


class NamesTest(unittest.TestCase):
    def test_how_people_say_programs(self):
        self.assertEqual(probe._wanted("клауд"), "claude")
        self.assertEqual(probe._wanted("Claude"), "claude")
        self.assertEqual(probe._wanted("телеграм"), "telegram")
        self.assertEqual(probe._wanted("яндекс музыка"), "yandex music")

    def test_matching_is_by_whole_name_first(self):
        self.assertEqual(probe._score("claude", "Claude"), 100)
        self.assertGreater(probe._score("claude", "Claude Code"), 0)
        self.assertLess(probe._score("claude", "Claude Code"), probe._score("claude", "Claude"))
        self.assertEqual(probe._score("claude", "Calculator"), 0)

    def test_kinds(self):
        self.assertEqual(probe.kind_of("WindowsTerminal.exe"), "terminal")
        self.assertEqual(probe.kind_of("brave.exe"), "browser")
        self.assertEqual(probe.kind_of("claude.exe"), "app")

    def test_a_store_app_is_started_by_its_appid_not_its_folder(self):
        path = r"C:\Program Files\WindowsApps\Claude_3.6556.2.0_x64__pzs8sxrjxfjjc\app\claude.exe"
        found = probe.re.search(r"\\WindowsApps\\([^\\_]+)_[^\\_]+_[^\\_]+__([^\\]+)\\", path)
        self.assertEqual((found.group(1), found.group(2)), ("Claude", "pzs8sxrjxfjjc"))
        self.assertEqual(probe._store_appid(r"C:\Tools\app.exe"), "")


class ScreenTest(unittest.TestCase):
    def test_clicks_are_mapped_from_the_screenshot_to_the_screen(self):
        probe.Screen.scale, probe.Screen.width, probe.Screen.height = 2 / 3, 1920, 1080
        self.assertEqual(probe._real(640, 360), (960, 540))
        self.assertEqual(probe._real(0, 0), (0, 0))
        self.assertEqual(probe._real(5000, 5000), (1919, 1079))  # never off the screen

    def test_a_screenshot_fits_the_model(self):
        shot = probe.screenshot()
        self.assertIn("1280x720", shot["text"]) if probe.Screen.width == 1920 else None
        self.assertLessEqual(max(probe.Screen.width * probe.Screen.scale, probe.Screen.height * probe.Screen.scale), 1281)
        self.assertTrue(shot["image"])

    def test_unknown_actions_are_refused(self):
        with self.assertRaises(ValueError):
            probe.screen_do("format_disk")

    def test_keys(self):
        self.assertEqual(probe._vk("ctrl"), 0x11)
        self.assertEqual(probe._vk("Return"), 0x0D)
        self.assertEqual(probe._vk("f5"), 0x74)
        with self.assertRaises(ValueError):
            probe._vk("несуществующая")


class WindowsTest(unittest.TestCase):
    def test_every_window_has_a_title_a_process_and_a_kind(self):
        for w in probe.windows():
            self.assertTrue(w["title"])
            self.assertIn(w["kind"], ("app", "terminal", "browser"))

    def test_nothing_called_that_is_closed_politely(self):
        self.assertTrue(probe.close_window("такого окна точно нет 123").startswith("Ошибка"))


if __name__ == "__main__":
    unittest.main()
