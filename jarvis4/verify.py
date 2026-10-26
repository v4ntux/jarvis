"""The last check before «готово»: is every claim in the answer backed by what really happened?

A cheap model reads the request, the journal of the task and the answer. If the answer says something was done
that the journal does not show, or hides a failure, it gets rewritten from the facts. The check runs only when
the task did something (or the answer claims it did): small talk costs nothing extra.
"""
from __future__ import annotations

import json
import logging

from . import persona
from .types import Interaction

log = logging.getLogger("jarvis4.verify")

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}, "fixed": {"type": "string"}, "why": {"type": "string"}},
    "required": ["ok", "fixed", "why"],
    "additionalProperties": False,
}

SYSTEM = """Ты проверяешь ответ ИИ-помощника Джарвиса перед отправкой Вану. Тебе дают просьбу, журнал того, что Джарвис \
реально сделал (инструмент, аргументы, ok/ошибка, результат), и его ответ.
Для вопросов о прошлом («что ты делал», «что писал») дан и журнал прошлых действий: ответ может опираться на него.
Проверь: всё, что ответ говорит о сделанном (отправил, переслал, позвонил, открыл, закрыл, вышел, проверил, цифры и \
факты), подтверждено журналом? Ответ не скрывает ошибку? Не выдаёт неудавшееся за удавшееся? Не заявляет, что дело сделано, \
если оно сделано не так, как просили (например, написал вместо звонка, открыл терминал вместо приложения)? Причины, \
оценки и выводы («это боты, норма»), которых нет в журнале, тоже неподтверждённое: их надо убрать.
Мелкие неточности формулировок, которым журнал не противоречит, — это ok=true: переписывай, только когда есть реальное расхождение (заявлено дело, которого не было; скрыта ошибка; выдумана цифра или причина). Если всё честно — ok=true, fixed="". Иначе ok=false, а fixed — тот же ответ, переписанный по фактам: коротко, тем же \
тоном и на «ты», строчными как в чате, без Markdown; убери неподтверждённое, прямо скажи, что не вышло и что пробовал. \
Не добавляй новых дел и фактов, fixed не длиннее исходного ответа. why — одна фраза, что не так."""


def journal_text(rows: list[dict]) -> str:
    if not rows:
        return "(инструменты не вызывались)"
    lines = []
    for r in rows:
        args = ", ".join(f"{k}={str(v)[:200]}" for k, v in r["args"].items())
        lines.append(f"- {r['tool']}({args[:300]}) → {'ОК' if r['ok'] else 'ОШИБКА'}: {r['result'][:200].replace(chr(10), ' ')}"
                     + (f" [доказательство: {r['evidence'][:150]}]" if r["evidence"] else ""))
    return "\n".join(lines)


def needed(answer: str, rows: list[dict], reads: set[str]) -> bool:
    """Anything beyond looking was done, or the answer claims it was."""
    did = [r for r in rows if r["tool"] not in reads]
    return bool(did) or (bool(persona.CLAIM.search(answer)) and not rows) or any(not r["ok"] for r in rows)


async def check(core, itx: Interaction, request: str, answer: str) -> str:
    rows = core.store.journal(30, task=itx.id)
    if not answer or not needed(answer, rows, core.READ_TOOLS):
        return answer
    earlier = [r for r in core.store.journal(12) if r["task"] != itx.id]
    prompt = (f"Просьба Вана: {request[:600]}\n\nЖурнал этой задачи:\n{journal_text(rows)}\n\n"
              f"Журнал прошлых действий (контекст, не этой задачи):\n{journal_text(earlier)}\n\n"
              f"Ответ Джарвиса:\n{answer}")
    try:
        reply = await core.llm.complete(core.budget.model_for(core.TIER_CHECK), SYSTEM,
                                        [{"role": "user", "content": prompt}], max_tokens=1500, effort="low",
                                        kind="verify", schema=SCHEMA)
        itx.spent += reply.cost
        verdict = json.loads(reply.text)
    except Exception as e:  # a broken checker must not silence the answer
        log.warning("verify failed: %s", e)
        return answer
    fixed = verdict.get("fixed", "").strip()
    if verdict.get("ok") or not fixed:
        return answer
    if len(fixed) > len(answer) * 1.3 + 40:  # a rewrite that grows is the checker inventing: keep what was said
        log.info("rewrite dropped, it is longer than the answer: %s", fixed[:100])
        return answer
    log.info("answer rewritten: %s", verdict.get("why"))
    return fixed
