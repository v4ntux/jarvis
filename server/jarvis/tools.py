"""Tool catalog for the brain.

LAPTOP tools run on the laptop through the hub (names match hands.py),
SERVER tools run here in the brain process.
"""


def _t(name, desc, props=None, required=None):
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props or {}, "required": required or []},
    }


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

LAPTOP_TOOLS = [
    _t("run_shell", "Выполнить команду PowerShell на ноутбуке и получить вывод. Полный доступ: файлы, "
       "программы, процессы, сеть, git, python. Предпочитай его кликам, когда задачу можно решить командой.",
       {"command": S, "timeout": {"type": "integer", "description": "секунды, по умолчанию 60, максимум 600"}},
       ["command"]),
    _t("open_app", "Запустить программу на ноутбуке по названию (блокнот, хром, телеграм, браузер...).",
       {"name": S}, ["name"]),
    _t("add_app", "Запомнить программу, которой нет в меню Пуск: название и полный путь к .exe. "
       "Потом open_app по этому названию будет её запускать.", {"name": S, "path": S}, ["name", "path"]),
    _t("close_app", "Закрыть программу по названию.", {"name": S}, ["name"]),
    _t("type_text", "Напечатать текст в активное окно (после open_app или focus_window).", {"text": S}, ["text"]),
    _t("hotkey", "Нажать сочетание клавиш, например ctrl+s, alt+tab, enter.", {"keys": S}, ["keys"]),
    _t("list_windows", "Список открытых окон.", {"limit": I}),
    _t("focus_window", "Переключиться на окно по части заголовка.", {"title": S}, ["title"]),
    _t("open_url", "Открыть сайт в браузере на ноутбуке (чтобы пользователь его увидел).", {"url": S}, ["url"]),
    _t("set_volume", "Установить громкость 0-100.", {"percent": I}, ["percent"]),
    _t("set_mute", "Выключить или включить звук.", {"muted": B}, ["muted"]),
    _t("media", "Управление плеером.",
       {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, ["action"]),
    _t("set_brightness", "Яркость экрана 0-100.", {"percent": I}, ["percent"]),
    _t("system_status", "Загрузка CPU, памяти, батарея ноутбука."),
    _t("read_file", "Прочитать текстовый файл на ноутбуке.", {"path": S}, ["path"]),
    _t("write_file", "Записать текстовый файл на ноутбуке (создаст или перезапишет).",
       {"path": S, "text": S}, ["path", "text"]),
    _t("send_file", "Отправить файл с ноутбука пользователю в Telegram.", {"path": S}, ["path"]),
    _t("send_screen", "Сделать скриншот всего экрана ноутбука и отправить пользователю в Telegram "
       "(сам ты его не видишь; чтобы посмотреть самому, используй look)."),
    _t("put_file", "Положить на ноутбук файл, который пользователь прислал в Telegram.",
       {"inbox_name": S, "path": {"type": "string", "description": "полный путь назначения на ноутбуке"}},
       ["inbox_name", "path"]),
    _t("clipboard_get", "Прочитать текст из буфера обмена."),
    _t("clipboard_set", "Положить текст в буфер обмена.", {"text": S}, ["text"]),
    _t("look", "Посмотреть на активное окно: список кнопок, ссылок, полей с id и координатами. "
       "Дёшево, используй перед ui_action. screenshot=true добавляет картинку окна, "
       "это дорого: только если по списку элементов не понять, куда нажать.", {"screenshot": B}),
    _t("ui_action", "Действие с элементом из последнего look. Результат сразу содержит свежий look окна "
       "с новым snapshot_id для следующего действия. Действия: click_element {element_id, button?, count?}, "
       "type_into_element {element_id, text}, scroll {element_id, direction: up|down, amount}, "
       "press_key {keys}, click_at {x, y} (координаты на скриншоте, только после look со screenshot).",
       {"action": {"type": "string",
                   "enum": ["click_element", "type_into_element", "scroll", "press_key", "click_at"]},
        "params": {"type": "object"}, "snapshot_id": S},
       ["action", "params", "snapshot_id"]),
]

SERVER_TOOLS = [
    _t("web_search", "Поиск в интернете (бесплатно, на сервере). news=true для свежих новостей.",
       {"query": S, "limit": I, "news": B}, ["query"]),
    _t("read_url", "Скачать страницу и получить её текст (статьи, документация, новости).", {"url": S}, ["url"]),
    _t("server_shell", "Команда bash на сервере Jarvis (Ubuntu): расчёты, python, curl, обработка файлов "
       "из Telegram (лежат в /opt/jarvis/data/inbox).", {"command": S, "timeout": I}, ["command"]),
    _t("remember", "Запомнить факт о пользователе надолго (имена, предпочтения, пути, договорённости).",
       {"text": S}, ["text"]),
    _t("forget", "Удалить факт из памяти по номеру.", {"id": I}, ["id"]),
    _t("set_reminder", "Напомнить пользователю в Telegram в указанное время. "
       "Время ISO 8601 по Москве (например 2026-10-25T09:00) или через N минут.",
       {"text": S, "at": S, "in_minutes": I}, ["text"]),
    _t("list_reminders", "Список активных напоминаний."),
]

LAPTOP_NAMES = {t["name"] for t in LAPTOP_TOOLS}
TOOLS = LAPTOP_TOOLS + SERVER_TOOLS
