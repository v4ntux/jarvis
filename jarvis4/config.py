"""Settings: money limits, models, prices, and where things live. Everything has a default, the environment overrides."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

HAIKU, SONNET, OPUS = "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"

# USD per million tokens (input, output). Haiku 5.5 doubles for prompts over 100K, which Jarvis never sends.
PRICES = {HAIKU: (0.10, 0.50), SONNET: (2.0, 10.0), OPUS: (4.0, 20.0)}
CACHE_WRITE, CACHE_READ = 1.25, 0.1  # of the input price


@dataclass(frozen=True)
class Config:
    owner_id: int = 0
    owner_name: str = "Ван"
    db_path: str = "/opt/jarvis/data/jarvis4.db"
    data_dir: str = "/opt/jarvis/data"
    anthropic_key: str = ""

    # The ladder: who answers what (see budget.py)
    mouth_model: str = HAIKU  # chat, simple tasks, the checker
    brain_model: str = SONNET  # new tasks, the screen
    deep_model: str = OPUS  # only for «подумай» or when the brain is stuck
    prices: dict = field(default_factory=lambda: dict(PRICES))

    # Money (USD per day on the owner's clock, and per task)
    daily_limit: float = 2.0  # nothing paid beyond this, except the owner's reserve
    soft_limit: float = 1.5  # beyond this only Haiku, unless the owner says «подумай»
    owner_reserve: float = 0.2  # Haiku keeps answering the owner past the daily limit, up to this much more
    task_limit: float = 0.4
    others_daily: float = 0.4  # strangers and groups together, of the same day

    # Behaviour
    max_steps: int = 40
    history: int = 30  # messages of a chat the model sees
    journal_lines: int = 12  # recent actions listed in the prompt
    burst_quiet: float = 0.7  # the owner's messages that come this close together are one request
    burst_max: float = 4.0
    freeze_seconds: float = 600  # after «стоп» watchers and timers do not act for this long

    # Where the world is
    hands_host: str = "100.115.94.31"
    hands_port: int = 8765
    hands_token: str = ""
    tg_api_id: int = 0
    tg_api_hash: str = ""
    tg_session: str = "/opt/jarvis/data/tg_user"
    gemini_key: str = ""
    gemini_models: str = "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"
    live_models: str = "gemini-3.8-live,gemini-3.1-flash-live-preview"
    live_voice: str = "Charon"
    call_minutes: int = 10
    owner_phone: str = ""
    smsc_login: str = ""
    smsc_password: str = ""
    smsc_api_key: str = ""
    phone_daily_limit: int = 10

    def price(self, model: str) -> tuple[float, float]:
        return self.prices.get(model, self.prices[SONNET])

    def with_(self, **changes) -> "Config":
        return replace(self, **changes)


def load(env=os.environ) -> Config:
    def num(key: str, default):
        return type(default)(env[key]) if env.get(key) else default

    base = Config()
    return Config(
        owner_id=int(env.get("OWNER_TG_ID") or 0),
        owner_name=env.get("OWNER_NAME") or base.owner_name,
        db_path=env.get("JARVIS4_DB") or base.db_path,
        data_dir=env.get("JARVIS_DATA") or base.data_dir,
        anthropic_key=env.get("ANTHROPIC_API_KEY", ""),
        mouth_model=env.get("JARVIS_MOUTH_MODEL") or base.mouth_model,
        brain_model=env.get("JARVIS_BRAIN_MODEL") or base.brain_model,
        deep_model=env.get("JARVIS_DEEP_MODEL") or base.deep_model,
        daily_limit=num("DAILY_LIMIT", base.daily_limit),
        soft_limit=num("SOFT_LIMIT", base.soft_limit),
        owner_reserve=num("OWNER_RESERVE", base.owner_reserve),
        task_limit=num("TASK_LIMIT", base.task_limit),
        others_daily=num("OTHERS_DAILY", base.others_daily),
        hands_host=env.get("HANDS_HOST") or base.hands_host,
        hands_port=num("HANDS_PORT", base.hands_port),
        hands_token=env.get("HANDS_TOKEN", ""),
        tg_api_id=int(env.get("TG_API_ID") or 0),
        tg_api_hash=env.get("TG_API_HASH", ""),
        tg_session=env.get("TG_SESSION") or base.tg_session,
        gemini_key=env.get("GEMINI_API_KEY", ""),
        gemini_models=env.get("GEMINI_MODEL") or base.gemini_models,
        live_models=env.get("GEMINI_LIVE_MODEL") or base.live_models,
        live_voice=env.get("GEMINI_VOICE") or base.live_voice,
        call_minutes=num("CALL_MINUTES", base.call_minutes),
        owner_phone=env.get("OWNER_PHONE", ""),
        smsc_login=env.get("SMSC_LOGIN", ""),
        smsc_password=env.get("SMSC_PASSWORD", ""),
        smsc_api_key=env.get("SMSC_API_KEY", ""),
        phone_daily_limit=num("PHONE_DAILY_LIMIT", base.phone_daily_limit),
    )
