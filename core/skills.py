"""Typed skill registry and execution boundary for local Windows actions."""
import math
import time
import logging
from dataclasses import dataclass, field

from core import actions, memory


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    data: dict = field(default_factory=dict)


class Skill:
    def __init__(self, name, about, params=None, danger=None, run=None,
                 category="system", risk=0, reversible=False):
        self.name = name
        self.about = about
        self.params = params or {}
        self.danger = danger
        self.run = run
        self.category = category
        self.risk = max(0, min(3, int(risk)))
        self.reversible = bool(reversible)


SKILLS = {}


def add(skill):
    SKILLS[skill.name] = skill
    return skill


def _spec(description, kind="str", minimum=None, maximum=None, required=False, choices=None):
    return {"description": description, "type": kind, "min": minimum,
            "max": maximum, "required": required, "choices": choices}


# Audio/display --------------------------------------------------------------
add(Skill("get_volume", "узнать громкость и состояние звука", run=actions.get_volume,
          category="media"))
add(Skill("get_brightness", "узнать яркость встроенного экрана", run=actions.brightness_status,
          category="display"))
add(Skill("set_volume", "установить громкость в процентах",
          {"percent": _spec("целое 0-100", "int", 0, 100)},
          run=lambda percent=50: actions.set_volume(percent), category="media", reversible=True))
add(Skill("change_volume", "изменить громкость",
          {"delta": _spec("целое от -100 до 100", "int", -100, 100)},
          run=lambda delta=10: actions.change_volume(delta), category="media", reversible=True))
add(Skill("mute", "выключить или включить звук",
          {"on": _spec("true выключить, false включить", "bool")},
          run=lambda on=True: actions.set_mute(on), category="media", reversible=True))
add(Skill("media", "управление плеером",
          {"what": _spec("play_pause, next, previous или stop", choices=("play_pause", "next", "previous", "stop"))},
          run=lambda what="play_pause": actions.media(what), category="media", reversible=True))
add(Skill("set_brightness", "установить яркость экрана",
          {"percent": _spec("целое 0-100", "int", 0, 100)},
          run=lambda percent=50: actions.set_brightness(percent), category="display", reversible=True))
add(Skill("change_brightness", "изменить яркость экрана",
          {"delta": _spec("целое от -100 до 100", "int", -100, 100)},
          run=lambda delta=10: actions.change_brightness(delta), category="display", reversible=True))

# Programs/windows -----------------------------------------------------------
add(Skill("open_app", "запустить программу по названию",
          {"name": _spec("название программы", required=True)},
          run=lambda name="": actions.open_app(name) or "Не нашёл такую программу.",
          category="windows"))
add(Skill("close_app", "закрыть программу",
          {"name": _spec("название программы", required=True)},
          danger="закрыть программу", risk=2, category="windows",
          run=lambda name="": actions.close_app(name) or "Такая программа не запущена."))
add(Skill("focus_window", "переключиться на окно",
          {"title": _spec("часть заголовка", required=True)}, category="windows",
          run=lambda title="": actions.focus_window(title) or "Такого окна нет."))
add(Skill("list_windows", "перечислить открытые окна", run=actions.list_windows,
          category="windows"))
add(Skill("show_desktop", "свернуть все окна", run=actions.show_desktop,
          category="windows", reversible=True))

add(Skill("minimize_window", "свернуть выбранное окно", run=actions.minimize_window,
          category="windows", reversible=True))
add(Skill("maximize_window", "развернуть выбранное окно", run=actions.maximize_window,
          category="windows", reversible=True))
add(Skill("hotkey", "нажать сочетание клавиш в выбранном окне, например ctrl+c",
          {"keys": _spec("названия клавиш через +", required=True)},
          run=actions.hotkey, category="text", risk=1))
add(Skill("scroll", "прокрутить выбранное окно вверх или вниз",
          {"direction": _spec("up или down", choices=("up", "down")),
           "amount": _spec("число шагов 1-20", "int", 1, 20)},
          run=actions.scroll, category="windows", reversible=True))
add(Skill("click_mouse", "щёлкнуть в текущем положении курсора; координаты не выбирает",
          {"button": _spec("left или right", choices=("left", "right")),
           "count": _spec("один или два щелчка", "int", 1, 2)},
          run=actions.click_mouse, category="windows", risk=2,
          danger="щёлкнуть в текущем положении курсора"))

# Internet/files -------------------------------------------------------------
add(Skill("open_url", "открыть сайт",
          {"url": _spec("адрес сайта", required=True)}, category="web",
          run=lambda url="": actions.open_url(url)))
add(Skill("web_search", "поискать в интернете",
          {"query": _spec("что искать", required=True),
           "engine": _spec("google, yandex или youtube", choices=("google", "yandex", "youtube"))}, category="web",
          run=lambda query="", engine="google": actions.web_search(query, engine)))
add(Skill("open_folder", "открыть стандартную папку Windows",
          {"name": _spec("загрузки, документы, рабочий стол, изображения, видео, музыка", required=True)},
          run=actions.open_folder, category="files"))


def _find(query):
    hits = actions.find_files(query)
    if not hits:
        return "Ничего не нашёл."
    return "Нашёл %d. Первый: %s." % (len(hits), hits[0].name)


def _open_file(query):
    hits = actions.find_files(query, limit=1)
    if not hits:
        raise actions.ActionError("Не нашёл файл с таким названием.")
    return actions.open_path(hits[0])


add(Skill("find_files", "найти файлы по имени",
          {"query": _spec("часть имени", required=True)}, category="files",
          run=lambda query="": _find(query)))
add(Skill("open_file", "открыть файл по имени",
          {"query": _spec("часть имени", required=True)}, category="files",
          run=lambda query="": _open_file(query)))

# System --------------------------------------------------------------------
add(Skill("time", "сказать текущее время", run=actions.get_time))
add(Skill("date", "сказать сегодняшнюю дату", run=actions.get_date))
add(Skill("status", "загрузка процессора, памяти и батареи", run=actions.system_status))
add(Skill("disks", "свободное место на дисках", run=actions.disk_status))
add(Skill("screenshot", "сделать снимок экрана", run=actions.screenshot,
          category="screen", risk=1))
add(Skill("lock", "заблокировать экран", run=actions.lock_screen, risk=1))
add(Skill("clipboard", "прочитать буфер обмена",
          run=lambda: actions.clipboard_get()[:180] or "Буфер пуст.",
          category="text", risk=1))
add(Skill("type_text", "напечатать текст в активном окне",
          {"text": _spec("что напечатать", required=True)}, category="text", risk=1,
          run=lambda text="": actions.type_text(text)))
add(Skill("sleep_pc", "перевести компьютер в спящий режим",
          danger="уложить компьютер спать", risk=3, run=actions.sleep_pc))
add(Skill("shutdown", "выключить компьютер", danger="выключить компьютер", risk=3,
          run=lambda: actions.power("shutdown")))
add(Skill("restart", "перезагрузить компьютер", danger="перезагрузить компьютер", risk=3,
          run=lambda: actions.power("restart")))
add(Skill("cancel_power", "отменить выключение", run=lambda: actions.power("cancel"),
          risk=1))

# Notes and reminders --------------------------------------------------------
def _add_note(text):
    note_id = memory.add_note(text)
    return "Записал заметку." if note_id else "Заметка пустая."


def _list_notes():
    notes = memory.list_notes(5)
    if not notes:
        return "Заметок нет."
    return "Последние заметки: " + "; ".join(item["text"][:80] for item in notes) + "."


def _add_reminder(text, delay_seconds=0, due_at=0):
    due = float(due_at or 0) or time.time() + max(1, int(delay_seconds or 0))
    if due <= time.time():
        return "Время напоминания уже прошло."
    memory.add_reminder(text or "Напоминание", due)
    minutes = max(1, round((due - time.time()) / 60))
    return "Напомню примерно через %d минут." % minutes


def _list_reminders():
    reminders = memory.pending_reminders(5)
    if not reminders:
        return "Активных напоминаний нет."
    return "Ближайшее: %s." % reminders[0]["text"]


add(Skill("add_note", "сохранить локальную заметку",
          {"text": _spec("текст заметки", required=True)}, category="productivity",
          run=_add_note))
add(Skill("list_notes", "прочитать последние заметки", category="productivity",
          run=_list_notes, risk=1))
add(Skill("add_reminder", "создать локальное напоминание",
          {"text": _spec("что напомнить", required=True),
           "delay_seconds": _spec("через сколько секунд", "int", 0, 31536000),
           "due_at": _spec("Unix timestamp", "float", 0, None)},
          category="productivity", run=_add_reminder))
add(Skill("list_reminders", "перечислить активные напоминания",
          category="productivity", run=_list_reminders))

# Routines ------------------------------------------------------------------
def _run_routine(name):
    routine = memory.find_routine(name)
    if routine is None:
        return ActionResult(False, "Такой рутины нет.")
    steps = routine.get("steps", [])
    if not steps:
        return ActionResult(False, "В рутине нет шагов.")
    if len(steps) > 20:
        return ActionResult(False, "Рутина слишком длинная: максимум 20 шагов.")
    # Preflight every step before allowing any side effect.
    prepared = []
    for index, step in enumerate(steps):
        skill_name = step.get("skill", "") if isinstance(step, dict) else ""
        if not skill_name or skill_name == "run_routine" or not exists(skill_name):
            return ActionResult(False, "В рутине неизвестный шаг %d." % (index + 1))
        if is_dangerous(skill_name):
            return ActionResult(False, "Шаг «%s» требует отдельного подтверждения." % skill_name)
        if not memory.permission(skill_name)["enabled"]:
            return ActionResult(False, "Шаг «%s» отключён в настройках." % skill_name)
        clean = validate_params(skill_name, step.get("params"))
        prepared.append((skill_name, clean))
    messages, failures = [], []
    for index, (skill_name, clean) in enumerate(prepared):
        result = execute(skill_name, clean)
        messages.append(result.message)
        if not result.ok:
            failures.append(index + 1)
            if routine.get("stop_on_error", True):
                return ActionResult(False, "Рутина остановлена на шаге %d: %s" % (index + 1, result.message))
    if failures:
        return ActionResult(False, "Рутина выполнена с ошибками на шагах %s. %s" % (
            ", ".join(map(str, failures)), messages[-1]))
    return ActionResult(True, "Рутина выполнена. " + messages[-1])


add(Skill("run_routine", "запустить сохранённую рутину",
          {"name": _spec("название или голосовой триггер", required=True)},
          category="routines", run=_run_routine))


def catalog_text():
    lines = []
    for skill in SKILLS.values():
        args = []
        for key, spec in skill.params.items():
            args.append("%s (%s)" % (key, spec.get("description", "значение")))
        suffix = ". Параметры: " + ", ".join(args) if args else ""
        lines.append("%s — %s%s" % (skill.name, skill.about, suffix))
    return "\n".join(lines)


def exists(name):
    return name in SKILLS


def metadata(name=None):
    items = [SKILLS[name]] if name in SKILLS else list(SKILLS.values()) if name is None else []
    return [{"name": s.name, "about": s.about, "params": s.params,
             "category": s.category, "risk": s.risk, "danger": s.danger,
             "reversible": s.reversible, **memory.permission(s.name)} for s in items]


def is_dangerous(name):
    skill = SKILLS.get(name)
    if not skill:
        return False
    permission = memory.permission(name)
    return bool(skill.danger or skill.risk >= 2 or permission["always_confirm"])


def danger_text(name):
    skill = SKILLS.get(name)
    return skill.danger if skill and skill.danger else skill.about if skill else ""


def risk(name):
    skill = SKILLS.get(name)
    return skill.risk if skill else 3


def validate_params(name, params=None):
    skill = SKILLS.get(name)
    if skill is None:
        raise ValueError("Неизвестное умение")
    source = {} if params is None else params
    if not isinstance(source, dict):
        raise ValueError("Параметры должны быть объектом")
    unknown = set(source) - set(skill.params)
    if unknown:
        raise ValueError("Неизвестный параметр %s" % ", ".join(sorted(map(str, unknown))))
    output = {}
    for key, spec in skill.params.items():
        if key not in source:
            if spec.get("required"):
                raise ValueError("Не хватает параметра %s" % key)
            continue
        value = source[key]
        if value is None:
            raise ValueError("Пустой параметр %s" % key)
        kind = spec.get("type", "str")
        try:
            if kind in ("int", "float"):
                if isinstance(value, bool):
                    raise ValueError("boolean is not a number")
                number = float(value)
                if not math.isfinite(number) or (kind == "int" and not number.is_integer()):
                    raise ValueError("not a finite number of the requested type")
                value = int(number) if kind == "int" else number
            elif kind == "bool":
                if not isinstance(value, bool):
                    token = str(value).strip().lower()
                    if token not in ("1", "true", "yes", "on", "да", "0", "false", "no", "off", "нет"):
                        raise ValueError("not a boolean")
                    value = token in ("1", "true", "yes", "on", "да")
            else:
                if not isinstance(value, str):
                    raise ValueError("not text")
                # Preserve dictation and pasted text, including punctuation and whitespace.
                value = value if key == "text" else value.strip()
                if len(value) > 20000:
                    raise ValueError("text too long")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Неверный параметр %s" % key) from exc
        if spec.get("min") is not None and value < spec["min"]:
            value = spec["min"]
        if spec.get("max") is not None and value > spec["max"]:
            value = spec["max"]
        if spec.get("choices") and value not in spec["choices"]:
            raise ValueError("Неверное значение %s: допустимо %s" % (key, ", ".join(spec["choices"])))
        if spec.get("required") and isinstance(value, str) and not value.strip():
            raise ValueError("Пустой параметр %s" % key)
        output[key] = value
    return output


def _execute_unlocked(name, params=None):
    """Execute one validated action once; never re-run after an internal exception."""
    skill = SKILLS.get(name)
    if skill is None:
        return ActionResult(False, "Я такого не умею.")
    if not memory.permission(name)["enabled"]:
        return ActionResult(False, "Это умение отключено в настройках.")
    try:
        clean = validate_params(name, params)
    except ValueError as exc:
        return ActionResult(False, "Не выполнил: %s." % exc)
    try:
        result = skill.run(**clean)
        if isinstance(result, ActionResult):
            return result
        if not isinstance(result, str) or not result.strip():
            return ActionResult(False, "Не удалось подтвердить выполнение действия.")
        return ActionResult(True, result)
    except actions.ActionError as exc:
        return ActionResult(False, str(exc))
    except Exception as exc:
        logging.getLogger(__name__).warning("Action %s failed (%s)", name, type(exc).__name__)
        if isinstance(exc, PermissionError):
            message = "Windows отказала в доступе к этому действию."
        elif isinstance(exc, FileNotFoundError):
            message = "Не найден нужный файл или компонент Windows."
        elif isinstance(exc, TimeoutError):
            message = "Windows не ответила вовремя. Попробуйте ещё раз."
        else:
            message = "Не удалось выполнить действие «%s» (%s)." % (skill.about, type(exc).__name__)
        return ActionResult(False, message)


def uses_desktop(name):
    """Resource ownership is independent from the action's confirmation risk."""
    skill = SKILLS.get(name)
    return bool(skill and (skill.category in ("windows", "web", "text", "screen")
                           or name in ("open_file", "open_folder", "lock", "sleep_pc",
                                       "shutdown", "restart", "run_routine")))


def execute(name, params=None):
    """Keep one foreground owner while leaving notes, conversation and audio usable."""
    from core import desktop_lock
    if not uses_desktop(name):
        return _execute_unlocked(name, params)
    with desktop_lock.acquire(blocking=False) as acquired:
        if not acquired:
            return ActionResult(False, "Компьютером сейчас управляет другая задача. Приостановите её или дождитесь завершения.")
        return _execute_unlocked(name, params)


def run(name, params=None):
    """Compatibility interface for callers which need only spoken text."""
    return execute(name, params).message
