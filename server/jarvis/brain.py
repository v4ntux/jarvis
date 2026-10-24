import anthropic

from .config import Config
from .memory import Memory

SYSTEM = (
    "Ты Джарвис, личный помощник. Отвечай по-русски, коротко и по делу. "
    "Пользователь пишет транслитом, это нормально. "
    "Пока у тебя нет доступа к ноутбуку: если просят что-то сделать на компьютере, "
    "честно скажи, что руки ещё не подключены."
)


class Brain:
    def __init__(self, cfg: Config, memory: Memory):
        self.cfg = cfg
        self.memory = memory
        self.client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_key)

    async def reply(self, text: str) -> str:
        self.memory.add("user", text)
        resp = await self.client.messages.create(
            model=self.cfg.model,
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=self.memory.recent(self.cfg.history_limit),
        )
        answer = "".join(b.text for b in resp.content if b.type == "text").strip() or "…"
        self.memory.add("assistant", answer)
        u = resp.usage
        cost = (u.input_tokens * self.cfg.price_in + u.output_tokens * self.cfg.price_out) / 1e6
        self.memory.log_usage(self.cfg.model, u.input_tokens, u.output_tokens, cost)
        return answer
