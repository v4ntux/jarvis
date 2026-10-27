"""Downloading videos, pictures and sound from TikTok, Instagram, Pinterest, YouTube and the rest, for anybody who asks.

yt-dlp does the work on the server (it knows a thousand sites); a pin or a post that is only a picture falls back to the
page's og:image. Files go to a fresh folder under work/media and are deleted once sent. No model is needed: a bare link
in a chat is downloaded straight away, and the tool does the same when somebody asks in words.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import sys
import time
import uuid

log = logging.getLogger("jarvis4.grab")

WORK = "/opt/jarvis/data/work/media"
COOKIES = "/opt/jarvis/data/cookies.txt"  # optional: Instagram and private posts need a logged-in browser's cookies
TIMEOUT = 240
OWNER_MB = 1000
OTHERS_MB = 50
OTHERS_DAILY = 15  # downloads a day in one chat that is not the owner's
SITES = re.compile(
    r"https?://(?:[\w-]+\.)*(?:tiktok\.com|instagram\.com|instagr\.am|pinterest\.[a-z.]+|pin\.it|youtube\.com|youtu\.be|"
    r"twitter\.com|x\.com|vk\.com|vkvideo\.ru|reddit\.com|redd\.it|facebook\.com|fb\.watch|likee\.video|"
    r"twitch\.tv|soundcloud\.com|vimeo\.com|dzen\.ru|rutube\.ru|threads\.net|snapchat\.com)/\S+", re.I)
OG_IMAGE = re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I)
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".mp3", ".m4a", ".ogg", ".opus", ".jpg", ".jpeg", ".png", ".webp",
             ".gif"}


class GrabError(Exception):
    """Why nothing came, in words for a person."""


def link_in(text: str) -> str:
    """The first link to a site yt-dlp knows, or ''."""
    m = SITES.search(text or "")
    return m.group(0).rstrip(").,!?»\"'") if m else ""


def bare_link(text: str) -> str:
    """A message that is nothing but such a link (maybe with a word or two): people send those to be downloaded."""
    url = link_in(text)
    if not url:
        return ""
    rest = re.sub(r"[\W_]+", " ", (text or "").replace(url, " ")).split()
    return url if len(rest) <= 2 else ""


QUALITY = ("res:720,ext:mp4:m4a", "res:360,ext:mp4:m4a")  # a phone plays mp4; 360p when 720p is over the limit
PIECE = re.compile(r"\.f\d+\.\w+$")  # one stream of a video that was never merged


def _args(url: str, folder: str, max_mb: int, audio: bool, quality: str = QUALITY[0]) -> list[str]:
    args = [sys.executable, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--restrict-filenames", "--no-progress",
            "--max-filesize", f"{max_mb}M", "-o", os.path.join(folder, "%(title).60B.%(ext)s"),
            "--print", "after_move:%(title).120s"]
    if audio:
        args += ["-x", "--audio-format", "mp3"]
    else:
        args += ["-S", quality, "--merge-output-format", "mp4"]
    if os.path.isfile(COOKIES):
        args += ["--cookies", COOKIES]
    return args + [url]


async def _yt_dlp(url: str, folder: str, max_mb: int, audio: bool, quality: str = QUALITY[0]) -> tuple[str, str]:
    """(title, error); an empty error means files are in the folder."""
    proc = await asyncio.create_subprocess_exec(*_args(url, folder, max_mb, audio, quality),
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return "", "слишком долго качается"
    title = out.decode(errors="replace").strip().splitlines()[-1:] or [""]
    if proc.returncode != 0:
        lines = [x for x in err.decode(errors="replace").splitlines() if "ERROR" in x] or ["yt-dlp не смог"]
        return "", lines[-1].replace("ERROR:", "").strip()[:300]
    return title[0], ""


async def _og_image(url: str, folder: str) -> str:
    """A post that is a picture: the page's og:image, saved in the folder. '' when there is none."""
    import httpx

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
        page = await client.get(url)
        found = OG_IMAGE.search(page.text)
        if not found:
            return ""
        pic = await client.get(html.unescape(found.group(1)))
        if pic.status_code != 200 or not pic.headers.get("content-type", "").startswith("image/"):
            return ""
        ext = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(pic.headers["content-type"], ".jpg")
        with open(os.path.join(folder, "picture" + ext), "wb") as f:
            f.write(pic.content)
        named = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', page.text, re.I)
        return html.unescape(named.group(1))[:120] if named else "картинка"


async def fetch(url: str, max_mb: int = OWNER_MB, audio: bool = False) -> tuple[str, list[str], str]:
    """(folder, files, title). The caller removes the folder (cleanup()) once the files are sent."""
    if not url.startswith(("http://", "https://")):
        raise GrabError("это не ссылка")
    folder = os.path.join(WORK, f"{int(time.time())}-{uuid.uuid4().hex[:6]}")
    os.makedirs(folder, exist_ok=True)
    title, error = await _yt_dlp(url, folder, max_mb, audio)
    files = _files(folder)
    if not files and not audio and _too_big(folder):  # yt-dlp drops a stream over the limit without an error
        _empty(folder)
        title, error = await _yt_dlp(url, folder, max_mb, audio, QUALITY[1])
        files = _files(folder)
    if not files and _too_big(folder):
        error = "File is larger than the limit"
    if not files and not audio:
        try:
            title = await _og_image(url, folder)
        except Exception as e:
            log.info("og:image fallback failed for %s: %s", url, e)
        files = _files(folder)
    if not files:
        cleanup(folder)
        if "File is larger" in error or "max-filesize" in error:
            raise GrabError(f"файл больше {max_mb} МБ")
        if "login" in error.lower() or "cookies" in error.lower() or "private" in error.lower():
            raise GrabError("сайт пускает только залогиненных (Instagram любит так): нужен cookies.txt на сервере")
        raise GrabError(error or "ничего не скачалось")
    return folder, files, title


def _files(folder: str) -> list[str]:
    found = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
             if os.path.splitext(n)[1].lower() in MEDIA_EXT and not PIECE.search(n)]
    return [f for f in found if os.path.getsize(f) > 0][:10]


def _too_big(folder: str) -> bool:
    return any(n.endswith(".part") for n in os.listdir(folder))


def _empty(folder: str) -> None:
    for name in os.listdir(folder):
        os.unlink(os.path.join(folder, name))


def cleanup(folder: str) -> None:
    shutil.rmtree(folder, ignore_errors=True)


def allowed_today(store, chat: str, owner: bool) -> bool:
    """Counts a download for a chat that is not the owner's; False once the day's share is used up."""
    if owner:
        return True
    day = time.strftime("%Y-%m-%d")
    key = f"grab:{chat}"
    got_day, _, n = store.meta_get(key).partition(" ")
    n = int(n) if got_day == day and n.isdigit() else 0
    if n >= OTHERS_DAILY:
        return False
    store.meta_set(key, f"{day} {n + 1}")
    return True


async def deliver(tg, store, chat: str, url: str, owner: bool, reply_ref: str = "", audio: bool = False) -> str:
    """Downloads and sends into the chat; the words for the journal. Raises GrabError when it did not work."""
    if not allowed_today(store, chat, owner):
        raise GrabError(f"на сегодня хватит: {OTHERS_DAILY} загрузок в этом чате")
    folder, files, title = await fetch(url, OWNER_MB if owner else OTHERS_MB, audio)
    try:
        items = []
        for path in files:
            with open(path, "rb") as f:
                items.append((os.path.basename(path), f.read()))
        ref = await tg.send(chat, "", items, reply_ref)
    finally:
        cleanup(folder)
    store.add_msg(ref, chat, "jarvis", "", {"kind": "video" if not audio else "voice", "about": title}, reply_ref=reply_ref)
    size = sum(len(d) for _, d in items) / 1024 / 1024
    return f"Скачал и отправил: {title or 'файл'} ({len(items)} шт., {size:.1f} МБ)."
