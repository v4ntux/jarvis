"""Who writes the words for strangers and groups.

Claude Haiku first: it thinks before it answers (traps like «кто ответит, тот…» are its job to notice), it does
not run out of a free daily quota, and at $0.10 per million tokens a reply costs a few hundredths of a cent.
Gemini answers when Claude cannot (no money, an outage, today's chat budget spent) and whenever there is a
video, a GIF or sound to look at: Claude sees pictures only.

Same face as Gemini for the callers (text, ready, lite_first, GeminiError), so strangers and groups do not
care who answered.
"""
import base64
import logging

import anthropic

from .config import Config, day_start
from .gemini import Gemini, GeminiError
from .memory import Memory

log = logging.getLogger("jarvis.chat")

IMAGES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
THINKING_ROOM = 4000  # thinking counts against max_tokens: room for it on top of the answer itself
USAGE_PREFIX = "chat:"


def to_messages(contents: list[dict]) -> list[dict] | None:
    """Gemini's contents as Claude messages; None when something in them is not text or a picture."""
    messages = []
    for item in contents:
        blocks = []
        for part in item.get("parts", []):
            if part.get("text"):
                blocks.append({"type": "text", "text": part["text"]})
            elif "inlineData" in part:
                mime = part["inlineData"].get("mimeType", "")
                if mime not in IMAGES:
                    return None
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime,
                                                           "data": part["inlineData"]["data"]}})
            else:
                return None
        if blocks:
            role = "assistant" if item.get("role") == "model" else "user"
            if messages and messages[-1]["role"] == role:
                messages[-1]["content"] += blocks
            else:
                messages.append({"role": role, "content": blocks})
    while messages and messages[0]["role"] != "user":
        messages.pop(0)
    if not messages or messages[-1]["role"] != "user":
        return None
    return messages


def _valid_image(block: dict) -> bool:
    """A picture Claude accepts: the bytes must match what the part says it is."""
    try:
        data = base64.b64decode(block["source"]["data"][:32])
    except ValueError:
        return False
    kind = block["source"]["media_type"]
    return ((kind == "image/jpeg" and data[:3] == b"\xff\xd8\xff") or (kind == "image/png" and data[:4] == b"\x89PNG")
            or (kind == "image/gif" and data[:3] == b"GIF") or (kind == "image/webp" and data[8:12] == b"WEBP"))


class Chat:
    def __init__(self, cfg: Config, memory: Memory, gemini: Gemini):
        self.cfg, self.memory, self.gemini = cfg, memory, gemini
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_key) if cfg.anthropic_key else None
        self.lite_first = gemini.lite_first

    @property
    def ready(self) -> bool:
        return bool(self.client) or self.gemini.ready

    def _claude_allowed(self) -> bool:
        return bool(self.client and self.cfg.chat_model) and \
            self.memory.spent_since(day_start(), USAGE_PREFIX) < self.cfg.chat_daily

    async def text(self, system: str, contents: list[dict], max_tokens: int = 1024, models: list[str] | None = None,
                   effort: str = "medium") -> str:
        """The answer's text. effort: how hard Claude thinks first (low for a glance, medium when spoken to)."""
        messages = to_messages(contents) if self._claude_allowed() else None
        if messages is not None:
            for msg in messages:  # a sticker thumbnail that is not what it claims would fail the whole request
                msg["content"] = [b for b in msg["content"] if b["type"] != "image" or _valid_image(b)] or \
                                 [{"type": "text", "text": "[картинка не открылась]"}]
            try:
                return await self._claude(system, messages, max_tokens, effort)
            except anthropic.APIError as e:
                log.warning("Claude chat failed, Gemini instead: %s", str(e)[:300])
        if not self.gemini.ready:
            raise GeminiError("ни Claude, ни Gemini сейчас не отвечают")
        return await self.gemini.text(system, contents, max_tokens, models=models)

    async def _claude(self, system: str, messages: list[dict], max_tokens: int, effort: str) -> str:
        resp = await self.client.messages.create(
            model=self.cfg.chat_model, max_tokens=max_tokens + THINKING_ROOM,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=messages, output_config={"effort": effort})
        self._log_usage(resp.usage)
        if resp.stop_reason == "refusal":
            log.info("Claude declined a chat answer: %s", getattr(resp.stop_details, "category", None))
            return ""  # silence rather than another model saying what this one would not
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    def _log_usage(self, u) -> None:
        tokens_in = (u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25
                     + (u.cache_read_input_tokens or 0) * 0.1)
        price_in, price_out = self.cfg.chat_prices
        cost = (tokens_in * price_in + u.output_tokens * price_out) / 1e6
        self.memory.log_usage(USAGE_PREFIX + self.cfg.chat_model, int(tokens_in), u.output_tokens, cost)
