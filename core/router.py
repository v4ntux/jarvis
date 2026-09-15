"""Pure local command planning; execution belongs to Session.

``route(..., execute=False)`` is the production boundary. The default execution
adapter is retained for old command-line integrations.
"""
import re
import time
from dataclasses import dataclass, field

from core import actions, memory, skills

MAX_STEPS = 8
_PUNCT = re.compile(r"[^\w\s%+-]", re.UNICODE)
_FILLER = re.compile(r"\b(пожалуйста|будь добр\w*|прошу|давай|ну-ка|ну)\b")


def normalize(text):
    low = _PUNCT.sub(" ", str(text or "").lower().replace("ё", "е"))
    return re.sub(r"\s+", " ", _FILLER.sub(" ", low)).strip()


_ONES = {"ноль": 0, "один": 1, "одну": 1, "два": 2, "две": 2, "три": 3,
         "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
         "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12,
         "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
         "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
_TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
         "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
         "девяносто": 90, "сто": 100}


def parse_number(text, default=None):
    digits = re.search(r"-?\d+", text or "")
    if digits:
        return int(digits.group())
    words = normalize(text).split()
    for i, word in enumerate(words):
        if word in _TENS:
            return _TENS[word] + (_ONES.get(words[i + 1], 0) if i + 1 < len(words) and _TENS[word] < 100 else 0)
        if word in _ONES:
            return _ONES[word]
    return default


def parse_duration(text, default_minutes=None):
    value = parse_number(text)
    if value is None:
        return None if default_minutes is None else default_minutes * 60
    if re.search(r"\b(сек\w*)\b", text):
        return value
    if re.search(r"\b(час\w*)\b", text):
        return value * 3600
    if re.search(r"\b(дн\w*|день|дня|дней)\b", text):
        return value * 86400
    return value * 60


@dataclass
class Result:
    reply: str = ""
    handled: bool = True
    action: str = ""
    skill: str = ""
    params: dict = field(default_factory=dict)
    steps: list = field(default_factory=list)


UNKNOWN = Result(handled=False)
SITES = {"ютуб": "youtube.com", "ютьюб": "youtube.com", "youtube": "youtube.com",
         "гугл": "google.com", "google": "google.com", "яндекс": "ya.ru",
         "вк": "vk.com", "вконтакте": "vk.com", "гитхаб": "github.com", "github": "github.com",
         "чатгпт": "chatgpt.com", "chatgpt": "chatgpt.com", "твитч": "twitch.tv",
         "википедия": "ru.wikipedia.org", "почта": "mail.google.com", "гмейл": "mail.google.com"}


def _step(name, params=None, action=None):
    return Result(action=action or name, skill=name, params=params or {},
                  steps=[{"skill": name, "params": params or {}}])


# Payload commands use the ORIGINAL string: punctuation and casing are data.
_RAW = (
    (r"^(?:напечатай|напиши в (?:окне|блокноте)|введи)\s+(.+)$", "type_text", "text", "type"),
    (r"^(?:запиши|сохрани|добавь)\s+заметку\s+(.+)$", "add_note", "text", "note"),
    (r"^(?:переключись на\s+(?:окно\s+)?|покажи окно\s+|разверни\s+(?:окно\s+)?)(.+)$", "focus_window", "title", "focus"),
)
_IMPERATIVES = r"(?:открой|запусти|включи|выключи|поставь|установи|сделай|напечатай|введи|найди|поищи|погугли|закрой|сверни|нажми|прокрути|переключись|запиши|сохрани|заблокируй|перезагрузи|убавь|прибавь|уменьши|увеличь)"
_SPLIT = re.compile(r"(?:\s*;\s*|\s*,?\s+(?:и затем|а затем|а потом|затем|потом|после этого|и)\s+)(?=" + _IMPERATIVES + r"\b)", re.I)


def _split_commands(raw):
    # A text payload is opaque, including words such as 'и открой'.
    output = []
    remainder = raw
    while len(output) <= MAX_STEPS:
        if any(re.match(pattern, remainder, re.I | re.S) for pattern, *_ in _RAW[:2]):
            output.append(remainder)
            return output
        match = _SPLIT.search(remainder)
        if not match:
            output.append(remainder)
            return output
        output.append(remainder[:match.start()].strip())
        remainder = remainder[match.end():].strip()
    return output + [remainder]


def parse(text, title="сэр"):
    raw = str(text or "").strip()
    if not raw:
        return UNKNOWN
    raw = re.sub(r"^(?:джарвис|jarvis)[,\s]+", "", raw, flags=re.I)
    raw = re.sub(r"^(?:пожалуйста[,\s]+|будь добр[,\s]+)", "", raw, flags=re.I)
    parts = _split_commands(raw)
    if len(parts) > MAX_STEPS:
        return Result("Слишком много шагов. Разделите задачу на части.", action="clarify")
    results = [_parse_one(part, title) for part in parts]
    if any(not item.handled for item in results):
        return UNKNOWN  # The entire request must be understood before any effect.
    if len(results) == 1:
        return results[0]
    if any(not item.steps for item in results):
        return UNKNOWN
    return Result(action="plan", steps=[step for item in results for step in item.steps])


def _parse_one(raw, title):
    # Strip polite command prefixes without ever cleaning the payload itself.
    raw = re.sub(r"^((?:открой|запусти|напечатай|введи))\s*,?\s*пожалуйста\s*,?\s*", r"\1 ", raw, flags=re.I)
    text = normalize(raw)
    if text in ("отбой", "свободен", "спасибо все", "спасибо это все", "это все", "хватит", "стоп", "отмена", "можешь спать", "закончили"):
        return Result("__SLEEP__", action="sleep")
    if re.fullmatch(r"(?:привет|здравствуй\w*|хай|доброе утро|добрый день|добрый вечер|здорово)", text):
        return Result("Здравствуйте, %s. Слушаю." % title, action="greeting")
    if text in ("спасибо", "благодарю", "отлично", "молодец", "супер", "класс"):
        return Result("Рад помочь.", action="thanks")
    if text in ("как дела", "как ты", "как сам", "ты тут", "ты здесь", "ты меня слышишь"):
        return Result("Я здесь, %s. Слушаю." % title, action="ping")
    if re.fullmatch(r"(?:начни|включи|запусти) (?:режим )?диктовк\w*", text):
        return Result("__DICTATION_START__", action="dictation")
    if re.fullmatch(r"(?:останови|выключи|закончи|стоп) (?:режим )?диктовк\w*", text):
        return Result("__DICTATION_STOP__", action="dictation")
    if re.match(r"^(перепиши|улучши|исправь|сократи|переформулируй|переведи)\b", text) and re.search(r"\bбуфер\w*\b", text):
        return Result("__CLIPBOARD_TRANSFORM__", action="clipboard_transform", params={"instruction": raw})
    if re.fullmatch(r"(?:верни|восстанови) (?:прошл\w+ )?буфер", text):
        return Result("__CLIPBOARD_RESTORE__", action="clipboard_restore")
    for pattern, name, key, action in _RAW:
        match = re.match(pattern, raw, re.I | re.S)
        if match and text != "разверни окно":
            return _step(name, {key: match.group(1)}, action)

    exact = {
        "время": ("time", "time"), "который час": ("time", "time"), "сколько времени": ("time", "time"),
        "сколько сейчас времени": ("time", "time"), "скажи время": ("time", "time"),
        "дата": ("date", "date"), "какое сегодня число": ("date", "date"), "какое число": ("date", "date"),
        "какой сегодня день": ("date", "date"), "сегодняшняя дата": ("date", "date"),
        "статус": ("status", "status"), "диагностика": ("status", "status"), "состояние": ("status", "status"),
        "что с памятью": ("status", "status"), "что с компьютером": ("status", "status"),
        "статус компьютера": ("status", "status"), "состояние компьютера": ("status", "status"),
        "что с дисками": ("disks", "disks"), "сколько места на диске": ("disks", "disks"),
        "громкость": ("get_volume", "volume"), "какая громкость": ("get_volume", "volume"),
        "яркость": ("get_brightness", "brightness"), "какая яркость": ("get_brightness", "brightness"),
        "скриншот": ("screenshot", "screenshot"), "сделай скриншот": ("screenshot", "screenshot"),
        "сделай снимок экрана": ("screenshot", "screenshot"),
        "заблокируй": ("lock", "lock"), "заблокируй экран": ("lock", "lock"), "заблокируй компьютер": ("lock", "lock"),
        "сверни все окна": ("show_desktop", "desktop"), "сверни все": ("show_desktop", "desktop"),
        "покажи рабочий стол": ("show_desktop", "desktop"),
        "сверни окно": ("minimize_window", "window"), "разверни окно": ("maximize_window", "window"),
        "окна": ("list_windows", "windows"), "какие окна открыты": ("list_windows", "windows"),
        "какие программы запущены": ("list_windows", "windows"), "что открыто": ("list_windows", "windows"),
        "что в буфере": ("clipboard", "clipboard"), "что в буфере обмена": ("clipboard", "clipboard"),
        "заметки": ("list_notes", "note"), "покажи заметки": ("list_notes", "note"), "прочитай заметки": ("list_notes", "note"),
        "напоминания": ("list_reminders", "reminder"), "покажи напоминания": ("list_reminders", "reminder"),
        "выключи компьютер": ("shutdown", "power"), "перезагрузи компьютер": ("restart", "power"),
        "перейди в спящий режим": ("sleep_pc", "sleep_pc"), "усни": ("sleep_pc", "sleep_pc"),
        "отмени выключение": ("cancel_power", "power"), "отмени перезагрузку": ("cancel_power", "power"),
    }
    if text in exact:
        name, action = exact[text]
        return _step(name, action=action)
    if text in ("выключи звук", "отключи звук", "убери звук", "заглуши звук", "без звука", "тишина", "мьют"):
        return _step("mute", {"on": True}, "mute")
    if text in ("включи звук", "верни звук", "восстанови звук", "со звуком"):
        return _step("mute", {"on": False}, "mute")
    match = re.fullmatch(r"(?:(?:поставь|сделай|установи|выстави|включи) )?(громкость|яркость)(?: на)? (.+?)(?: процентов| процента| процент|%)?", text)
    if match:
        value = parse_number(match.group(2))
        if value is not None:
            return _step("set_volume" if match.group(1) == "громкость" else "set_brightness", {"percent": value}, "volume" if match.group(1) == "громкость" else "brightness")
    match = re.fullmatch(r"(?:сделай )?(громче|погромче|тише|потише|ярче|темнее|потемнее|посветлее)(?: на (.+))?", text)
    if match:
        word = match.group(1)
        bright = word in ("ярче", "темнее", "потемнее", "посветлее")
        direction = -1 if word in ("тише", "потише", "темнее", "потемнее") else 1
        return _step("change_brightness" if bright else "change_volume", {"delta": direction * abs(parse_number(match.group(2), 15 if bright else 10))}, "brightness" if bright else "volume")
    match = re.fullmatch(r"(прибавь|добавь|увеличь|убавь|уменьши) (громкость|звук|яркость)(?: на (.+))?", text)
    if match:
        bright = match.group(2) == "яркость"
        direction = -1 if match.group(1) in ("убавь", "уменьши") else 1
        return _step("change_brightness" if bright else "change_volume", {"delta": direction * abs(parse_number(match.group(3), 15 if bright else 10))}, "brightness" if bright else "volume")
    media = {"следующий трек": "next", "следующая песня": "next", "дальше": "next", "предыдущий трек": "previous", "назад": "previous", "пауза": "play_pause", "поставь на паузу": "play_pause", "продолжи": "play_pause", "продолжи музыку": "play_pause", "останови музыку": "stop"}
    if text in media:
        return _step("media", {"what": media[text]}, "media")
    match = re.match(r"^(?:поставь|запусти|создай) таймер на (.+)$", text)
    if match:
        seconds = parse_duration(match.group(1))
        return _step("add_reminder", {"text": "Таймер", "delay_seconds": seconds}, "reminder") if seconds and seconds > 0 else Result("На сколько поставить таймер?", action="clarify")
    match = re.match(r"^напомни\s+через\s+(.+?)\s+(?:что|о том чтобы|про)\s+(.+)$", raw, re.I)
    if match:
        seconds = parse_duration(normalize(match.group(1)))
        return _step("add_reminder", {"text": match.group(2), "delay_seconds": seconds}, "reminder") if seconds and seconds > 0 else Result("Через сколько напомнить?", action="clarify")
    match = re.match(r"^(?:запусти|включи|выполни) (?:режим|рутину|сценарий) (.+)$", raw, re.I)
    if match:
        return _step("run_routine", {"name": match.group(1)}, "routine")
    match = re.match(r"^(найди|открой)\s+(?:файл\w*|папк\w*)\s+(.+)$", raw, re.I)
    if match:
        return _step("open_file" if match.group(1).lower() == "открой" else "find_files", {"query": match.group(2)}, "files")
    match = re.match(r"^(?:найди|поищи|погугли|поиск)\s+(?:в\s+(интернете|гугле|google|ютубе|youtube|яндексе)\s+)?(.+)$", raw, re.I)
    if match:
        engine = (match.group(1) or "google").lower()
        engine = "youtube" if engine in ("ютубе", "youtube") else "yandex" if engine == "яндексе" else "google"
        return _step("web_search", {"query": match.group(2), "engine": engine}, "search")
    match = re.match(r"^(?:открой|запусти|включи|зайди на)\s+(?:сайт\s+)?(.+)$", raw, re.I)
    if match:
        name = match.group(1).strip().rstrip("!?,")
        folder = normalize(name)
        if folder in ("загрузки", "документы", "рабочий стол", "изображения", "картинки", "видео", "музыка", "downloads", "documents", "desktop", "pictures", "videos", "music"):
            return _step("open_folder", {"name": folder}, "files")
        site = SITES.get(normalize(name))
        if site:
            return _step("open_url", {"url": site}, "site")
        if re.fullmatch(r"(?:https?://)?[\w.-]+\.[a-zA-Z]{2,}(?:[/:?#].*)?", name):
            return _step("open_url", {"url": name}, "site")
        if re.search(r"\b(?:и|затем|потом)\b", name):
            return UNKNOWN
        return _step("open_app", {"name": name}, "open_app")
    match = re.match(r"^закрой\s+(.+)$", raw, re.I)
    if match:
        return _step("close_app", {"name": match.group(1)}, "close_app")
    match = re.match(r"^нажми\s+(.+)$", raw, re.I)
    if match:
        return _step("hotkey", {"keys": match.group(1)}, "keyboard")
    match = re.fullmatch(r"(?:прокрути|листай) (вниз|вверх)(?: на (\d+))?", text)
    if match:
        return _step("scroll", {"direction": "down" if match.group(1) == "вниз" else "up", "amount": int(match.group(2) or 3)}, "mouse")
    if text in ("кликни", "нажми мышкой", "двойной клик", "правый клик"):
        return _step("click_mouse", {"button": "right" if text == "правый клик" else "left", "count": 2 if text == "двойной клик" else 1}, "mouse")
    return UNKNOWN


def route(text, title="сэр", safe=False, execute=True):
    result = parse(text, title)
    if not execute or not result.steps:
        return result
    replies = []
    # Compatibility adapter. Session always opts out and owns all execution.
    direct = {"time": "get_time", "date": "get_date", "status": "system_status",
              "disks": "disk_status", "get_volume": "get_volume", "screenshot": "screenshot",
              "lock": "lock_screen", "show_desktop": "show_desktop", "list_windows": "list_windows"}
    for step in result.steps:
        name, params = step["skill"], step["params"]
        if name in ("shutdown", "restart", "sleep_pc") or (safe and skills.is_dangerous(name)):
            result.reply = "__CONFIRM__%s|%s" % (skills.danger_text(name), name)
            result.skill, result.params = name, params
            return result
        if name in direct:
            reply = getattr(actions, direct[name])(**params)
        else:
            reply = skills.run(name, params)
        replies.append(str(reply or ""))
    result.reply = " ".join(replies)
    return result


def rules_count():
    return len(skills.SKILLS)
