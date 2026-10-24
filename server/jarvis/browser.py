"""Jarvis's own browser on the server: headless Chromium, works while the laptop is off.

Pages come back as text plus numbered links, buttons and fields, so the brain acts on
them without pictures. Cookies stay in a profile, so sites remember a login the owner
made once. Password fields are never filled by Jarvis. Downloads land in the work dir.
"""
import asyncio
import logging
import os
import time
from urllib.parse import unquote

from . import shell

log = logging.getLogger("jarvis.browser")

PROFILE = "/opt/jarvis/data/browser"
TEXT_CHARS = 2500   # with the element list this stays under the brain's 5000-char tool limit
READ_CHARS = 4800
MAX_ELEMENTS = 60
IDLE_CLOSE = 600    # Chromium takes ~400 MB of RAM; close it when nobody browses

COLLECT = """(max) => {
  const sel = 'a[href], button, input, textarea, select, summary, [role=button], [role=link], [role=tab],' +
              '[role=menuitem], [role=checkbox], [role=option], [contenteditable=true]';
  document.querySelectorAll('[data-jarvis-id]').forEach(e => e.removeAttribute('data-jarvis-id'));
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.top > innerHeight * 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || el.disabled) continue;
    const tag = el.tagName.toLowerCase(), type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const id = 'b' + (out.length + 1);
    el.setAttribute('data-jarvis-id', id);
    const label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                   el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '')
                  .trim().replace(/\\s+/g, ' ').slice(0, 70);
    const field = tag === 'input' || tag === 'textarea' || tag === 'select' || el.isContentEditable;
    out.push({id, tag, type, label, field, password: type === 'password',
              value: field && type !== 'password' ? String(el.value || '').slice(0, 50) : '',
              href: tag === 'a' ? el.href.slice(0, 100) : ''});
    if (out.length >= max) break;
  }
  return out;
}"""


class BrowserError(RuntimeError):
    pass


class Browser:
    def __init__(self):
        self.pw = None
        self.context = None
        self.page = None
        self.lock = asyncio.Lock()
        self.last_use = 0.0
        self.downloads: list[str] = []
        self._closer: asyncio.Task | None = None

    async def _ensure(self):
        if self.context:
            return
        from playwright.async_api import async_playwright

        os.makedirs(PROFILE, exist_ok=True)
        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            PROFILE, headless=True, locale="ru-RU", timezone_id="Europe/Moscow",
            viewport={"width": 1280, "height": 900}, accept_downloads=True)
        self.context.on("page", self._new_tab)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        self._watch(self.page)
        if not self._closer:
            self._closer = asyncio.create_task(self._close_when_idle())

    def _new_tab(self, page):
        # A link that opens a tab: follow it, the way a person would look at the new tab
        self.page = page
        self._watch(page)

    def _watch(self, page):
        page.on("download", lambda d: asyncio.create_task(self._save(d)))

    async def _save(self, download):
        os.makedirs(shell.WORK, exist_ok=True)
        path = os.path.join(shell.WORK, os.path.basename(download.suggested_filename) or "download")
        await download.save_as(path)
        self.downloads.append(path)

    async def _close_when_idle(self):
        while True:
            await asyncio.sleep(60)
            if self.context and time.monotonic() - self.last_use > IDLE_CLOSE:
                async with self.lock:
                    await self.close()

    async def close(self):
        if self.context:
            try:
                await self.context.close()
                await self.pw.stop()
            except Exception:
                log.exception("browser close failed")
        self.context = self.page = self.pw = None

    async def _settle(self, navigating: bool = False):
        if navigating:
            # A click or Enter starts loading a moment later; a snapshot taken now shows the old page
            await asyncio.sleep(0.6)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
        try:
            await self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass  # busy pages never go idle; what has loaded is enough

    async def _snapshot(self, note: str = "") -> str:
        elements = await self.page.evaluate(COLLECT, MAX_ELEMENTS)
        text = (await self.page.evaluate("document.body ? document.body.innerText : ''")) or ""
        text = " ".join(text.split())
        lines = [f"Страница: {await self.page.title()}", unquote(self.page.url)]
        if note:
            lines.append(note)
        lines.append("Текст: " + text[:TEXT_CHARS] + ("…" if len(text) > TEXT_CHARS else ""))
        lines.append("Элементы:")
        for e in elements:
            kind = "поле пароля" if e["password"] else ("поле" if e["field"] else ("ссылка" if e["tag"] == "a" else "кнопка"))
            line = f"{e['id']} {kind} «{e['label']}»"
            if e["value"]:
                line += f" = «{e['value']}»"
            lines.append(line)
        if self.downloads:
            lines.append("Скачано в " + ", ".join(self.downloads))
            self.downloads = []
        return "\n".join(lines)

    async def open(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        async with self.lock:
            self.last_use = time.monotonic()
            await self._ensure()
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                raise BrowserError(f"страница не открылась: {type(e).__name__}: {str(e)[:200]}") from e
            await self._settle()
            return await self._snapshot()

    async def act(self, action: str, element: str = "", text: str = "") -> str:
        async with self.lock:
            self.last_use = time.monotonic()
            if not self.context:
                raise BrowserError("браузер закрыт, сначала browse")
            page = self.page
            target = page.locator(f'[data-jarvis-id="{element}"]') if element else None
            try:
                if action == "read":
                    body = " ".join(((await page.evaluate("document.body.innerText")) or "").split())
                    return f"{await page.title()}\n{unquote(page.url)}\n{body[:READ_CHARS]}"
                if action == "click":
                    await (await self._need(target, element)).click(timeout=10000)
                elif action == "type":
                    field = await self._need(target, element)
                    if await field.get_attribute("type") == "password":
                        raise BrowserError("в поля паролей я не пишу: войди на сайт сам, вход сохранится")
                    await field.fill(text, timeout=10000)
                elif action == "press":
                    await page.keyboard.press(text or "Enter")
                elif action == "scroll":
                    await page.mouse.wheel(0, 800 if text != "up" else -800)
                elif action == "back":
                    await page.go_back(timeout=15000)
                else:
                    raise BrowserError(f"неизвестное действие {action}")
            except BrowserError:
                raise
            except Exception as e:
                raise BrowserError(f"{action} не получилось: {type(e).__name__}: {str(e)[:200]}") from e
            await self._settle(navigating=action in ("click", "press", "back"))
            return await self._snapshot()

    @staticmethod
    async def _need(target, element):
        if target is None:
            raise BrowserError("нужен id элемента из последнего снимка страницы")
        if not await target.count():
            raise BrowserError(f"элемента {element} уже нет на странице, сделай browser_action read или browse заново")
        return target

    async def screenshot(self) -> bytes:
        async with self.lock:
            self.last_use = time.monotonic()
            if not self.context:
                raise BrowserError("браузер закрыт, сначала browse")
            return await self.page.screenshot(type="png")
