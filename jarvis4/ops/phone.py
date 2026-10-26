"""SMS and robot calls to the owner's own phone through SMSC.ru.

Plain SMS and calls keep working when mobile internet is cut off, so this is how
Jarvis gets through when Telegram does not. Only the owner's number is ever used.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable

import httpx

from ..clock import day_start
from ..config import Config
from ..store import Store

log = logging.getLogger("jarvis4.phone")

URL = "https://smsc.ru/sys/send.php"
# Cyrillic SMS parts hold 67 characters; long texts multiply the price
MAX_SMS = 300
MAX_CALL = 500
# Urgent events: a Telegram voice message every ALARM_EVERY seconds, ALARM_REPEATS times;
# with no «Принял» after PHONE_AFTER seconds, an SMS and a robot call follow
ALARM_REPEATS = 6
ALARM_EVERY = 100
PHONE_AFTER = 300


class PhoneError(RuntimeError):
    pass


class Phone:
    def __init__(self, cfg: Config, memory: Store):
        self.cfg = cfg
        self.memory = memory

    @property
    def ready(self) -> bool:
        return bool(self.cfg.owner_phone and self.cfg.smsc_login and (self.cfg.smsc_api_key or self.cfg.smsc_password))

    async def sms(self, text: str) -> str:
        return await self._send("sms", text[:MAX_SMS])

    async def call(self, text: str) -> str:
        return await self._send("call", text[:MAX_CALL])

    async def _send(self, kind: str, text: str) -> str:
        if not self.ready:
            raise PhoneError("SMS и звонки не настроены: в /opt/jarvis/.env нужны SMSC_LOGIN, "
                             "SMSC_PASSWORD (или SMSC_API_KEY) и OWNER_PHONE.")
        used, _ = self.memory.phone_since(day_start())
        if used >= self.cfg.phone_daily_limit:
            raise PhoneError(f"дневной лимит SMS и звонков исчерпан ({self.cfg.phone_daily_limit}).")
        data = {"login": self.cfg.smsc_login, "phones": self.cfg.owner_phone, "mes": text,
                "charset": "utf-8", "fmt": 3, "cost": 3}
        if self.cfg.smsc_api_key:
            data["apikey"] = self.cfg.smsc_api_key
        else:
            data["psw"] = self.cfg.smsc_password
        if kind == "call":
            data.update(call=1, voice=self.cfg.smsc_voice)
        res = await self._post(data)
        if "error" in res:
            raise PhoneError(f"SMSC отказал: {res['error']} (код {res.get('error_code')}).")
        cost = float(res.get("cost") or 0)
        self.memory.log_phone(kind, cost)
        log.info("%s sent, cost %s", kind, cost)
        what = "Звоню" if kind == "call" else "SMS отправлено"
        return f"{what}: {cost:.2f} ₽, на счёте SMSC {res.get('balance', '?')} ₽."

    @staticmethod
    async def _post(data: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(URL, data=data)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise PhoneError(f"SMSC недоступен: {type(e).__name__}: {e}") from e


async def ring_until_heard(heard: asyncio.Event, remind: Callable[[], Awaitable[None]],
                           escalate: Callable[[], Awaitable[None]], repeats: int = ALARM_REPEATS,
                           every: float = ALARM_EVERY, escalate_after: float = PHONE_AFTER) -> bool:
    """Repeats remind() until heard is set; escalate() runs once if nobody answers in time.

    Returns True when the owner answered.
    """
    started, escalated = time.monotonic(), False
    for _ in range(repeats):
        await remind()
        try:
            await asyncio.wait_for(heard.wait(), every)
            return True
        except asyncio.TimeoutError:
            pass
        if not escalated and time.monotonic() - started >= escalate_after:
            escalated = True
            await escalate()
    return heard.is_set()
