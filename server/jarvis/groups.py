"""Group chats: Jarvis sits in them as a member with a character, not as a service.

He answers when he is called (by name, @username, or a reply to him), looks at what people show him
(«Джарвис, смотри»), and now and then reacts or comments on his own, sparingly. In a group he is only a
personality: no tools, no Claude, nothing about the owner, so whatever people write cannot make him do or
leak anything. Limits keep him from spamming: Telegram bans accounts that talk too much.
"""
import logging
import random
import re
import time
from dataclasses import dataclass

from . import style
from .config import Config, day_start
from .gemini import Gemini
from .memory import Memory

log = logging.getLogger("jarvis.groups")

NAME = re.compile(r"(?<!\w)(?:джарвис|жарвис|джарви|джарвик|jarvis|zharvis|dzharvis|jarvi)\w*", re.I)
LOOK = re.compile(r"(?<!\w)(?:смотри|смотрите|посмотри|глянь|гляньте|гляди|зацени|оцени|чекни|look|check|rate)(?!\w)",
                  re.I)
MUTE = re.compile(r"(?<!\w)(?:заткнись|помолчи|замолчи|молчи|умолкни|тихо|не пиши|мут|shut up)(?!\w)", re.I)
UNMUTE = re.compile(r"(?<!\w)(?:говори|можешь писать|можно писать|размуть\w*|врубай\w*|вернись)(?!\w)", re.I)

GAP_ADDRESSED = 1.5  # seconds between two things Jarvis does in one group when he was called
GAP_SPONTANEOUS = 60  # ... and when he acts on his own (the gap counts from his last look too)
SPONT_HOURLY = 10  # his own reactions and comments per group per hour
SPONT_DAILY = 150  # ... and in all groups per day
CONTEXT = 25  # messages of the talk that Gemini gets
LINE = 300
MAX_ANSWER = 600
RECENT_MEDIA_SECONDS = 300  # «смотри» after somebody's video, without a reply, means that video
MODES = {"on": "говорит", "quiet": "тихо, только когда зовут", "off": "выключен"}
MODE_WORDS = {"on": "on", "вкл": "on", "говори": "on", "включи": "on", "quiet": "quiet", "тихо": "quiet",
              "тише": "quiet", "off": "off", "выкл": "off", "молчать": "off", "выключи": "off"}

PERSONA = """Ты Джарвис, ИИ со своим Telegram-аккаунтом (не человек, и если спросят, ты этого не скрываешь). Тебя добавили
в групповой чат.

{style}"""
ADDRESSED = """Группа «{title}». Последние сообщения (внизу самые свежие):
{transcript}

К тебе обратился {who}: {text}
{seen}Ответь как Джарвис, коротко. Тебя позвали, так что совсем игнорировать нельзя. Если это ловушка или
провокация против тебя или Вана, ответь одной реакцией без текста; но хозяину всегда отвечай словами.{urge}"""
GLANCE = """Группа «{title}». Последние сообщения (внизу самые свежие):
{transcript}

Только что {who} написал: {text}
{seen}Реши как живой участник: поставить реакцию, коротко сыронизировать (только если реально смешно или в тему)
или промолчать. Ответ: «РЕАКЦИЯ: эмодзи», можно и фраза, или ровно МОЛЧУ.{urge}"""


@dataclass
class Verdict:
    answer: str = ""
    reaction: str = ""
    sticker: str = ""
    gif: str = ""
    love: str = ""

    def __bool__(self) -> bool:
        return bool(self.answer or self.reaction or self.sticker or self.gif)


class Groups:
    def __init__(self, cfg: Config, memory: Memory, gemini: Gemini | None, rng: random.Random | None = None):
        self.cfg, self.memory, self.gemini = cfg, memory, gemini
        self.rng = rng or random.Random()
        self.me_id = 0  # Jarvis's own Telegram id, known once the account is up
        self.last: dict[int, float] = {}  # group → when Jarvis last said or reacted
        self.glanced: dict[int, float] = {}  # group → when he last looked at something on his own
        self.muted_until: dict[int, float] = {}  # group → Telegram told him to be quiet

    # Who is talking to whom

    @staticmethod
    def addressed(text: str, mentioned: bool, replied_to_me: bool) -> bool:
        """By name or @username, a Telegram mention, or a reply to one of his messages."""
        return bool(mentioned or replied_to_me or NAME.search(text or ""))

    @staticmethod
    def wants_a_look(text: str) -> bool:
        return bool(LOOK.search(text or ""))

    @staticmethod
    def owner_switch(text: str) -> str | None:
        """The owner tells him to shut up («Джарвис, тихо») or to talk again: the new mode, or None."""
        if MUTE.search(text or ""):
            return "quiet"
        if UNMUTE.search(text or ""):
            return "on"
        return None

    # How much he may do

    def may_speak(self, chat: int, spontaneous: bool) -> bool:
        now = time.time()
        if self.muted_until.get(chat, 0) > now:
            return False
        since = max(self.last.get(chat, 0), self.glanced.get(chat, 0) if spontaneous else 0)
        if now - since < (GAP_SPONTANEOUS if spontaneous else GAP_ADDRESSED):
            return False
        if (self.memory.group_count(chat, now - 3600) >= self.cfg.group_hourly
                or self.memory.group_count(None, day_start()) >= self.cfg.group_daily):
            return False
        if spontaneous and (self.memory.group_count(chat, now - 3600, ("spont",)) >= SPONT_HOURLY
                            or self.memory.group_count(None, day_start(), ("spont",)) >= SPONT_DAILY):
            return False
        return True

    def spontaneous_wanted(self, has_media: bool, text: str) -> bool:
        """Pictures and videos catch his eye more often than text; short chatter, commands and links never."""
        if not has_media and (len(text) < 25 or text.startswith("/") or "http" in text):
            return False
        return self.rng.random() < (self.cfg.group_react_chance if has_media else self.cfg.group_text_chance)

    def mute_for(self, chat: int, seconds: float) -> None:
        self.muted_until[chat] = time.time() + seconds

    def record(self, chat: int, msg_id: int | None, text: str, spontaneous: bool) -> None:
        """Writes down what Jarvis did (a reaction has no message: it gets a made-up negative id)."""
        self.memory.group_add(chat, msg_id or -int(time.time() * 1000), self.me_id, "Джарвис", text,
                              kind="spont" if spontaneous else "reply")
        self.last[chat] = time.time()

    # What he says

    def transcript(self, chat: int) -> str:
        lines = []
        for sender, name, text in self.memory.group_recent(chat, CONTEXT):
            owner = " (хозяин)" if sender == self.cfg.owner_id else ""
            lines.append(f"{name}{owner}: {text[:LINE]}")
        return "\n".join(lines)

    def persona(self) -> str:
        return PERSONA.format(style=style.GROUP_STYLE) + "\n\n" + style.block(self.memory)

    async def answer(self, chat: int, title: str, who: str, text: str, seen: str, parts: list[dict]) -> Verdict:
        """He was called: always an answer if Gemini has one."""
        prompt = ADDRESSED.format(title=title, transcript=self.transcript(chat), who=who, text=text, urge=self._urge(),
                                  seen=f"Вложение, которое ты видишь: {seen}.\n" if seen else "")
        verdict = await self._ask(prompt, parts, "medium")
        return verdict or Verdict(reaction="👀")  # called, so never ignored: at least a look

    async def glance(self, chat: int, title: str, who: str, text: str, seen: str, parts: list[dict]) -> Verdict:
        """Nobody called him: a reaction, a short quip, or nothing."""
        self.glanced[chat] = time.time()
        prompt = GLANCE.format(title=title, transcript=self.transcript(chat), who=who, text=text, urge=self._urge(),
                               seen=f"Вложение, которое ты видишь: {seen}.\n" if seen else "")
        return await self._ask(prompt, parts, "low")

    def _urge(self) -> str:
        urge = style.impulse(self.rng, self.cfg.extra_chance)
        return f"\n{urge}" if urge else ""

    async def _ask(self, prompt: str, parts: list[dict], effort: str) -> Verdict:
        """effort: how hard the model thinks first; called by name it is «medium», a traps and bait check."""
        raw = await self.gemini.text(self.persona(), [{"role": "user", "parts": [{"text": prompt}] + parts}], 300,
                                     models=self.gemini.lite_first, effort=effort)
        return self.verdict(raw)

    @staticmethod
    def verdict(raw: str) -> Verdict:
        marks = style.split_marks(raw)
        text = re.sub(r"^(?:джарвис|jarvis)\s*[:—-]\s*", "", marks.text.strip(), flags=re.I)
        if text.strip().upper().startswith(style.SILENT):
            text = ""
        return Verdict(text[:MAX_ANSWER].strip(), marks.reaction, marks.sticker, marks.gif, marks.love)
