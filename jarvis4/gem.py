"""Gemini, the free eyes and ears: it hears voice messages, looks at stickers, GIFs, videos and circles (Claude cannot),
and describes them in words that go into the chat history. It never talks to anybody and never decides anything.
"""
from __future__ import annotations

import base64
import logging
import re
import time

import httpx

from .config import Config

log = logging.getLogger("jarvis4.gem")

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TIMEOUT = 45
RETRY_STATUS = {429, 500, 502, 503, 504}
TRANSCRIBE = "Запиши дословно, что сказано. Только сам текст, без пояснений и кавычек. Если речи нет, ответь пустой строкой."
DESCRIBE = ("Опиши коротко (до 25 слов) по-русски, что на картинке или в ролике: сюжет, текст, эмоция, мем. Если это "
            "известный мем, назови его. Без вступлений.")
FIT = ("Выше {n} картинок по порядку (кадры стикеров или гифок, номер перед каждой). Момент в переписке: «{about}». "
       "Какие подходят к этому моменту по смыслу и настроению, так что человек в чате улыбнётся, а не удивится? "
       "Неуместные, оскорбительные и просто случайные не бери. Ответь только номерами через запятую, лучший первым; "
       "если ни одна не подходит, ответь 0.")
SAFETY = [{"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_ONLY_HIGH"}]


class GeminiError(RuntimeError):
    pass


def text_of(parts: list[dict]) -> str:
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def rest_for(status: int, data) -> float:
    error = data.get("error") if isinstance(data, dict) else None
    for detail in (error.get("details") or [] if isinstance(error, dict) else []):
        delay = str(detail.get("retryDelay", ""))
        if delay.endswith("s") and delay[:-1].replace(".", "", 1).isdigit():
            return min(float(delay[:-1]), 86400)
    return 60 if status == 429 else 20


class Gemini:
    def __init__(self, cfg: Config):
        self.key = cfg.gemini_key
        self.models = [m.strip() for m in cfg.gemini_models.split(",") if m.strip()]
        self.lite_first = sorted(self.models, key=lambda m: "lite" not in m)
        self.resting: dict[str, float] = {}

    @property
    def ready(self) -> bool:
        return bool(self.key and self.models)

    async def generate(self, system: str, contents: list[dict], max_tokens: int = 1024) -> str:
        errors = []
        for model in self.lite_first:
            if self.resting.get(model, 0) > time.time():
                errors.append(f"{model}: квота или перегрузка")
                continue
            body = {"systemInstruction": {"parts": [{"text": system}]}, "contents": contents, "safetySettings": SAFETY,
                    "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.4,
                                         "thinkingConfig": {"thinkingLevel": "minimal"}}}
            try:
                async with httpx.AsyncClient(timeout=TIMEOUT) as client:
                    r = await client.post(URL.format(model=model), json=body, headers={"x-goog-api-key": self.key})
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"{model}: {type(e).__name__}")
                continue
            if r.status_code in RETRY_STATUS:
                self.resting[model] = time.time() + rest_for(r.status_code, data)
                errors.append(f"{model}: {r.status_code}")
                continue
            if r.status_code != 200:
                raise GeminiError(f"Gemini {model} ответил {r.status_code}: {str(data)[:300]}")
            parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
            if text_of(parts):
                return text_of(parts)
            errors.append(f"{model}: пусто")
        raise GeminiError("Gemini не ответил: " + "; ".join(errors))

    async def transcribe(self, audio: bytes, mime: str) -> str:
        part = {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}}
        text = await self.generate("Ты точная стенографистка.", [{"role": "user", "parts": [part, {"text": TRANSCRIBE}]}])
        return text.strip('"«» ')

    async def describe(self, parts: list[dict]) -> str:
        """What a sticker, GIF or video shows, in a few words."""
        return await self.generate("Ты описываешь картинки и видео для друга, который их не видит.",
                                   [{"role": "user", "parts": [*parts, {"text": DESCRIBE}]}], max_tokens=300)

    async def fitting(self, frames: list[tuple[str, bytes]], about: str) -> list[int]:
        """Which pictures (mime, bytes) fit the moment, best first, as 0-based positions; [] when none does."""
        parts: list[dict] = []
        for n, (mime, data) in enumerate(frames, 1):
            parts += [{"text": f"#{n}"}, {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}]
        text = await self.generate("Ты со вкусом подбираешь стикеры и гифки к переписке друзей.",
                                   [{"role": "user", "parts": [*parts, {"text": FIT.format(n=len(frames), about=about)}]}],
                                   max_tokens=60)
        picked = []
        for number in re.findall(r"\d+", text):
            i = int(number) - 1
            if 0 <= i < len(frames) and i not in picked:
                picked.append(i)
        return picked
