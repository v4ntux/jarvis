"""Jarvis's stickers and GIFs: he picks them himself, keeps favorites, and sends them like a person.

Stickers come from the packs installed on his own account (the first time he searches for a few meme packs and
installs them), and from what his friends send: a sticker seen in a chat is added to the pool of that emoji. A GIF is
a search through Telegram's @gif bot or one of his favorites. Before anything goes out Gemini (free) looks at a few
candidates and picks the one that fits the moment; when none fits, nothing is sent and he answers in words. Everything
here is best effort: a failed sticker never breaks an answer.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time

log = logging.getLogger("jarvis4.taste")

PACK_QUERIES = ("мемы", "dark meme", "кринж", "pepe", "cats meme")
MIN_PACKS = 3
MAX_INSTALL = 5
POOL_TTL = 3600
GAP = 20  # seconds between two stickers or GIFs in one chat
HOURLY = 60
CHOICES = 5
LOOK_AT = 6  # candidates Gemini sees before one is picked
FRAME_MAX = 1024 * 1024
GIF_BOT = "gif"
BLOCKED = re.compile(r"porn|sex|nude|naked|nsfw|xxx|hentai|gore|секс|порно|голый|голая|обнаж|18\+", re.I)


class Taste:
    def __init__(self, client, store, rng: random.Random | None = None, gem=None):
        self.client, self.store, self.gem = client, store, gem
        self.rng = rng or random.Random()
        self.pool: dict[str, list] = {}  # emoji → stickers of his packs
        self.pool_at = 0.0
        self.friends: dict[str, list] = {}  # emoji → stickers people sent him lately (he sends them back)
        self.sent: dict[int, float] = {}
        self.hourly: list[float] = []
        self.frames: dict[int, tuple[str, bytes]] = {}  # document id → a frame Gemini can look at

    def may(self, chat: int) -> bool:
        now = time.time()
        self.hourly = [t for t in self.hourly if now - t < 3600]
        return now - self.sent.get(chat, 0) >= GAP and len(self.hourly) < HOURLY

    async def express(self, entity, chat_id: int, sticker: str = "", gif: str = "", reply_to: int | None = None,
                      about: str = ""):
        """Sends one sticker or GIF (the sticker wins when both are asked) that fits «about»; the sent message, or None."""
        if not (sticker or gif) or not self.may(chat_id):
            return None
        try:
            sent = await self._sticker(entity, sticker, reply_to, about) if sticker else None
            sent = sent or (await self._gif(entity, gif, reply_to, about) if gif else None)
        except Exception as e:  # Telegram refuses things now and then; a missing sticker is not worth an error
            log.warning("sticker or GIF failed: %s: %s", type(e).__name__, e)
            return None
        if sent:
            self.sent[chat_id] = time.time()
            self.hourly.append(time.time())
        return sent

    def saw_sticker(self, msg) -> None:
        """A friend sent a sticker: it joins the stickers Jarvis may send for that emoji."""
        emoji = ((msg.file.emoji if msg.file else "") or "").replace("️", "")
        if emoji and msg.sticker:
            bucket = self.friends.setdefault(emoji, [])
            if all(d.id != msg.sticker.id for d in bucket):
                bucket.append(msg.sticker)
                del bucket[:-30]

    async def love(self, msg, description: str) -> None:
        """He liked the GIF or sticker in this message: a GIF goes to his favorites, a sticker's pack gets installed."""
        from telethon import utils
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import InstallStickerSetRequest, SaveGifRequest
        from telethon.tl.types import DocumentAttributeSticker, InputStickerSetID, InputStickerSetShortName

        try:
            if msg.gif:
                await self.client(SaveGifRequest(id=utils.get_input_document(msg.gif), unsave=False))
                self.store.fave_add("gif", msg.gif.id, description)
            elif msg.sticker:
                for attr in msg.sticker.attributes:
                    if isinstance(attr, DocumentAttributeSticker) and isinstance(
                            attr.stickerset, (InputStickerSetID, InputStickerSetShortName)):
                        await self.client(InstallStickerSetRequest(stickerset=attr.stickerset, archived=False))
                        self.pool_at = 0
                        break
        except RPCError as e:
            log.warning("could not keep it: %s", e)

    async def warm(self) -> None:
        try:
            await self._pool()
        except Exception as e:
            log.warning("sticker pool failed: %s: %s", type(e).__name__, e)

    async def _pool(self) -> dict[str, list]:
        if self.pool and time.time() - self.pool_at < POOL_TTL:
            return self.pool
        from telethon.tl.functions.messages import GetAllStickersRequest, GetStickerSetRequest
        from telethon.tl.types import InputStickerSetID

        sets = list(getattr(await self.client(GetAllStickersRequest(0)), "sets", []))
        if len(sets) < MIN_PACKS:
            await self._find_packs()
            sets = list(getattr(await self.client(GetAllStickersRequest(0)), "sets", []))
        pool: dict[str, list] = {}
        for pack in sets[:15]:
            full = await self.client(GetStickerSetRequest(InputStickerSetID(pack.id, pack.access_hash), 0))
            docs = {d.id: d for d in full.documents}
            for entry in full.packs:
                for doc_id in entry.documents:
                    if doc_id in docs:
                        pool.setdefault(entry.emoticon.replace("️", ""), []).append(docs[doc_id])
        self.pool, self.pool_at = pool, time.time()
        log.info("sticker pool: %d packs, %d emoji", len(sets), len(pool))
        return pool

    async def _find_packs(self) -> None:
        from telethon.tl.functions.messages import InstallStickerSetRequest, SearchStickerSetsRequest
        from telethon.tl.types import InputStickerSetID

        installed = 0
        for query in PACK_QUERIES:
            found = await self.client(SearchStickerSetsRequest(q=query, hash=0))
            for covered in list(getattr(found, "sets", []))[:1]:
                pack = covered.set
                await self.client(InstallStickerSetRequest(
                    stickerset=InputStickerSetID(pack.id, pack.access_hash), archived=False))
                installed += 1
            if installed >= MAX_INSTALL:
                break

    async def _frame(self, doc) -> tuple[str, bytes]:
        """A still Gemini can look at: the sticker itself when it is a small webp, else the preview frame."""
        if doc.id in self.frames:
            return self.frames[doc.id]
        from . import media

        if getattr(doc, "mime_type", "") == "image/webp" and (getattr(doc, "size", 0) or 0) <= FRAME_MAX:
            data, mime = await self.client.download_media(doc, file=bytes), "image/webp"
        else:
            data = await self.client.download_media(doc, file=bytes, thumb=-1)
            mime = media.sniff(data or b"")
        if len(self.frames) > 400:
            self.frames.clear()
        self.frames[doc.id] = (mime, data or b"")
        return self.frames[doc.id]

    async def _pick(self, docs: list, about: str, firsts: int) -> int | None:
        """Which of the docs to send: the best fit by Gemini's eye; random among the first few when nobody can look
        (no Gemini, no frames); None when Gemini saw them and none fits the moment."""
        if not docs:
            return None
        if not about or self.gem is None or not self.gem.ready:
            return self.rng.randrange(min(firsts, len(docs)))
        frames = await asyncio.gather(*(self._frame(d) for d in docs), return_exceptions=True)
        shown = [i for i, f in enumerate(frames) if isinstance(f, tuple) and f[1]]
        if not shown:
            return self.rng.randrange(min(firsts, len(docs)))
        try:
            order = await self.gem.fitting([frames[i] for i in shown], about)
        except Exception as e:
            log.info("fit check skipped: %s", e)
            return self.rng.randrange(min(firsts, len(docs)))
        if not order:
            log.info("nothing fits «%s» among %d", about, len(shown))
            return None
        return shown[self.rng.choice(order[:2])]  # the best two, so the same moment does not always get the same one

    async def _sticker(self, entity, emoji: str, reply_to: int | None, about: str = ""):
        from telethon.tl.functions.messages import GetStickersRequest

        key = emoji.replace("️", "")
        options = list((await self._pool()).get(key) or []) + self.friends.get(key, [])
        if not options:
            options = list(getattr(await self.client(GetStickersRequest(emoticon=emoji, hash=0)), "stickers", []))
        if not options:
            return None
        sample = self.rng.sample(options, min(LOOK_AT, len(options)))
        i = await self._pick(sample, about, len(sample))
        return None if i is None else await self.client.send_file(entity, sample[i], reply_to=reply_to)

    async def _gif(self, entity, what: str, reply_to: int | None, about: str = ""):
        if what.startswith("#"):
            return await self._favorite(entity, what[1:], reply_to)
        if BLOCKED.search(what):
            return None
        results = [r for r in list(await self.client.inline_query(GIF_BOT, what) or [])[:LOOK_AT + 2]
                   if getattr(r, "document", None) is not None][:LOOK_AT]
        i = await self._pick([r.document for r in results], about or what, CHOICES)
        return None if i is None else await results[i].click(entity, reply_to=reply_to)

    async def _favorite(self, entity, number: str, reply_to: int | None):
        from telethon.tl.functions.messages import GetSavedGifsRequest

        doc_id = self.store.fave_doc(int(number)) if number.isdigit() else None
        if doc_id is None:
            return None
        gifs = getattr(await self.client(GetSavedGifsRequest(0)), "gifs", [])
        doc = next((g for g in gifs if g.id == doc_id), None)
        if doc is None:
            return None
        return await self.client.send_file(entity, doc, reply_to=reply_to)
