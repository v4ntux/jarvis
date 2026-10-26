"""Reaching the owner when something happens without him asking: reminders, urgent alarms, news from the monitor.

An alarm rings in Telegram (a call, or a voice message when he does not pick up) every 100 seconds until he says
anything in his chat; after five minutes of silence an SMS and a robot call go to his phone, which works without
mobile internet. Plain news is one message.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from .ops.phone import PhoneError, ring_until_heard

log = logging.getLogger("jarvis4.alerts")

TICK = 10


class Alerts:
    def __init__(self, core, account, phone, store):
        self.core, self.account, self.phone, self.store = core, account, phone, store
        self.heard: dict[str, asyncio.Event] = {}
        core.owner_hooks.append(self.owner_seen)

    # ---- plumbing ----------------------------------------------------------------------------------------------

    def owner_seen(self) -> None:
        """Any word from the owner in his chat counts as «принял» for every running alarm."""
        for event in self.heard.values():
            event.set()

    async def say(self, text: str) -> None:
        """A message to the owner from Jarvis himself: stored, so «что ты мне писал» has an answer."""
        out = self.core.out_for("owner")
        for piece in _pieces(text):
            ref = await out.say(piece)
            if ref:
                self.store.add_msg(ref, "owner", "jarvis", piece)

    async def ring(self, text: str) -> bool:
        """A Telegram call to the owner with `text`; True when he picked up."""
        if self.account.dry("ring", text[:100]):
            return True
        api = self.account.callapi
        return bool(api and await api.ring_owner(text))

    # ---- reminders ---------------------------------------------------------------------------------------------

    async def reminders_loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("reminder pass failed")
            await asyncio.sleep(TICK)

    async def tick(self) -> None:
        if self.core.frozen():
            return
        for rid, text, call in self.store.due_reminders():
            self.store.finish_reminder(rid)
            try:
                if call:
                    if not await self.ring(text):
                        await self.say(f"📞 звонил, ты не взял: {text}")
                else:
                    await self.say(f"⏰ {text}")
            except Exception:
                log.exception("reminder %s failed", rid)

    # ---- alarms ------------------------------------------------------------------------------------------------

    async def alarm(self, text: str) -> None:
        key = uuid.uuid4().hex[:8]
        self.heard[key] = heard = asyncio.Event()
        await self.say("🚨 " + text[:3500] + "\n\nОтветь что угодно или возьми трубку, и я перестану звонить.")

        async def remind() -> None:
            if await self.ring("Срочно. " + text[:300]):
                heard.set()
            elif not self.account.dry("voice"):
                try:
                    await self.account.send_voice("owner", text[:500])
                except Exception:
                    log.exception("alarm voice failed")

        async def escalate() -> None:
            if self.phone is None or not self.phone.ready:
                return
            report = []
            for send in (self.phone.sms, self.phone.call):
                try:
                    report.append(await send("Джарвис: " + text[:280]))
                except PhoneError as e:
                    report.append(f"Ошибка: {e}")
            await self.say("📵 ты не ответил, продублировал на телефон:\n" + "\n".join(report))

        try:
            await ring_until_heard(heard, remind, escalate)
        finally:
            self.heard.pop(key, None)

    async def monitor_news(self, text: str, urgent: bool) -> None:
        """Sites and servers: plain news is a message, a stranger on a server rings until he answers."""
        if urgent:
            self.core.spawn(self.alarm(text))
        else:
            await self.say("📡 " + text)


def _pieces(text: str, size: int = 3900) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]
