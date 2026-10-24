import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    owner_id: int
    bot_token: str
    anthropic_key: str
    model: str
    db_path: str
    history_limit: int
    # USD per million tokens, used only for the spend counter
    price_in: float
    price_out: float


def load() -> Config:
    return Config(
        owner_id=int(os.environ["OWNER_TG_ID"]),
        bot_token=os.environ["TG_BOT_TOKEN"],
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("JARVIS_MODEL", "claude-haiku-5-5"),
        db_path=os.environ.get("JARVIS_DB", "/opt/jarvis/data/jarvis.db"),
        history_limit=int(os.environ.get("JARVIS_HISTORY", "20")),
        price_in=float(os.environ.get("PRICE_IN", "1")),
        price_out=float(os.environ.get("PRICE_OUT", "5")),
    )
