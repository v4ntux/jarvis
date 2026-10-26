"""Keeps Jarvis's own account inside Telegram's limits, so Telegram has no reason to stop it.

Every request to Telegram goes through one place (PacedClient). Messages are spaced out per chat and in
total, edits of one message are spaced too, and when Telegram still asks to wait (FloodWait), everyone waits
for that time instead of hitting it again: the owner's messages are delivered late, never lost. Each request
books its moment under a short lock and then waits on its own, so one long wait never holds up the others.
Reactions and «печатает…» are dropped instead of waiting, and so is anything sent under `at_most(seconds)`
(the «Работаю…» line) when its turn is further away than that: a task never stalls on a status line.

The account is watched hard by Telegram's anti-spam, so the limits are well below what Telegram allows.
"""
import asyncio
import bisect
import contextlib
import contextvars
import logging
import time

from telethon import TelegramClient, utils
from telethon.errors import FloodWaitError, PeerFloodError
from telethon.tl import functions, types

log = logging.getLogger("jarvis4.pace")

SEND = (functions.messages.SendMessageRequest, functions.messages.SendMediaRequest,
        functions.messages.SendMultiMediaRequest, functions.messages.ForwardMessagesRequest,
        functions.messages.SendInlineBotResultRequest)
EDIT = (functions.messages.EditMessageRequest,)
EXTRA = (functions.messages.SendReactionRequest, functions.messages.SetTypingRequest)
LOOKUP = (functions.contacts.ImportContactsRequest, functions.contacts.ResolveUsernameRequest)

GAP_PRIVATE = 1.2  # seconds between two messages in one private chat
GAP_GROUP = 3.0  # ... in one group
GAP_EDIT = 3.0  # between two edits of one message
PER_MINUTE = 14  # messages in all chats together
EDITS_PER_MINUTE = 12
EXTRAS_PER_MINUTE = 20  # reactions and «печатает…»
LOOKUPS_PER_HOUR = 20  # looking people up by phone or @username: Telegram is strict about it
# How long a message may wait for its turn: the owner's ones wait out even a long FloodWait, others go stale
WAIT_OWNER = 15 * 60
WAIT_OTHERS = 45
FLOOD_RETRIES = 3
PEER_FLOOD_PAUSE = 6 * 3600

_limit: contextvars.ContextVar[float | None] = contextvars.ContextVar("pace_limit", default=None)


@contextlib.contextmanager
def at_most(seconds: float):
    """Inside this block a request whose turn is more than `seconds` away is not sent at all (Throttled)."""
    token = _limit.set(seconds)
    try:
        yield
    finally:
        _limit.reset(token)


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
        self.last_sent: dict[int, float] = {}  # chat → when its last message goes (may be a booked moment ahead)
        self.last_edit: dict[tuple[int, int], float] = {}  # (chat, message) → when it was last edited
        self.windows: dict[str, list[float]] = {"send": [], "edit": [], "extra": [], "lookup": []}  # sorted
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
        at = max(now, self.cool_until.get(kind, 0))
        count, span = self.limits[kind]
        window = self.windows[kind]
        while window and window[0] <= now - span:
            window.pop(0)
        if len(window) >= count:
            at = max(at, window[-count] + span)  # the count-th newest booking leaves the window then
        if kind == "send" and chat:
            at = max(at, self.last_sent.get(chat, -1e9) + (GAP_GROUP if group else GAP_PRIVATE))
        if kind == "edit" and msg_id:
            at = max(at, self.last_edit.get((chat, msg_id), -1e9) + GAP_EDIT)
        return at - now

    def patience(self, kind: str, chat: int) -> float:
        limit = _limit.get()
        base = WAIT_OWNER if chat == self.owner_id else WAIT_OTHERS
        return base if limit is None else min(base, limit)

    def book(self, kind: str, chat: int, wait: float, msg_id: int = 0) -> None:
        """Takes the moment now + wait for this request, so the next one is judged after it."""
        at = self.clock() + wait
        bisect.insort(self.windows[kind], at)
        if kind == "send" and chat:
            self.last_sent[chat] = max(self.last_sent.get(chat, -1e9), at)
        if kind == "edit" and msg_id:
            self.last_edit[(chat, msg_id)] = max(self.last_edit.get((chat, msg_id), -1e9), at)

    def done(self, kind: str, chat: int, msg_id: int = 0) -> None:
        self.book(kind, chat, 0, msg_id)

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
        return "; ".join(parts) or f"лимиты в норме (FloodWait с запуска: {self.floods})"


class PacedClient(TelegramClient):
    """A TelegramClient whose every message, edit, reaction and lookup waits its turn (see Pace)."""

    # py-tgcalls picks its Telegram library by the client's module name: it must still read «telethon»
    __module__ = "telethon.client.telegramclient"
    pace: Pace

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        kind = _kind(request)
        if not kind:
            return await super().__call__(request, ordered, flood_sleep_threshold)
        pace = self.pace
        chat, group = _peer_key(request)
        msg_id = getattr(request, "id", 0) if kind == "edit" else 0
        for attempt in range(FLOOD_RETRIES + 1):
            if kind == "extra":
                # Never waits: SetTyping answers a bool, and a dropped reaction answers nothing anyone reads
                if pace.delay(kind, chat, group) > 0:
                    return True
                pace.done(kind, chat)
            else:
                async with pace.lock:  # only the booking is under the lock; the waiting is not
                    wait = pace.delay(kind, chat, group, msg_id)
                    if wait > pace.patience(kind, chat):
                        raise Throttled(request=request, capture=wait)
                    pace.book(kind, chat, wait, msg_id)
                if wait > 0:
                    await pace.sleep(wait)
            try:
                return await super().__call__(request, ordered, flood_sleep_threshold)
            except FloodWaitError as e:
                pace.flooded(kind, e.seconds)
                if kind == "extra" or attempt == FLOOD_RETRIES or e.seconds > pace.patience(kind, chat):
                    raise
                # The next round books a moment after the pause, so it waits there instead of hitting it again
            except PeerFloodError:
                pace.peer_flood_until = pace.clock() + PEER_FLOOD_PAUSE
                log.warning("Telegram's anti-spam refused a message to %s: messages to strangers are limited", chat)
                raise
