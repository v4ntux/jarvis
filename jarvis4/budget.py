"""Money: what a call costs, how much is left today, and which model may answer now (the ladder).

The owner's limit is $2 a day. Haiku (the mouth) does the talking and simple tasks, Sonnet (the brain) takes new
tasks and the screen, Opus only when asked to think hard. Past the soft limit only Haiku works, past the daily
limit only the owner's small reserve is left, and strangers never get the reserve.
"""
from __future__ import annotations

import enum

from . import clock
from .config import CACHE_READ, CACHE_WRITE, Config
from .store import Store


class Tier(enum.IntEnum):
    MOUTH = 0
    BRAIN = 1
    DEEP = 2


class OutOfMoney(Exception):
    def __init__(self, text: str):
        super().__init__(text)
        self.text = text


def cost_of(cfg: Config, model: str, tokens_in: int, cache_write: int, cache_read: int, tokens_out: int) -> float:
    price_in, price_out = cfg.price(model)
    weighted = tokens_in + cache_write * CACHE_WRITE + cache_read * CACHE_READ
    return (weighted * price_in + tokens_out * price_out) / 1e6


class Budget:
    def __init__(self, cfg: Config, store: Store):
        self.cfg, self.store = cfg, store

    def spent_today(self) -> float:
        return self.store.spent_since(clock.day_start())

    def spent_others_today(self) -> float:
        return self.store.spent_since(clock.day_start(), kind="others")

    def left_today(self) -> float:
        return max(0.0, self.cfg.daily_limit - self.spent_today())

    def model_for(self, tier: Tier) -> str:
        return {Tier.MOUTH: self.cfg.mouth_model, Tier.BRAIN: self.cfg.brain_model, Tier.DEEP: self.cfg.deep_model}[tier]

    def pick(self, want: Tier, owner: bool = True, think: bool = False) -> str:
        """The model that may answer now, or OutOfMoney. `think`: the owner asked to think hard."""
        day, cfg = self.spent_today(), self.cfg
        if not owner:
            if day >= cfg.daily_limit or self.spent_others_today() >= cfg.others_daily:
                raise OutOfMoney("на сегодня лимит чужих чатов исчерпан")
            return self.model_for(Tier.MOUTH)
        if day >= cfg.daily_limit + cfg.owner_reserve:
            raise OutOfMoney(f"дневной лимит ${cfg.daily_limit:.2f} кончился, резерв тоже. Обнулится в полночь по Москве")
        if day >= cfg.daily_limit:
            return self.model_for(Tier.MOUTH)  # the reserve: the owner can still talk
        if want >= Tier.DEEP and not think:
            want = Tier.BRAIN
        if day >= cfg.soft_limit and not (think and want >= Tier.BRAIN):
            want = Tier.MOUTH
        return self.model_for(want)

    def task_room(self, spent: float) -> bool:
        return spent < self.cfg.task_limit

    def summary(self) -> str:
        day = clock.day_start()
        lines = [f"Сегодня потрачено ${self.spent_today():.3f} из ${self.cfg.daily_limit:.2f}."]
        for kind, cost, calls in self.store.spent_by_kind(day):
            lines.append(f"  {kind}: ${cost:.3f} ({calls})")
        return "\n".join(lines)
