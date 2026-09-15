"""Проверка локальных правил: что понимается без интернета, а что уходит в модель."""
import unittest
from unittest import mock

from core import router


class NormalizeTests(unittest.TestCase):
    def test_filler_and_punctuation(self):
        self.assertEqual(router.normalize("Открой, пожалуйста, браузер!"), "открой браузер")

    def test_yo(self):
        self.assertEqual(router.normalize("Сверни всё"), "сверни все")


class NumberTests(unittest.TestCase):
    def test_digits(self):
        self.assertEqual(router.parse_number("громкость 40"), 40)

    def test_words(self):
        self.assertEqual(router.parse_number("громкость сорок"), 40)

    def test_compound(self):
        self.assertEqual(router.parse_number("сорок пять"), 45)

    def test_default(self):
        self.assertEqual(router.parse_number("громче", 10), 10)


class RouteTests(unittest.TestCase):
    """Действия подменены: проверяем выбор правила и аргументы, а не эффект."""

    def setUp(self):
        self.calls = []
        stubs = {
            "get_time": "Сейчас 20:30.",
            "get_date": "Сегодня 22 августа, суббота.",
            "system_status": "Процессор загружен на 12 процентов, память на 60.",
            "disk_status": "C свободно 30 гигабайт.",
            "get_volume": "Громкость 40 процентов, звук включён.",
            "set_volume": "Громкость 40 процентов.",
            "change_volume": "Прибавил.",
            "set_mute": "Звук выключен.",
            "media": "Следующий трек.",
            "screenshot": "Снимок сохранён в папку Jarvis.",
            "lock_screen": "Блокирую.",
            "show_desktop": "Свернул.",
            "list_windows": "Открыто: Браузер.",
            "open_app": "Запускаю notepad.",
            "close_app": "Закрыл.",
            "focus_window": "Переключился на Блокнот.",
            "open_url": "Открываю.",
            "web_search": "Ищу.",
            "type_text": "Напечатал.",
            "power": "Отменил.",
            "clipboard_get": "текст",
            "find_files": [],
        }
        for name, value in stubs.items():
            def make(name=name, value=value):
                def fake(*args, **kwargs):
                    self.calls.append((name, args, kwargs))
                    return value
                return fake

            patcher = mock.patch.object(router.actions, name, side_effect=make())
            patcher.start()
            self.addCleanup(patcher.stop)

    def assert_action(self, phrase, action, arg=None):
        result = router.route(phrase)
        self.assertTrue(result.handled, "ушло бы в модель: %r" % phrase)
        self.assertEqual(result.action, action, "не то правило для %r" % phrase)
        if arg is not None:
            last = self.calls[-1]
            values = list(last[1]) + list(last[2].values())
            self.assertIn(arg, values, "аргумент для %r: %s" % (phrase, values))

    def test_time_and_date(self):
        self.assert_action("который час", "time")
        self.assert_action("какое сегодня число", "date")

    def test_volume(self):
        self.assert_action("поставь громкость на 40", "volume", 40)
        self.assert_action("сделай громкость сорок процентов", "volume", 40)
        self.assert_action("сделай громче", "volume", 10)
        self.assert_action("потише на 20", "volume", -20)
        self.assert_action("выключи звук", "mute", True)
        self.assert_action("включи звук", "mute", False)

    def test_apps_and_sites(self):
        self.assert_action("открой блокнот", "open_app", "блокнот")
        self.assert_action("запусти калькулятор", "open_app", "калькулятор")
        self.assert_action("закрой блокнот", "close_app", "блокнот")
        self.assert_action("открой ютуб", "site")
        self.assert_action("открой github.com", "site")

    def test_search_and_files(self):
        self.assert_action("найди в интернете рецепт борща", "search")
        self.assert_action("погугли погоду", "search")
        self.assert_action("найди файл отчет", "files")

    def test_system(self):
        self.assert_action("сделай скриншот", "screenshot")
        self.assert_action("сверни все окна", "desktop")
        self.assert_action("заблокируй экран", "lock")
        self.assert_action("что с памятью", "status")
        self.assert_action("какие окна открыты", "windows")
        self.assert_action("следующий трек", "media")
        self.assert_action("поставь на паузу", "media")

    def test_dangerous_asks_confirmation(self):
        for phrase in ("выключи компьютер", "перезагрузи компьютер"):
            result = router.route(phrase)
            self.assertTrue(result.handled)
            self.assertTrue(result.reply.startswith("__CONFIRM__"),
                            "опасное должно спрашивать подтверждение: %r" % phrase)

    def test_small_talk_is_free(self):
        for phrase in ("привет", "спасибо", "как дела", "ты меня слышишь"):
            result = router.route(phrase)
            self.assertTrue(result.handled)
            self.assertEqual(self.calls, [], "болтовня не должна дёргать действия")

    def test_dismiss(self):
        self.assertEqual(router.route("отбой").reply, "__SLEEP__")

    def test_complex_goes_to_model(self):
        for phrase in (
            "напиши письмо коллеге про перенос встречи",
            "объясни как работает двигатель",
            "что ты думаешь о моей идее",
            "переведи это на английский",
            "расскажи анекдот",
        ):
            self.assertFalse(router.route(phrase).handled,
                             "должно уходить в модель: %r" % phrase)

    def test_empty(self):
        self.assertFalse(router.route("").handled)


class WakeWordTests(unittest.TestCase):
    def test_recognises_mangled_name(self):
        from core import ears

        for heard in ("Джарвис, открой браузер", "Джармис открой браузер",
                      "Джервис", "Jarvis открой браузер", "джарвиз включи музыку"):
            called, _rest = ears.split_wake(heard)
            self.assertTrue(called, "имя не распознано: %r" % heard)

    def test_ignores_similar_words(self):
        from core import ears

        for heard in ("сервис не работает", "архив открой", "джинсы порвались",
                      "привет как дела"):
            called, _rest = ears.split_wake(heard)
            self.assertFalse(called, "ложное срабатывание: %r" % heard)

    def test_extracts_command(self):
        from core import ears

        called, rest = ears.split_wake("Джарвис, открой браузер")
        self.assertTrue(called)
        self.assertEqual(rest, "открой браузер")


if __name__ == "__main__":
    unittest.main()
