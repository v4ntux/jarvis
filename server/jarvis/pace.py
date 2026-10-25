"""Keeps Jarvis's own account inside Telegram's limits, so Telegram has no reason to stop it.

Every request to Telegram goes through one place (PacedClient). Messages are spaced out per chat and in
total, edits of one message are spaced too, and when Telegram still asks to wait (FloodWait), everyone waits
for that time instead of hitting it again: the owner's messages are delivered late, never lost, and nothing
else is sent meanwhile. Reactions and «печатает…» are the first to go when it gets tight.

A new account is watched harder by Telegram's anti-spam, so the limits are well below what Telegram allows.
"""
import asyncio
import logging
import time
from collections import deque

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl import functions, types

log = logging.getLogger("jarvis.pace")

SEND = (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest,
        functions.messages.SendMultiMediaRequest, functions.messages.ForwardMessagesRequest,
        functions.messages.SendInlineBotResultRequest)
EDIT = (functions.messages.EditMessageRequest,)
EXTRA = (functions.messages.SendReactionRequest, functions.messages.SetTypingRequest)
LOOKUP = (functions.contacts.ImportContactsRequest, functions.contacts.ResolveUsernameRequest)

GAP_PRIVATE = 1.2  # seconds between two messages in one private chat
GAP_GROUP = 4.0  # ... in one group (Telegram allows about 20 a minute there)
GAP_EDIT = 3.0  # between two edits of one message
PER_MINUTE = 18  # messages in all chats together
EDITS_PER_MINUTE = 15
EXTRAS_PER_MINUTE = 20  # reactions and «печатает…»
LOOKUPS_PER_HOUR = 20  # looking people up by phone or @username: Telegram is strict about it
# How long a message may wait for its turn: the owner's ones wait out even a long FloodWait, others go stale
WAIT_OWNER = 15 * 60
WAIT_OTHERS = 45
PEER_FLOOD_PAUSE = 6 * 3600


class Throttled(FloodWaitError):
    """Raised instead of sending when a message would have to wait too long; callers treat it as a FloodWait."""


def _peer_key(request) -> tuple[int, bool]:
    """(chat id, is a group) of the chat a request goes to; (0, False) when it has none."""
    peer = getattr(request, "to_peer", None) or getattr(request, "peer", None)
    if peer is None:
        return 0, False
    try:
        return utils.get_peer_id(peer), isinstance(peer, (types.InputPeerChat, types.InputPeerChannel,
                                                          types.PeerChat, types.PeerChannel))
    except (TypeError, ValueError):
        return 0, False


def _kind(request) -> str:
    for kind, classes in (("send", SEND), ("edit", EDIT), ("extra", EXTRA), ("lookup", LOOKUP)):
        if isinstance(request, classes):
            return kind
    return ""


class Pace:
    """The bookkeeping, separate from Telethon so it can be tested with a fake clock."""

    def __init__(self, owner_id: int, clock=time.monotonic):
        self.owner_id, self.clock = owner_id, clock
        self.last_sent: dict[int, float] = {}  # chat → when the last message went there
        self.last_edit: dict[tuple[int, int], float] = {}  # (chat, message) → when it was last edited
        self.windows = {"send": deque(), "edit": deque(), "extra": deque(), "lookup": deque()}
        self.limits = {"send": (PER_MINUTE, 60), "edit": (EDITS_PER_MINUTE, 60), "extra": (EXTRAS_PER_MINUTE, 60),
                       "lookup": (LOOKUPS_PER_HOUR, 3600)}
        self.cool_until: dict[str, float] = {}  # kind → Telegram asked to wait until then
        self.peer_flood_until = 0.0  # Telegram's anti-spam limited messages to strangers
        self.floods = 0  # FloodWaits seen since start, for /status
        self.lock = asyncio.Lock()
        self.sleep = asyncio.sleep  # tests put a fake clock here

    def delay(self, kind: str, chat: int, group: bool, msg_id: int = 0) -> float:
        """Seconds this request has to wait from now (0: go)."""
        now = self.clock()
        wait = max(0.0, self.cool_until.get(kind, 0) - now)
        count, span = self.limits[kind]
        window = self.windows[kind]
        while window and now - window[0] >= span:
            window.popleft()
        if len(window) >= count:
            wait = max(wait, window[0] + span - now)
        if kind == "send" and chat:
            wait = max(wait, self.last_sent.get(chat, -1e9) + (GAP_GROUP if group else GAP_PRIVATE) - now)
        if kind == "edit" and msg_id:
            wait = max(wait, self.last_edit.get((chat, msg_id), -1e9) + GAP_EDIT - now)
        return wait

    def patience(self, kind: str, chat: int) -> float:
        return WAIT_OWNER if chat == self.owner_id else WAIT_OTHERS

    def done(self, kind: str, chat: int, msg_id: int = 0) -> None:
        now = self.clock()
        self.windows[kind].append(now)
        if kind == "send" and chat:
            self.last_sent[chat] = now
        if kind == "edit" and msg_id:
            self.last_edit[(chat, msg_id)] = now

    def flooded(self, kind: str, seconds: float) -> None:
        self.floods += 1
        self.cool_until[kind] = max(self.cool_until.get(kind, 0), self.clock() + seconds + 1)
        log.warning("Telegram asked to wait %ss before the next %s", seconds, kind)

    def status(self) -> str:
        now = self.clock()
        parts = []
        for kind, until in self.cool_until.items():
            if until > now:
                parts.append(f"{kind}: пауза ещё {int(until - now)} с")
        if self.peer_flood_until > now:
            parts.append(f"антиспам Telegram: писать первым незнакомым нельзя ещё ~{int((self.peer_flood_until - now) / 60)} мин")
        return "; ".join(parts) or "лимиты в норме"


class PacedClient(TelegramClient):
    """A TelegramClient whose every message, edit, reaction and lookup waits its turn (see Pace)."""

    # py-tgcalls picks its Telegram library by the client's module name: it must still read «telethon»
    __module__ = "telethon.client.telegramclient"
    pace: Pace

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None, retried=False):
        kind = _kind(request)
        if not kind:
            return await super().__call__(request, ordered, flood_sleep_threshold)
        pace = self.pace
        chat, group = _peer_key(request)
        msg_id = getattr(request, "id", 0) if kind == "edit" else 0
        if kind == "extra":
            # Never waits: SetTyping answers a bool, and a dropped reaction answers nothing anyone reads
            if pace.lock.locked() or pace.delay(kind, chat, group) > 0:
                return True
            pace.done(kind, chat)
        else:
            async with pace.lock:  # one at a time: the bookkeeping must see each send before the next is judged
                wait = pace.delay(kind, chat, group, msg_id)
                if wait > pace.patience(kind, chat):
                    raise Throttled(request=request, capture=wait)
                if wait > 0:
                    await pace.sleep(wait)
                pace.done(kind, chat, msg_id)
        try:
            return await super().__call__(request, ordered, flood_sleep_threshold)
        except FloodWaitError as e:
            pace.flooded(kind, e.seconds)
            if kind == "extra" or retried or e.seconds > pace.patience(kind, chat):
                raise
            await pace.sleep(e.seconds + 1)
            return await self.__call__(request, ordered, flood_sleep_threshold, retried=True)
        except PeerFloodError:
            pace.peer_flood_until = pace.clock() + PEER_FLOOD_PAUSE
            log.warning("Telegram's anti-spam refused a message to %s: messages to strangers are limited", chat)
            raise
