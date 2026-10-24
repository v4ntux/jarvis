import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# The owner lives on Moscow time: reminders, schedules and the daily limit follow it
TZ = ZoneInfo("Europe/Moscow")


def day_start() -> float:
    """Last Moscow midnight: daily limits reset here."""
    return dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True)
class Config:
    owner_id: int
    bot_token: str
    anthropic_key: str
    model: str
    smart_model: str
    db_path: str
    history_limit: int
    # USD per million tokens (input, output) for (model, smart_model); only for the spend counter
    prices: tuple[tuple[float, float], tuple[float, float]]
    # USD caps: one request, and everything since Moscow midnight
    task_limit: float
    daily_limit: float
    hands_token: str
    hands_host: str
    hands_port: int
    # SMS and robot calls through SMSC.ru; empty means the phone channel is off
    owner_phone: str
    smsc_login: str
    smsc_password: str
    smsc_api_key: str
    smsc_voice: str
    phone_daily_limit: int


def load() -> Config:
    return Config(
        owner_id=int(os.environ["OWNER_TG_ID"]),
        bot_token=os.environ["TG_BOT_TOKEN"],
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("JARVIS_MODEL", "claude-haiku-5-5"),
        smart_model=os.environ.get("JARVIS_SMART_MODEL", "claude-sonnet-5-5"),
        db_path=os.environ.get("JARVIS_DB", "/opt/jarvis/data/jarvis.db"),
        history_limit=int(os.environ.get("JARVIS_HISTORY", "10")),
        prices=(
            (float(os.environ.get("PRICE_IN", "1")), float(os.environ.get("PRICE_OUT", "5"))),
            (float(os.environ.get("SMART_PRICE_IN", "3")), float(os.environ.get("SMART_PRICE_OUT", "15"))),
        ),
        task_limit=float(os.environ.get("TASK_LIMIT", "0.30")),
        daily_limit=float(os.environ.get("DAILY_LIMIT", "2")),
        hands_token=os.environ["HANDS_TOKEN"],
        # Tailscale address only: the hub is never exposed to the internet
        hands_host=os.environ.get("HANDS_HOST", "100.115.94.31"),
        hands_port=int(os.environ.get("HANDS_PORT", "8765")),
        owner_phone=os.environ.get("OWNER_PHONE", "").strip().lstrip("+"),
        smsc_login=os.environ.get("SMSC_LOGIN", ""),
        smsc_password=os.environ.get("SMSC_PASSWORD", ""),
        smsc_api_key=os.environ.get("SMSC_API_KEY", ""),
        smsc_voice=os.environ.get("SMSC_VOICE", "m"),
        phone_daily_limit=int(os.environ.get("PHONE_DAILY_LIMIT", "10")),
    )
