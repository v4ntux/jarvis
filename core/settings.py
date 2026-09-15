"""Configuration schema, validation and protected secret migration."""
import os
import math
import sys
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

from core import credentials


def _root():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


ROOT = _root()
DATA_DIR = credentials.DATA_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)
LEGACY_ENV_PATH = ROOT / ".env"
ENV_PATH = DATA_DIR / "settings.env"
FIRST_RUN_MARK = DATA_DIR / "onboarding.complete"
load_dotenv(LEGACY_ENV_PATH)
load_dotenv(ENV_PATH, override=True)

SECRET_MASK = "••••••••••"


class Spec:
    def __init__(self, key, label, group, kind="text", default="", choices=(),
                 hint="", minimum=None, maximum=None):
        self.key = key
        self.label = label
        self.group = group
        self.kind = kind
        self.default = default
        self.choices = choices
        self.hint = hint
        self.minimum = minimum
        self.maximum = maximum


GROUPS = (
    ("ai", "AI POOL"),
    ("activation", "АКТИВАЦИЯ"),
    ("speech", "ГОЛОС И РЕЧЬ"),
    ("behaviour", "ПОВЕДЕНИЕ"),
    ("privacy", "ПРИВАТНОСТЬ"),
    ("look", "ИНТЕРФЕЙС"),
)

SPECS = (
    Spec("JARVIS_AI", "AI Pool", "ai", "choice", "auto",
         ("off", "auto", "groq", "cerebras", "gemini", "openrouter", "claude", "ollama"),
         "auto выбирает здорового провайдера; off оставляет только локальные команды"),
    Spec("JARVIS_AI_ORDER", "Порядок fallback", "ai", "text",
         "groq,cerebras,gemini,openrouter",
         hint="Провайдеры через запятую; cooldown и доступность учитываются автоматически"),
    Spec("JARVIS_GROQ_KEY", "Ключ Groq", "ai", "secret", "",
         hint="Хранится через Windows DPAPI, не в .env"),
    Spec("JARVIS_CEREBRAS_KEY", "Ключ Cerebras", "ai", "secret", "",
         hint="Необязательный бесплатный fallback"),
    Spec("JARVIS_GEMINI_KEY", "Ключ Gemini", "ai", "secret", "",
         hint="Free Tier может использовать запросы для улучшения моделей Google"),
    Spec("JARVIS_OPENROUTER_KEY", "Ключ OpenRouter", "ai", "secret", "",
         hint="openrouter/free — резерв с небольшой дневной квотой"),
    Spec("JARVIS_CLAUDE_KEY", "Ключ Claude", "ai", "secret", "",
         hint="Не используется в бесплатной цепочке по умолчанию"),
    Spec("JARVIS_GROQ_MODEL", "Модель Groq", "ai", "text", "openai/gpt-oss-20b"),
    Spec("JARVIS_CEREBRAS_MODEL", "Модель Cerebras", "ai", "text", "llama3.1-8b"),
    Spec("JARVIS_GEMINI_MODEL", "Модель Gemini", "ai", "text", "gemini-3.6-flash"),
    Spec("JARVIS_OPENROUTER_MODEL", "Модель OpenRouter", "ai", "text", "openrouter/free"),
    Spec("JARVIS_AI_MODEL", "Общая модель (override)", "ai", "text", "",
         hint="Оставьте пустым, чтобы использовать модель конкретного провайдера"),

    Spec("JARVIS_NAME", "Имя", "activation", "text", "Джарвис"),
    Spec("JARVIS_WAKE_ENGINE", "Wake engine", "activation", "choice", "auto",
         ("auto", "openwakeword", "whisper"),
         "Русское имя — локальный Whisper; openwakeword — английское hey jarvis"),
    Spec("JARVIS_WAKE_THRESHOLD", "Порог wake-word", "activation", "float", "0.45",
         minimum=0.1, maximum=0.95, hint="Выше — меньше ложных срабатываний"),
    Spec("JARVIS_WAKE_PATIENCE", "Подтверждающих кадров", "activation", "int", "2",
         minimum=1, maximum=6),
    Spec("JARVIS_WAKE_FUZZY", "Похожесть имени", "activation", "float", "0.70",
         minimum=0.5, maximum=0.95),
    Spec("JARVIS_MODEL_WAKE", "Whisper-верификатор", "activation", "choice", "tiny",
         ("tiny", "base")),
    Spec("JARVIS_MIC", "Микрофон", "activation", "text", "",
         hint="Номер устройства; пусто — системный"),
    Spec("JARVIS_MIC_SENSITIVITY", "Чувствительность речи", "activation", "float", "3.0",
         minimum=1.0, maximum=10.0),
    Spec("JARVIS_SILENCE_TAIL", "Пауза завершает фразу", "activation", "float", "0.85",
         minimum=0.35, maximum=3.0),

    Spec("JARVIS_SPEECH_MODE", "Распознавание", "speech", "choice", "auto",
         ("auto", "cloud", "private"),
         "auto: облако после wake и локальный fallback; private: только локально"),
    Spec("JARVIS_CLOUD_AUDIO", "Разрешить cloud STT после wake", "speech", "bool", "true"),
    Spec("JARVIS_MODEL_MAIN", "Локальная STT-модель", "speech", "choice", "small",
         ("tiny", "base", "small", "medium"), "Загружается только при необходимости"),
    Spec("JARVIS_LIVE_TEXT", "Живой текст", "speech", "bool", "true"),
    Spec("JARVIS_TTS", "Озвучивать ответы", "speech", "bool", "true"),
    Spec("JARVIS_VOICE", "Голос", "speech", "choice", "ru-RU-DmitryNeural",
         ("ru-RU-DmitryNeural", "ru-RU-SvetlanaNeural",
          "en-AU-WilliamMultilingualNeural", "en-US-BrianMultilingualNeural",
          "en-US-AndrewMultilingualNeural", "en-US-AvaMultilingualNeural")),
    Spec("JARVIS_VOICE_RATE", "Скорость голоса", "speech", "choice", "+10%",
         ("-10%", "+0%", "+10%", "+20%")),
    Spec("JARVIS_VOICE_PITCH", "Тембр", "speech", "choice", "+0Hz",
         ("-20Hz", "-10Hz", "+0Hz", "+10Hz")),

    Spec("JARVIS_TITLE", "Обращение", "behaviour", "text", "сэр"),
    Spec("JARVIS_FOLLOW_UP", "Обычное окно, сек", "behaviour", "int", "15",
         minimum=0, maximum=120),
    Spec("JARVIS_FOLLOW_ACTION", "После действия, сек", "behaviour", "int", "8",
         minimum=0, maximum=120),
    Spec("JARVIS_FOLLOW_QUESTION", "После вопроса, сек", "behaviour", "int", "25",
         minimum=5, maximum=180),
    Spec("JARVIS_UNLOAD_AFTER", "Выгрузка STT, сек", "behaviour", "int", "180",
         minimum=0, maximum=3600),
    Spec("JARVIS_CONFIRM", "Подтверждать рискованные действия", "behaviour", "bool", "true"),
    Spec("JARVIS_LEARN", "Запоминать команды", "behaviour", "bool", "true"),

    Spec("JARVIS_REMEMBER_CHAT", "Хранить сводки разговоров", "privacy", "bool", "false"),
    Spec("JARVIS_ACTION_LOG_DAYS", "Хранить журнал, дней", "privacy", "int", "14",
         minimum=0, maximum=365),
    Spec("JARVIS_DIAGNOSTIC_TEXT", "Логировать распознанный текст", "privacy", "bool", "false"),

    Spec("JARVIS_UI", "Показывать island", "look", "bool", "true"),
    Spec("JARVIS_UI_POSITION", "Положение", "look", "choice", "top",
         ("top", "top-right", "bottom-right")),
    Spec("JARVIS_UI_THEME", "Тема", "look", "choice", "dark",
         ("dark", "light", "system")),
    Spec("JARVIS_UI_ACCENT", "Акцент", "look", "choice", "cyan",
         ("cyan", "white", "blue", "green", "amber")),
    Spec("JARVIS_REDUCED_MOTION", "Уменьшить движение", "look", "bool", "false"),
)

BY_KEY = {spec.key: spec for spec in SPECS}
SECRET_KEYS = {spec.key for spec in SPECS if spec.kind == "secret"}

# Carry existing non-secret choices into the profile once, so source and EXE
# use the same settings regardless of their installation directory.
if not ENV_PATH.exists() and LEGACY_ENV_PATH.exists():
    _legacy = dotenv_values(LEGACY_ENV_PATH)
    _portable = {key: value for key, value in _legacy.items()
                 if key in BY_KEY and key not in SECRET_KEYS and value is not None}
    if _portable:
        from dotenv import set_key
        for _key, _value in _portable.items():
            set_key(str(ENV_PATH), _key, _value, quote_mode="always")


def _secret(key):
    stored = credentials.get(key)
    if stored:
        return stored
    legacy = (os.getenv(key, "") or "").strip()
    if not legacy and key == "JARVIS_CLAUDE_KEY":
        legacy = (os.getenv("ANTHROPIC_API_KEY", "") or "").strip()
    if legacy:
        try:
            credentials.set(key, legacy)
        except RuntimeError:
            pass
    return legacy


def _raw(key):
    if key in SECRET_KEYS:
        return _secret(key)
    spec = BY_KEY.get(key)
    default = spec.default if spec else ""
    value = os.getenv(key)
    return default if value is None or value == "" else value.strip()


def text(key):
    return _raw(key)


def flag(key):
    return _raw(key).lower() in ("1", "true", "yes", "on", "да")


def integer(key):
    try:
        return int(float(_raw(key)))
    except (TypeError, ValueError):
        return int(float(BY_KEY[key].default))


def number(key):
    try:
        return float(_raw(key).replace(",", "."))
    except (AttributeError, TypeError, ValueError):
        return float(BY_KEY[key].default)


def _bind_runtime_values():
    global AI_MODE, AI_ORDER, AI_MODEL, CLAUDE_KEY, GEMINI_KEY, GROQ_KEY
    global CEREBRAS_KEY, OPENROUTER_KEY, LEARN, TTS_ENABLED, VOICE, VOICE_RATE
    global VOICE_PITCH, MODEL_MAIN, MODEL_WAKE, MIC, MIC_SENSITIVITY
    global SILENCE_TAIL, LIVE_TEXT, NAME, TITLE, FOLLOW_UP, FOLLOW_ACTION
    global FOLLOW_QUESTION, WAKE_FUZZY, WAKE_ENGINE, WAKE_THRESHOLD, WAKE_PATIENCE
    global UNLOAD_AFTER, CONFIRM, UI_ENABLED, UI_POSITION, UI_ACCENT, UI_THEME
    global REDUCED_MOTION, SPEECH_MODE, CLOUD_AUDIO, REMEMBER_CHAT
    global ACTION_LOG_DAYS, DIAGNOSTIC_TEXT, PROVIDER_MODELS

    AI_MODE = text("JARVIS_AI").lower()
    AI_ORDER = text("JARVIS_AI_ORDER")
    AI_MODEL = text("JARVIS_AI_MODEL")
    CLAUDE_KEY = text("JARVIS_CLAUDE_KEY")
    GEMINI_KEY = text("JARVIS_GEMINI_KEY")
    GROQ_KEY = text("JARVIS_GROQ_KEY")
    CEREBRAS_KEY = text("JARVIS_CEREBRAS_KEY")
    OPENROUTER_KEY = text("JARVIS_OPENROUTER_KEY")
    PROVIDER_MODELS = {
        "groq": text("JARVIS_GROQ_MODEL"),
        "cerebras": text("JARVIS_CEREBRAS_MODEL"),
        "gemini": text("JARVIS_GEMINI_MODEL"),
        "openrouter": text("JARVIS_OPENROUTER_MODEL"),
    }
    LEARN = flag("JARVIS_LEARN")
    TTS_ENABLED = flag("JARVIS_TTS")
    VOICE = text("JARVIS_VOICE")
    VOICE_RATE = text("JARVIS_VOICE_RATE")
    VOICE_PITCH = text("JARVIS_VOICE_PITCH")
    MODEL_MAIN = text("JARVIS_MODEL_MAIN")
    MODEL_WAKE = text("JARVIS_MODEL_WAKE")
    MIC = text("JARVIS_MIC") or None
    MIC_SENSITIVITY = number("JARVIS_MIC_SENSITIVITY")
    SILENCE_TAIL = number("JARVIS_SILENCE_TAIL")
    LIVE_TEXT = flag("JARVIS_LIVE_TEXT")
    NAME = text("JARVIS_NAME")
    TITLE = text("JARVIS_TITLE")
    FOLLOW_UP = integer("JARVIS_FOLLOW_UP")
    FOLLOW_ACTION = integer("JARVIS_FOLLOW_ACTION")
    FOLLOW_QUESTION = integer("JARVIS_FOLLOW_QUESTION")
    WAKE_FUZZY = number("JARVIS_WAKE_FUZZY")
    WAKE_ENGINE = text("JARVIS_WAKE_ENGINE")
    WAKE_THRESHOLD = number("JARVIS_WAKE_THRESHOLD")
    WAKE_PATIENCE = integer("JARVIS_WAKE_PATIENCE")
    UNLOAD_AFTER = integer("JARVIS_UNLOAD_AFTER")
    CONFIRM = flag("JARVIS_CONFIRM")
    SPEECH_MODE = text("JARVIS_SPEECH_MODE")
    CLOUD_AUDIO = flag("JARVIS_CLOUD_AUDIO")
    REMEMBER_CHAT = flag("JARVIS_REMEMBER_CHAT")
    ACTION_LOG_DAYS = integer("JARVIS_ACTION_LOG_DAYS")
    DIAGNOSTIC_TEXT = flag("JARVIS_DIAGNOSTIC_TEXT")
    UI_ENABLED = flag("JARVIS_UI")
    UI_POSITION = text("JARVIS_UI_POSITION")
    UI_ACCENT = text("JARVIS_UI_ACCENT")
    UI_THEME = text("JARVIS_UI_THEME")
    REDUCED_MOTION = flag("JARVIS_REDUCED_MOTION")


_bind_runtime_values()

SAMPLE_RATE = 16000
MAX_PHRASE = 30.0
SCREENSHOT_DIR = Path.home() / "Pictures" / "Jarvis"
LOG_PATH = DATA_DIR / "jarvis.log"
DB_PATH = DATA_DIR / "jarvis.db"


def current_values():
    values = {}
    for spec in SPECS:
        values[spec.key] = SECRET_MASK if spec.kind == "secret" and _secret(spec.key) else _raw(spec.key)
    return values


def validate(values):
    clean, errors = {}, {}
    for key, value in values.items():
        spec = BY_KEY.get(key)
        if spec is None:
            continue
        value = ("" if value is None else str(value)).strip()
        if "\n" in value or "\r" in value:
            errors[key] = "значение должно быть в одной строке"
            continue
        if spec.kind == "secret":
            clean[key] = value
        elif spec.kind == "bool":
            clean[key] = "true" if value.lower() in ("1", "true", "yes", "on", "да") else "false"
        elif spec.kind in ("int", "float"):
            try:
                number_value = float(value.replace(",", "."))
            except ValueError:
                errors[key] = "нужно число"
                continue
            if not math.isfinite(number_value):
                errors[key] = "нужно конечное число"
                continue
            if spec.kind == "int" and not number_value.is_integer():
                errors[key] = "нужно целое число"
                continue
            if spec.minimum is not None and number_value < spec.minimum:
                errors[key] = "минимум %g" % spec.minimum
                continue
            if spec.maximum is not None and number_value > spec.maximum:
                errors[key] = "максимум %g" % spec.maximum
                continue
            clean[key] = str(int(number_value)) if spec.kind == "int" else "%g" % number_value
        elif spec.kind == "choice" and spec.choices and value not in spec.choices:
            errors[key] = "выберите из списка"
        else:
            clean[key] = value
    return clean, errors


def save(values):
    clean, errors = validate(values)
    if errors:
        return errors
    for key in list(clean):
        if key not in SECRET_KEYS:
            continue
        value = clean.pop(key)
        if value == SECRET_MASK:
            continue
        try:
            credentials.set(key, value)
        except RuntimeError as exc:
            errors[key] = str(exc)
    if errors:
        return errors

    existing = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    written, out = set(), []
    for line in existing:
        key = line.split("=", 1)[0].strip()
        if key in SECRET_KEYS or key == "ANTHROPIC_API_KEY":
            continue
        if key in clean:
            value = clean[key].replace("\\", "\\\\").replace("'", "\\'")
            out.append("%s='%s'" % (key, value))
            written.add(key)
        else:
            out.append(line)
    for key, value in clean.items():
        if key not in written:
            escaped = value.replace("\\", "\\\\").replace("'", "\\'")
            out.append("%s='%s'" % (key, escaped))
    staged = ENV_PATH.with_suffix(".tmp")
    staged.write_text("\n".join(out).strip() + "\n", encoding="utf-8")
    staged.replace(ENV_PATH)
    return {}


def write_example(path=None):
    path = Path(path or ROOT / ".env.example")
    lines = ["# Jarvis 1.0 Beta settings. API keys are stored by Windows DPAPI.", ""]
    for group, title in GROUPS:
        lines.append("# --- %s ---" % title.lower())
        for spec in SPECS:
            if spec.group != group:
                continue
            if spec.hint:
                lines.append("# %s" % spec.hint)
            if spec.choices:
                lines.append("# варианты: %s" % ", ".join(spec.choices))
            lines.append("%s=%s" % (spec.key, "" if spec.kind == "secret" else spec.default))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def reload():
    load_dotenv(ENV_PATH, override=True)
    _bind_runtime_values()
    return dotenv_values(ENV_PATH)
