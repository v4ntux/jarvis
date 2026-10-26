"""Money: what a call costs, how much is left today, and which model may answer now (the ladder).

The owner's limit is $2 a day. Haiku (the mouth) does the talking and simple tasks, Sonnet (the brain) takes new
tasks and the screen, Opus only when asked to think hard. Past the soft limit only Haiku works, past the daily
limit only the owner's small reserve is left, and strangers never get the reserve.
"""
from __future__ import annotations

import enum
import time

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

    # The owner can move today's line himself (/limit): reset the count from now on, or set today's limit. Both end at
    # midnight on his clock, and tomorrow the limit from the settings is back.

    def _floor(self) -> float:
        try:
            return max(clock.day_start(), float(self.store.meta_get("spend_floor", "0") or 0))
        except ValueError:
            return clock.day_start()

    def reset_count(self) -> float:
        """Count from zero again from this moment; returns what had been counted today."""
        had = self.spent_today()
        self.store.meta_set("spend_floor", repr(time.time()))
        return had

    def set_today(self, amount: float) -> None:
        self.store.meta_set("limit_today", f"{clock.now():%Y-%m-%d}:{amount}")

    def today_override(self) -> float | None:
        day, _, amount = self.store.meta_get("limit_today", "").partition(":")
        try:
            return float(amount) if day == f"{clock.now():%Y-%m-%d}" else None
        except ValueError:
            return None

    @property
    def daily(self) -> float:
        override = self.today_override()
        return override if override is not None else self.cfg.daily_limit

    @property
    def soft(self) -> float:
        override = self.today_override()
        return override * 0.75 if override is not None else self.cfg.soft_limit

    def spent_today(self) -> float:
        return self.store.spent_since(self._floor())

    def spent_others_today(self) -> float:
        return self.store.spent_since(self._floor(), kind="others")

    def left_today(self) -> float:
        return max(0.0, self.daily - self.spent_today())

    def model_for(self, tier: Tier) -> str:
        return {Tier.MOUTH: self.cfg.mouth_model, Tier.BRAIN: self.cfg.brain_model, Tier.DEEP: self.cfg.deep_model}[tier]

    def pick(self, want: Tier, owner: bool = True, think: bool = False) -> str:
        """The model that may answer now, or OutOfMoney. `think`: the owner asked to think hard."""
        day, cfg, daily = self.spent_today(), self.cfg, self.daily
        if not owner:
            if day >= daily or self.spent_others_today() >= cfg.others_daily:
                raise OutOfMoney("на сегодня лимит чужих чатов исчерпан")
            return self.model_for(Tier.MOUTH)
        if day >= daily + cfg.owner_reserve:
            raise OutOfMoney(f"дневной лимит ${daily:.2f} кончился, резерв тоже. Обнулится в полночь по Москве. "
                             "Не ждать: /limit reset обнулит счёт на сегодня, /limit 5 поставит лимит $5")
        if day >= daily:
            return self.model_for(Tier.MOUTH)  # the reserve: the owner can still talk
        if want >= Tier.DEEP and not think:
            want = Tier.BRAIN
        if day >= self.soft and not (think and want >= Tier.BRAIN):
            want = Tier.MOUTH
        return self.model_for(want)

    def task_room(self, spent: float) -> bool:
        return spent < self.cfg.task_limit

    def summary(self) -> str:
        lines = [f"Сегодня потрачено ${self.spent_today():.3f} из ${self.daily:.2f}."]
        for kind, cost, calls in self.store.spent_by_kind(self._floor()):
            lines.append(f"  {kind}: ${cost:.3f} ({calls})")
        return "\n".join(lines)
