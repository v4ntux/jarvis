"""Plain-words commands that must never depend on a model's mood: stop, call me now, call me later.

«Позвони мне через две минуты» used to go through a model that picked a watcher with a guessed clock
time and never rang. Here the words are parsed by rule, the time is computed from the real clock.
"""
import datetime as dt
import re
from dataclasses import dataclass


@dataclass
class Intent:
    kind: str  # "stop" | "call"
    at: float | None = None  # call: when to ring (None = now)


NUMBERS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6,
    "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
    "odin": 1, "dva": 2, "dve": 2, "tri": 3, "chetire": 4, "pyat": 5, "shest": 6, "sem": 7, "vosem": 8,
    "devyat": 9, "desyat": 10, "pyatnadtsat": 15, "dvadtsat": 20, "tridtsat": 30, "sorok": 40, "pyatdesyat": 50,
}
WORD = "|".join(sorted(NUMBERS, key=len, reverse=True))
AMOUNT = rf"(?:\d+(?:[.,]\d+)?|{WORD})(?:\s+(?:\d+|{WORD}))?"
UNIT = r"(?:секунд\w*|сек\w*|минут\w*|мин\w*|час\w*|sekund\w*|sek\w*|minut\w*|min\w*|chas\w*)"
RELATIVE = re.compile(rf"\b(?:через|cherez)\s+(?P<amount>{AMOUNT})\s+(?P<unit>{UNIT})\b")
CLOCK = re.compile(r"\b(?:в|v)\s+(?P<h>\d{1,2})(?:[:.\s](?P<m>\d{2}))?(?:\s*(?P<part>утра|вечера|дня|ночи))?"
                   r"(?=\s|$)")
SPECIAL = (  # words that stand for a number and a unit
    (r"\bчерез\s+(?:пол\s?часа|полчаса)\b", "через 30 минут"),
    (r"\bчерез\s+полтора\s+часа\b", "через 90 минут"),
    (r"\bчерез\s+пару\s+минут\b", "через 2 минуты"),
    (r"\bчерез\s+(?:несколько|пять-десять)\s+минут\b", "через 5 минут"),
    (r"\bчерез\s+(?:минуту|минутку|минуточку)\b", "через 1 минуту"),
    (r"\bчерез\s+секунду\b", "через 1 секунду"),
    (r"\bчерез\s+час\b", "через 1 час"),
)
CALL = re.compile(r"^(?:(?:джарвис|jarvis|эй|слушай)\s+)*(?:(?:пожалуйста|плиз|plz|pls)\s+)?"
                  r"(?:позвони|позвоните|позвонить|прозвони|набери|наберите|звони|pozvoni|pozvonite|nabery|zvoni)\b"
                  r"(?P<rest>.*)$")
STOP = re.compile(r"^(?:(?:джарвис|jarvis)\s+)?(?:стоп|stop|хватит|отмена|отбой|стой|остановись|ostanovis|stoy)$")
# What may stay around the call request; anything else means it is about something else («позвони Сардору»)
FILLER = {"мне", "меня", "пожалуйста", "плиз", "плз", "pls", "plz", "mne", "menya", "jarvis", "джарвис", "телеграм",
          "телеграме", "телеге", "тг", "tg", "telegram", "в", "v", "быстро", "срочно", "ка", "пож", "ну", "давай",
          "ещё", "еще", "сюда", "голосом", "по", "звонку"}
UNIT_SECONDS = (("сек", 1), ("sek", 1), ("мин", 60), ("min", 60), ("час", 3600), ("chas", 3600))


def _norm(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[^\w:.]+", " ", text)
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", text)  # dots that are not decimal points
    return re.sub(r"\s+", " ", text).strip()


def _amount(text: str) -> float:
    total = 0.0
    for token in text.split():
        total += float(token.replace(",", ".")) if token[0].isdigit() else NUMBERS[token]
    return total


def _clock(match: re.Match, now: dt.datetime) -> float | None:
    hour, minute, part = int(match["h"]), int(match["m"] or 0), match["part"]
    if part in ("вечера", "дня") and hour < 12:
        hour += 12
    elif part in ("утра", "ночи") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    moment = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if moment <= now:
        moment += dt.timedelta(days=1)
    return moment.timestamp()


def _when(rest: str, now: dt.datetime) -> tuple[float | None, str]:
    """The moment named in the words (None: now) and the words without it."""
    for pattern, replacement in SPECIAL:
        rest = re.sub(pattern, replacement, rest)
    found = RELATIVE.search(rest)
    if found:
        seconds = next(k for prefix, k in UNIT_SECONDS if found["unit"].startswith(prefix))
        return now.timestamp() + _amount(found["amount"]) * seconds, rest[:found.start()] + " " + rest[found.end():]
    found = CLOCK.search(rest)
    if found:
        at = _clock(found, now)
        if at is not None:
            return at, rest[:found.start()] + " " + rest[found.end():]
    return None, rest


def parse(text: str, now: dt.datetime) -> Intent | None:
    """An intent for a message that is nothing but the command, or None to let the models handle it."""
    words = _norm(text)
    if STOP.match(words):
        return Intent("stop")
    call = CALL.match(words)
    if not call:
        return None
    at, rest = _when(call["rest"], now)
    if set(rest.split()) - FILLER:
        return None
    return Intent("call", at)
