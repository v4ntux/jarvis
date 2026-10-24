"""Vision client formatting without a GPU or a running parser."""
import os
import unittest
from unittest import mock

os.environ["JARVIS_SERVER_BRAIN"] = "0"  # tests never talk to the real server brain
from core import vision


def element(n, kind="icon", content="Settings"):
    return {"id": "v%d" % n, "type": kind, "content": content, "center": [10 * n, 20]}


class DescribeTests(unittest.TestCase):
    def test_lines_name_kind_and_click_point(self):
        text = vision.describe([element(1), element(2, "button", "Сохранить"), element(3, "text", "Итого")], 812)
        self.assertEqual(text.splitlines(), ["Локальное зрение (812 мс), координаты для click_at:",
                                             "v1 иконка «Settings» @10,20", "v2 кнопка «Сохранить» @20,20",
                                             "v3 текст «Итого» @30,20"])

    def test_long_lists_stop_on_a_whole_line_and_say_how_many_are_left(self):
        many = [element(n, content="x" * 50) for n in range(1, 200)]
        text = vision.describe(many, 1, max_chars=1000)
        self.assertLessEqual(len(text), 1000 + 40)
        last = text.splitlines()[-1]
        shown = len(text.splitlines()) - 2
        self.assertEqual(last, "(ещё %d элементов не показано)" % (199 - shown))
        self.assertTrue(all(l.endswith(",20") for l in text.splitlines()[1:-1]))

    def test_missing_install_is_a_clear_error(self):
        with mock.patch.object(vision, "_running", return_value=False), \
             mock.patch.object(vision, "installed", return_value=False):
            with self.assertRaisesRegex(vision.VisionError, "не установлено"):
                vision.parse("")


if __name__ == "__main__":
    unittest.main()
