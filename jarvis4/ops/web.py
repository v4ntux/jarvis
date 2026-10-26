"""Free internet access from the server: search and page reading, no paid API."""
import asyncio

import httpx
import trafilatura
from ddgs import DDGS

MAX_PAGE_CHARS = 12000
UA = "JarvisPersonalAssistant/1.0 (+https://t.me/jarvis_for_vun_bot) httpx"  # Wikipedia rejects fake browser UAs


def _search(query: str, limit: int, news: bool) -> str:
    with DDGS() as ddgs:
        hits = (ddgs.news if news else ddgs.text)(query, max_results=limit, region="ru-ru")
    if not hits:
        return "Ничего не найдено."
    lines = []
    for i, h in enumerate(hits, 1):
        date = f" ({h['date'][:10]})" if h.get("date") else ""
        lines.append(f"{i}. {h.get('title', '')}{date}\n{h.get('href') or h.get('url', '')}\n{h.get('body', '')[:300]}")
    return "\n\n".join(lines)


async def search(query: str, limit: int = 6, news: bool = False) -> str:
    try:
        return await asyncio.to_thread(_search, query, max(1, min(limit, 10)), news)
    except Exception as e:
        return f"Ошибка поиска: {type(e).__name__}: {e}"


async def fetch(url: str) -> str:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20, headers={"User-Agent": UA}) as c:
            r = await c.get(url)
        r.raise_for_status()
    except Exception as e:
        return f"Ошибка загрузки: {type(e).__name__}: {e}"
    if "html" not in r.headers.get("content-type", "html"):
        return r.text[:MAX_PAGE_CHARS]
    text = await asyncio.to_thread(trafilatura.extract, r.text, include_links=False, include_tables=True)
    if not text:
        return "Не удалось вытащить текст со страницы."
    return text[:MAX_PAGE_CHARS] + ("\n…(обрезано)" if len(text) > MAX_PAGE_CHARS else "")
