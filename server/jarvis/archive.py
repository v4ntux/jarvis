"""The chat archive: messages older than the window are folded into one rolling summary.

The window stays small and cheap, yet nothing the owner said is forgotten: names, agreements, unfinished
business and preferences live on in the summary until /reset.
"""
import logging

from .gemini import Gemini
from .memory import Memory

log = logging.getLogger("jarvis.archive")

COMPACT_AFTER = 30  # messages that fell out of the window before they are worth folding in
BATCH = 120
MAX_LINE = 500
SUMMARY_CHARS = 2200

ASK = """Ты ведёшь краткое содержание переписки Вана с его ассистентом Джарвисом. Ниже прежнее содержание и новые
сообщения. Обнови содержание: что обсуждали, что решили и пообещали, незаконченные дела, имена и предпочтения, на
чём остановились. Только суть, без воды, до {chars} символов, списком коротких строк. Ничего не выдумывай.

Прежнее содержание:
{summary}

Новые сообщения:
{messages}"""


async def compact(memory: Memory, gemini: Gemini, window: int) -> bool:
    """Folds the messages outside the last `window` into the summary; True when it did."""
    upto, summary = memory.summary()
    fresh = memory.unsummarized(memory.window_start(window), BATCH)
    if len(fresh) < COMPACT_AFTER:
        return False
    lines = "\n".join(f"{'Ван' if role == 'user' else 'Джарвис'}: {text[:MAX_LINE]}" for _, role, text in fresh)
    ask = ASK.format(chars=SUMMARY_CHARS, summary=summary or "(пока пусто)", messages=lines)
    text = await gemini.text("Ты аккуратный секретарь.", [{"role": "user", "parts": [{"text": ask}]}], 1500,
                             models=gemini.lite_first)
    memory.set_summary(fresh[-1][0], text[:SUMMARY_CHARS + 300])
    log.info("folded %d messages into the summary (%d chars)", len(fresh), len(text))
    return True
