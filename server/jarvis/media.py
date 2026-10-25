"""Looking at what people send: photos, stickers, GIFs, videos and video circles, as parts for Gemini.

Gemini sees pictures and video (with sound) directly. Big or long videos are shrunk with ffmpeg first;
animated Telegram stickers (Lottie) are shown by their preview frame and emoji.
"""
import asyncio
import base64
import logging
import os
import tempfile

log = logging.getLogger("jarvis.media")

INLINE_LIMIT = 12 * 1024 * 1024  # what goes to Gemini in one request
MAX_SECONDS = 90
OWNER_MAX = 60 * 1024 * 1024  # biggest download from the owner
STRANGER_MAX = 8 * 1024 * 1024  # strangers get no bandwidth for long videos


def sniff(data: bytes) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def part(mime: str, data: bytes) -> dict:
    return {"inlineData": {"mimeType": mime, "data": base64.b64encode(data).decode()}}


async def shrink(data: bytes) -> bytes:
    """A smaller copy of a video: 360p, 10 frames a second, at most MAX_SECONDS, mono sound."""
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = os.path.join(tmp, "in"), os.path.join(tmp, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", src, "-t", str(MAX_SECONDS), "-vf", "scale=-2:360", "-r", "10",
            "-c:v", "libx264", "-crf", "32", "-preset", "veryfast", "-c:a", "aac", "-b:a", "32k", "-ac", "1", dst)
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg не смог сжать видео")
        with open(dst, "rb") as f:
            return f.read()


def label(m) -> str:
    """What a message carries, in words and without downloading anything: '' for plain text."""
    if m.sticker:
        emoji = ((m.file.emoji if m.file else "") or "").strip()
        return f"[стикер {emoji}]".replace(" ]", "]")
    if m.photo:
        return "[фото]"
    if m.gif or m.video or m.video_note:
        kind = "гифка" if m.gif else "кружок" if m.video_note else "видео"
        seconds = int(m.file.duration or 0) if m.file else 0
        return f"[{kind}" + (f" {seconds} сек]" if seconds and not m.gif else "]")
    if m.document and m.file and (m.file.mime_type or "").startswith("image/"):
        return "[картинка файлом]"
    return ""


async def look(m, limit: int) -> tuple[str, list[dict]]:
    """(what it is in words, parts for Gemini) for a photo, sticker, GIF, video or circle; ('', []) for the rest."""
    words = label(m)
    if m.sticker:
        mime = m.file.mime_type or ""
        if mime == "image/webp":
            return words, [part("image/webp", await m.download_media(file=bytes))]
        if mime in ("video/webm", "video/mp4") and (m.file.size or 0) <= INLINE_LIMIT:
            return words, [part(mime, await m.download_media(file=bytes))]
        thumb = await m.download_media(file=bytes, thumb=-1)  # animated (Lottie) stickers: the preview frame
        return words, [part(sniff(thumb), thumb)] if thumb else []
    if m.photo:
        return words, [part("image/jpeg", await m.download_media(file=bytes))]
    if m.gif or m.video or m.video_note:
        seconds = int(m.file.duration or 0)
        if (m.file.size or 0) > limit:
            thumb = await m.download_media(file=bytes, thumb=-1)
            return words + " (слишком большое, вижу только кадр)", [part(sniff(thumb), thumb)] if thumb else []
        data = await m.download_media(file=bytes)
        mime = m.file.mime_type or "video/mp4"
        if len(data) > INLINE_LIMIT or seconds > MAX_SECONDS:
            try:
                data, mime = await shrink(data), "video/mp4"
            except Exception:
                log.exception("video shrink failed")
                thumb = await m.download_media(file=bytes, thumb=-1)
                return words + " (не смог сжать, вижу только кадр)", [part(sniff(thumb), thumb)] if thumb else []
        return words, [part(mime, data)]
    if words:  # a picture sent as a file
        if (m.file.size or 0) > INLINE_LIMIT:
            return words + " (слишком большое)", []
        return words, [part(m.file.mime_type, await m.download_media(file=bytes))]
    return "", []
