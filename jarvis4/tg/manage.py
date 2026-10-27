"""Everything else a person does with his Telegram account: join and leave, block, make and delete channels and groups,
run a group (kick, ban, mute, admins, pins, title), change his own profile.

A mixin of Account, so it shares the client, the pacer, the store and rehearsal mode. Every method raises TGError with
words the owner can read; the tools decide who may ask for what.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from ..ports import TGError

INVITE = re.compile(r"(?:t\.me|telegram\.me)/(?:\+|joinchat/)([\w-]+)", re.I)
PUBLIC = re.compile(r"(?:(?:t\.me|telegram\.me)/|@)([a-z]\w{3,31})", re.I)
FOREVER = None


class Manage:
    """Mixed into Account: self.client, self.store, self.dry, self.entity, self.group_key are there."""

    def _kind(self, chat) -> str:
        if getattr(chat, "broadcast", False):
            return "channel"
        return "group"

    def _keep(self, chat) -> str:
        from telethon import utils

        key = self.group_key(utils.get_peer_id(chat))
        self.store.chat_seen(key, self._kind(chat), getattr(chat, "title", "") or "чат", utils.get_peer_id(chat))
        return key

    async def join(self, link: str) -> tuple[str, str, str]:
        """(chat key, title, kind) of the group or channel joined by @name, t.me/name or an invite link."""
        from telethon.errors import RPCError, UserAlreadyParticipantError
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest

        if self.dry("join", link):
            return f"g{abs(hash(link)) % 10**9}", link, "group"
        invite, public = INVITE.search(link), PUBLIC.search(link)
        try:
            if invite:
                try:
                    res = await self.client(ImportChatInviteRequest(invite.group(1)))
                    chat = res.chats[0]
                except UserAlreadyParticipantError:
                    chat = (await self.client(CheckChatInviteRequest(invite.group(1)))).chat
            elif public:
                entity = await self.client.get_entity(public.group(1))
                if not hasattr(entity, "title"):
                    raise TGError(f"@{public.group(1)} — это человек, а не группа или канал")
                res = await self.client(JoinChannelRequest(entity))
                chat = res.chats[0] if getattr(res, "chats", None) else entity
            else:
                raise TGError("нужна ссылка t.me/… или @имя группы/канала")
        except (RPCError, ValueError) as e:
            raise TGError(f"Telegram не пустил: {e}")
        return self._keep(chat), chat.title, self._kind(chat)

    async def block(self, person: str, on: bool = True) -> None:
        from telethon.errors import RPCError
        from telethon.tl.functions.contacts import BlockRequest, UnblockRequest

        if self.dry("block" if on else "unblock", person):
            return
        try:
            await self.client((BlockRequest if on else UnblockRequest)(id=await self.entity(person)))
        except RPCError as e:
            raise TGError(f"Telegram не дал: {e}")

    async def create_chat(self, title: str, about: str = "", channel: bool = True, members: list[str] | None = None,
                          username: str = "") -> tuple[str, str]:
        """(chat key, link) of a new channel (channel=True) or group with the people (keys) added."""
        from telethon.errors import RPCError
        from telethon.tl.functions.channels import CreateChannelRequest, InviteToChannelRequest, UpdateUsernameRequest
        from telethon.tl.functions.messages import ExportChatInviteRequest

        if self.dry("create", f"{'канал' if channel else 'группа'} {title}"):
            return f"g{abs(hash(title)) % 10**9}", "https://t.me/+dry"
        try:
            res = await self.client(CreateChannelRequest(title=title, about=about, broadcast=channel,
                                                         megagroup=not channel))
            chat = res.chats[0]
            key = self._keep(chat)
            if members:
                users = [await self.entity(m) for m in members]
                await self.client(InviteToChannelRequest(chat, users))
            if username:
                await self.client(UpdateUsernameRequest(chat, username.lstrip("@")))
                return key, f"https://t.me/{username.lstrip('@')}"
            link = (await self.client(ExportChatInviteRequest(chat))).link
        except RPCError as e:
            raise TGError(f"Telegram не дал: {e}")
        return key, link

    async def delete_chat(self, chat: str) -> None:
        """Deletes a channel or group Jarvis created (for everybody), or leaves one he did not."""
        from telethon.errors import RPCError
        from telethon.tl.functions.channels import DeleteChannelRequest

        if self.dry("delete chat", chat):
            return
        entity = await self.entity(chat)
        try:
            if getattr(entity, "channel_id", None):
                await self.client(DeleteChannelRequest(entity))
            else:
                await self.client.delete_dialog(entity, revoke=True)
        except RPCError as e:
            raise TGError(f"Telegram не дал удалить (удалять может только создатель): {e}")

    async def is_admin(self, chat: str, person: str) -> bool:
        """Is this person an admin (or the creator) of the group, in Telegram itself?"""
        try:
            perms = await self.client.get_permissions(await self.entity(chat), await self.entity(person))
        except Exception:
            return False
        return bool(perms.is_admin or perms.is_creator)

    async def moderate(self, chat: str, action: str, person: str = "", msg_id: int = 0, text: str = "",
                       minutes: int = 0) -> str:
        """One move of a group admin; the words of what happened."""
        from telethon.errors import RPCError
        from telethon.tl.functions.channels import EditTitleRequest
        from telethon.tl.functions.messages import EditChatAboutRequest, ExportChatInviteRequest

        if self.dry("moderate", f"{chat} {action} {person} {msg_id} {text[:60]}"):
            return "(репетиция) сделал"
        entity = await self.entity(chat)
        who = await self.entity(person) if person else None
        until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=minutes) if minutes else FOREVER
        c = self.client
        try:
            if action == "kick":
                await c.kick_participant(entity, who)
            elif action == "ban":
                await c.edit_permissions(entity, who, until_date=until, view_messages=False)
            elif action == "unban":
                await c.edit_permissions(entity, who, view_messages=True)
            elif action == "mute":
                await c.edit_permissions(entity, who, until_date=until, send_messages=False)
            elif action == "unmute":
                await c.edit_permissions(entity, who, send_messages=True, send_media=True, send_stickers=True,
                                         send_gifs=True)
            elif action == "admin":
                await c.edit_admin(entity, who, is_admin=True, title=text[:16] or None)
            elif action == "unadmin":
                await c.edit_admin(entity, who, is_admin=False)
            elif action == "invite":
                from telethon.tl.functions.channels import InviteToChannelRequest
                await c(InviteToChannelRequest(entity, [who]))
            elif action == "pin":
                await c.pin_message(entity, msg_id, notify=False)
            elif action == "unpin":
                await c.unpin_message(entity, msg_id or None)
            elif action == "delete":
                await c.delete_messages(entity, [msg_id])
            elif action == "title":
                await c(EditTitleRequest(entity, text[:128]))
                self.store.chat_seen(chat, (self.store.chat(chat) or ("group",))[0], text[:128])
            elif action == "about":
                await c(EditChatAboutRequest(entity, text[:255]))
            elif action == "link":
                return (await c(ExportChatInviteRequest(entity))).link
            else:
                raise TGError(f"не знаю действия {action}")
        except RPCError as e:
            raise TGError(f"Telegram не дал ({action}): {e}. Может, я там не админ")
        return "сделал"

    async def set_profile(self, first_name: str = "", last_name: str | None = None, bio: str | None = None,
                          username: str = "", photo: str = "") -> str:
        from telethon.errors import RPCError
        from telethon.tl.functions.account import UpdateProfileRequest, UpdateUsernameRequest
        from telethon.tl.functions.photos import UploadProfilePhotoRequest

        if self.dry("profile", f"{first_name} {last_name} {bio} {username} {photo}"):
            return "(репетиция) обновил"
        changed = []
        try:
            if first_name or last_name is not None or bio is not None:
                await self.client(UpdateProfileRequest(first_name=first_name or None, last_name=last_name,
                                                       about=bio))
                changed.append("имя/о себе")
            if username:
                await self.client(UpdateUsernameRequest(username.lstrip("@")))
                changed.append("username")
            if photo:
                if not os.path.isfile(photo):
                    raise TGError(f"нет файла {photo}")
                await self.client(UploadProfilePhotoRequest(file=await self.client.upload_file(photo)))
                changed.append("аватарка")
        except RPCError as e:
            raise TGError(f"Telegram не дал: {e}")
        return "обновил: " + ", ".join(changed) if changed else "нечего менять"
