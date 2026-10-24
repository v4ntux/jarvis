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
    _t("server_shell", "Команда bash на сервере Jarvis (Ubuntu, работает 24/7, ноутбук не нужен): расчёты, python, "
       "curl, скачивание, обработка файлов. Папка по умолчанию /opt/jarvis/data/work, файлы из Telegram лежат "
       "в /opt/jarvis/data/inbox. python и pip — твоё собственное окружение, ставь пакеты свободно "
       "(pip install yt-dlp и т.п.). До 10 минут, дольше — server_job.",
       {"command": S, "timeout": I}, ["command"]),
    _t("server_job", "Запустить долгую команду bash на сервере в фоне (скачивание, конвертация, долгий скрипт). "
       "Ответ сразу, а когда команда закончится, пользователь получит сообщение с концом вывода. "
       "then — что сделать тебе самому после окончания (например прислать файл), стоит денег.",
       {"command": S, "what": {"type": "string", "description": "коротко, что делает задача"}, "then": S},
       ["command", "what"]),
    _t("server_jobs", "Список фоновых задач на сервере, которые ещё идут."),
    _t("stop_job", "Остановить фоновую задачу на сервере по номеру.", {"id": I}, ["id"]),
    _t("server_send_file", "Отправить пользователю в Telegram файл с сервера (до 50 МБ).", {"path": S}, ["path"]),
    _t("remember", "Запомнить факт о пользователе надолго (имена, предпочтения, пути, договорённости).",
       {"text": S}, ["text"]),
    _t("forget", "Удалить факт из памяти по номеру.", {"id": I}, ["id"]),
    _t("set_reminder", "Напомнить пользователю в Telegram в указанное время. "
       "Время ISO 8601 по Москве (например 2026-10-25T09:00) или через N минут.",
       {"text": S, "at": S, "in_minutes": I}, ["text"]),
    _t("list_reminders", "Список активных напоминаний."),
    _t("add_watcher",
       "Наблюдатель «если случится X — сообщи / сделай Y». Условие проверяется на сервере без ИИ, бесплатно, 24/7. "
       "source: time — по расписанию (at ЧЧ:ММ по Москве, days: daily|weekdays|weekends|mon,tue,...); "
       "laptop — команда PowerShell на ноутбуке, только чтение, ошибки глушить -ErrorAction SilentlyContinue; "
       "server — команда bash на сервере; url — текст страницы; laptop_offline — ноутбук пропал дольше minutes; "
       "laptop_online — ноутбук снова на связи. "
       "condition для laptop/server/url: changed (вывод изменился), contains / missing (value есть / нет в выводе), "
       "below / above (первое число в выводе меньше / больше value), equals (вывод равен value). "
       "every_min — как часто проверять. once=false — срабатывать каждый раз, по умолчанию один раз и удалить. "
       "notify — текст уведомления. urgent=true — «позвонить»: голосовые уведомления повторяются, пока "
       "пользователь не нажмёт «Принял», а через 5 минут без ответа — SMS и звонок робота на телефон. sms=true — сразу "
       "дублировать уведомление по SMS (платно). task — что сделать тебе самому при "
       "срабатывании, стоит денег: только если просили действие (утренний бриф, прислать новости). "
       "Примеры: загрузка закончилась → laptop, command \"(Get-ChildItem $HOME\\Downloads -Filter *.crdownload "
       "-ErrorAction SilentlyContinue).Count\", condition equals, value 0. Батарея ниже 20% → laptop, "
       "\"(Get-CimInstance Win32_Battery).EstimatedChargeRemaining\", below 20, once=false.",
       {"what": {"type": "string", "description": "коротко, что отслеживаем"},
        "source": {"type": "string", "enum": ["time", "laptop", "server", "url", "laptop_offline", "laptop_online"]},
        "command": S, "url": S,
        "condition": {"type": "string", "enum": ["changed", "contains", "missing", "below", "above", "equals"]},
        "value": S, "every_min": I, "at": S, "days": S, "minutes": I, "once": B,
        "notify": S, "urgent": B, "sms": B, "task": S},
       ["what", "source"]),
    _t("send_sms", "Отправить SMS на телефон пользователя (только ему). Доходит и без интернета. Платно, "
       "до 300 символов: пиши коротко.", {"text": S}, ["text"]),
    _t("phone_call", "Позвонить пользователю на обычный телефон: робот прочитает текст. Платно, только по "
       "просьбе или когда очень срочно.", {"text": S}, ["text"]),
    _t("list_watchers", "Список наблюдателей."),
    _t("remove_watcher", "Удалить наблюдателя по номеру.", {"id": I}, ["id"]),
]

LAPTOP_NAMES = {t["name"] for t in LAPTOP_TOOLS}
TOOLS = LAPTOP_TOOLS + SERVER_TOOLS
