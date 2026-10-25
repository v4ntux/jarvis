"""Jarvis's taste in stickers and GIFs: he picks them himself, keeps favorites, and sends them like a person.

Stickers come from the packs installed on his own account (the first time he searches for a few meme packs and
installs them). A GIF is either a search through Telegram's @gif bot or one of his favorites: GIFs he liked
enough to keep go to the account's saved GIFs, and their descriptions into his prompt, so he can throw them
back later. Everything here is best effort: a failed sticker never breaks an answer.
"""
import logging
import random
import re
import time

log = logging.getLogger("jarvis.taste")

PACK_QUERIES = ("мемы", "dark meme", "кринж", "pepe", "cats meme")
MIN_PACKS = 3  # fewer installed packs than this and he goes looking for more
MAX_INSTALL = 5
POOL_TTL = 3600
GAP = 120  # seconds between two stickers or GIFs in one chat
HOURLY = 12  # stickers and GIFs per hour in all chats
CHOICES = 5  # a search gives several GIFs; one of the first few is picked
GIF_BOT = "gif"
BLOCKED = re.compile(r"porn|sex|nude|naked|nsfw|xxx|hentai|gore|секс|порно|голый|голая|обнаж|18\+", re.I)


class Taste:
    def __init__(self, client, memory, rng: random.Random | None = None):
        self.client, self.memory = client, memory
        self.rng = rng or random.Random()
        self.pool: dict[str, list] = {}  # emoji → stickers of his packs
        self.pool_at = 0.0
        self.sent: dict[int, float] = {}
        self.hourly: list[float] = []

    def may(self, chat: int) -> bool:
        now = time.time()
        self.hourly = [t for t in self.hourly if now - t < 3600]
        return now - self.sent.get(chat, 0) >= GAP and len(self.hourly) < HOURLY

    async def express(self, chat: int, sticker: str = "", gif: str = "", reply_to: int | None = None) -> bool:
        """Sends one sticker or GIF (the sticker wins when both are asked); True when something went out."""
        if not (sticker or gif) or not self.may(chat):
            return False
        try:
            done = bool(sticker) and await self._sticker(chat, sticker, reply_to)
            done = done or (bool(gif) and await self._gif(chat, gif, reply_to))
        except Exception as e:  # Telegram refuses things now and then; a missing sticker is not worth an error
            log.warning("sticker or GIF failed: %s: %s", type(e).__name__, e)
            return False
        if done:
            self.sent[chat] = time.time()
            self.hourly.append(time.time())
        return done

    async def love(self, msg, description: str) -> None:
        """He liked the GIF or sticker in this message: a GIF goes to his favorites, a sticker's pack gets installed."""
        from telethon import utils
        from telethon.errors import RPCError
        from telethon.tl.functions.messages import InstallStickerSetRequest, SaveGifRequest
        from telethon.tl.types import DocumentAttributeSticker, InputStickerSetID, InputStickerSetShortName

        try:
            if msg.gif:
                await self.client(SaveGifRequest(id=utils.get_input_document(msg.gif), unsave=False))
                fave = self.memory.fave_add("gif", msg.gif.id, description)
                log.info("a new favorite GIF #%s: %s", fave, description)
            elif msg.sticker:
                for attr in msg.sticker.attributes:
                    if isinstance(attr, DocumentAttributeSticker) and isinstance(
                            attr.stickerset, (InputStickerSetID, InputStickerSetShortName)):
                        await self.client(InstallStickerSetRequest(stickerset=attr.stickerset, archived=False))
                        self.pool_at = 0  # the pool picks the new pack up next time
                        log.info("installed the pack of a sticker he liked: %s", description)
                        break
        except RPCError as e:
            log.warning("could not keep it: %s", e)

    async def warm(self) -> None:
        """At startup: build the sticker pool, and find packs if he has hardly any."""
        try:
            await self._pool()
        except Exception as e:
            log.warning("sticker pool failed: %s: %s", type(e).__name__, e)

    # Stickers

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
        """A new account has no sticker packs: install the top meme packs from a few searches."""
        from telethon.tl.functions.messages import InstallStickerSetRequest, SearchStickerSetsRequest
        from telethon.tl.types import InputStickerSetID

        installed = 0
        for query in PACK_QUERIES:
            found = await self.client(SearchStickerSetsRequest(q=query, hash=0))
            for covered in list(getattr(found, "sets", []))[:1]:
                pack = covered.set
                await self.client(InstallStickerSetRequest(
                    stickerset=InputStickerSetID(pack.id, pack.access_hash), archived=False))
                log.info("installed sticker pack «%s» for «%s»", pack.title, query)
                installed += 1
            if installed >= MAX_INSTALL:
                break

    async def _sticker(self, chat: int, emoji: str, reply_to: int | None) -> bool:
        from telethon.tl.functions.messages import GetStickersRequest

        options = (await self._pool()).get(emoji.replace("️", ""))
        if not options:  # none of his packs has it: Telegram can look for the emoji everywhere
            options = list(getattr(await self.client(GetStickersRequest(emoticon=emoji, hash=0)), "stickers", []))
        if not options:
            return False
        await self.client.send_file(chat, self.rng.choice(options), reply_to=reply_to)
        return True

    # GIFs

    async def _gif(self, chat: int, what: str, reply_to: int | None) -> bool:
        if what.startswith("#"):
            return await self._favorite(chat, what[1:], reply_to)
        if BLOCKED.search(what):
            return False
        results = await self.client.inline_query(GIF_BOT, what)
        if not results:
            return False
        await self.rng.choice(list(results)[:CHOICES]).click(chat, reply_to=reply_to)
        return True

    async def _favorite(self, chat: int, number: str, reply_to: int | None) -> bool:
        from telethon.tl.functions.messages import GetSavedGifsRequest

        doc_id = self.memory.fave_doc(int(number)) if number.isdigit() else None
        if doc_id is None:
            return False
        gifs = getattr(await self.client(GetSavedGifsRequest(0)), "gifs", [])
        doc = next((g for g in gifs if g.id == doc_id), None)  # fresh from Telegram: file references go stale
        if doc is None:
            return False
        await self.client.send_file(chat, doc, reply_to=reply_to)
        return True
