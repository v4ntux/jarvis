"""Tools the laptop can run. Names map 1:1 to functions in core/actions.py."""


def _t(name, desc, props=None, required=None):
    return {
        "name": name,
        "description": desc,
        "input_schema": {"type": "object", "properties": props or {}, "required": required or []},
    }


S = {"type": "string"}

TOOLS = [
    _t("open_app", "Запустить программу на ноутбуке по названию (блокнот, хром, телеграм, браузер...).",
       {"name": S}, ["name"]),
    _t("add_app", "Запомнить программу, которой нет в меню Пуск: название и полный путь к .exe. "
       "Потом open_app по этому названию будет её запускать.",
       {"name": S, "path": S}, ["name", "path"]),
    _t("close_app", "Закрыть программу по названию.", {"name": S}, ["name"]),
    _t("type_text", "Напечатать текст в активное окно (после open_app или focus_window).",
       {"text": S}, ["text"]),
    _t("hotkey", "Нажать сочетание клавиш, например ctrl+s, alt+tab, enter.", {"keys": S}, ["keys"]),
    _t("list_windows", "Список открытых окон.", {"limit": {"type": "integer"}}),
    _t("focus_window", "Переключиться на окно по части заголовка.", {"title": S}, ["title"]),
    _t("open_url", "Открыть сайт в браузере.", {"url": S}, ["url"]),
    _t("web_search", "Поиск в интернете в браузере.",
       {"query": S, "engine": {"type": "string", "enum": ["google", "yandex", "youtube"]}}, ["query"]),
    _t("set_volume", "Установить громкость 0-100.", {"percent": {"type": "integer"}}, ["percent"]),
    _t("set_mute", "Выключить или включить звук.", {"muted": {"type": "boolean"}}, ["muted"]),
    _t("media", "Управление плеером.",
       {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, ["action"]),
    _t("set_brightness", "Яркость экрана 0-100.", {"percent": {"type": "integer"}}, ["percent"]),
    _t("system_status", "Загрузка CPU, памяти, батарея ноутбука."),
    _t("disk_status", "Свободное место на дисках."),
    _t("open_folder", "Открыть папку: загрузки, документы, рабочий стол, изображения, видео, музыка.",
       {"name": S}, ["name"]),
    _t("find_files", "Найти файлы по имени в папках пользователя.", {"query": S}, ["query"]),
    _t("open_path", "Открыть файл или папку по полному пути.", {"path": S}, ["path"]),
    _t("minimize_window", "Свернуть активное окно."),
    _t("maximize_window", "Развернуть активное окно."),
    _t("show_desktop", "Показать рабочий стол."),
    _t("clipboard_get", "Прочитать текст из буфера обмена."),
    _t("clipboard_set", "Положить текст в буфер обмена.", {"text": S}, ["text"]),
    _t("look", "Посмотреть на активное окно: список кнопок, ссылок, полей с id и координатами. "
       "Дёшево, используй всегда перед ui_action. screenshot=true добавляет картинку окна, "
       "это дорого: только если по списку элементов не понять, куда нажать.",
       {"screenshot": {"type": "boolean"}}),
    _t("ui_action", "Действие с элементом из последнего look. После каждого действия снимок сбрасывается, "
       "для следующего действия снова вызови look. Действия: click_element {element_id, button?, count?}, "
       "type_into_element {element_id, text}, scroll {element_id, direction: up|down, amount}, "
       "press_key {keys}, click_at {x, y} (координаты на скриншоте, только после look со screenshot).",
       {"action": {"type": "string",
                   "enum": ["click_element", "type_into_element", "scroll", "press_key", "click_at"]},
        "params": {"type": "object"}, "snapshot_id": S},
       ["action", "params", "snapshot_id"]),
]
