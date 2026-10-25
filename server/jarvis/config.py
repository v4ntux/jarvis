import datetime as dt
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

# The owner's clock (Moscow, UTC+3; set OWNER_TZ when he travels): every «в 18:00», reminder, schedule and the
# daily limit follow it
TZ = ZoneInfo(os.environ.get("OWNER_TZ", "Europe/Moscow"))


def day_start() -> float:
    """Last midnight on the owner's clock: daily limits reset here."""
    return dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


@dataclass(frozen=True)
class Config:
    owner_id: int
    bot_token: str
    anthropic_key: str
    model: str
    smart_model: str
    db_path: str
    history_limit: int  # messages the Claude brain sees
    front_history: int  # messages the free Gemini chat sees
    # USD per million tokens (input, output) for (model, smart_model); only for the spend counter
    prices: tuple[tuple[float, float], tuple[float, float]]
    # USD caps: one request, and everything since the owner's midnight
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
    # Jarvis's own Telegram account (Telethon); empty api_id means the account is off
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    # Gemini (free): everyday chat with the owner, everyone else, and calls; empty key means Claude does it all
    gemini_key: str
    gemini_model: str  # comma-separated chain: the next one answers when the first is overloaded
    live_model: str  # same, for calls (Gemini Live: listens and speaks itself)
    live_voice: str
    owner_name: str
    # Strangers: replies per person per hour, replies in total per day, minutes per call
    stranger_hourly: int
    stranger_daily: int
    call_minutes: int
    # Strangers and groups talk to Claude Haiku first (Gemini when it cannot): model, dollars a day, prices
    chat_model: str
    chat_daily: float
    chat_prices: tuple[float, float]
    # Monitoring: sites to watch (comma-separated), the other VPS, SSH keys of the owner's own machines
    monitor_sites: str
    monitor_hosts: str
    # Groups: what Jarvis says in all of them (per group per hour, in total per day), and how often he reacts or
    # comments on his own when nobody called him (share of pictures/videos, share of longer text messages)
    group_hourly: int
    group_daily: int
    group_react_chance: float
    group_text_chance: float
    # How often a request carries the urge to throw in a sticker or a GIF (the model never does it on its own)
    extra_chance: float


def load() -> Config:
    return Config(
        owner_id=int(os.environ["OWNER_TG_ID"]),
        # Optional since Jarvis has its own account; empty means no bot at all
        bot_token=os.environ.get("TG_BOT_TOKEN", ""),
        anthropic_key=os.environ["ANTHROPIC_API_KEY"],
        model=os.environ.get("JARVIS_MODEL", "claude-haiku-5-5"),
        smart_model=os.environ.get("JARVIS_SMART_MODEL", "claude-sonnet-5-5"),
        db_path=os.environ.get("JARVIS_DB", "/opt/jarvis/data/jarvis.db"),
        history_limit=int(os.environ.get("JARVIS_HISTORY", "40")),
        front_history=int(os.environ.get("JARVIS_FRONT_HISTORY", "100")),
        # Haiku 5.5 and Sonnet 5.5 (the old defaults were Haiku 4.5's, ten times too high)
        prices=(
            (float(os.environ.get("PRICE_IN", "0.1")), float(os.environ.get("PRICE_OUT", "0.5"))),
            (float(os.environ.get("SMART_PRICE_IN", "2")), float(os.environ.get("SMART_PRICE_OUT", "10"))),
        ),
        task_limit=float(os.environ.get("TASK_LIMIT", "0.60")),
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
        tg_api_id=int(os.environ.get("TG_API_ID") or 0),
        tg_api_hash=os.environ.get("TG_API_HASH", ""),
        tg_session=os.environ.get("TG_SESSION", "/opt/jarvis/data/tg_user"),
        gemini_key=os.environ.get("GEMINI_API_KEY", ""),
        # The free tier gives gemini-3.6-flash only 20 requests a day; the lite ones carry the rest
        gemini_model=os.environ.get("GEMINI_MODEL", "gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.1-flash-lite"),
        live_model=os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live,gemini-3.1-flash-live-preview"),
        live_voice=os.environ.get("GEMINI_LIVE_VOICE", "Charon"),
        owner_name=os.environ.get("OWNER_NAME", ""),
        stranger_hourly=int(os.environ.get("STRANGER_HOURLY", "20")),
        stranger_daily=int(os.environ.get("STRANGER_DAILY", "300")),
        call_minutes=int(os.environ.get("CALL_MINUTES", "10")),
        chat_model=os.environ.get("CHAT_MODEL", "claude-haiku-5-5"),
        chat_daily=float(os.environ.get("CHAT_DAILY", "0.5")),
        chat_prices=(float(os.environ.get("CHAT_PRICE_IN", "0.1")), float(os.environ.get("CHAT_PRICE_OUT", "0.5"))),
        monitor_sites=os.environ.get("MONITOR_SITES", "https://nvate.uz"),
        monitor_hosts=os.environ.get("MONITOR_HOSTS", "London=root@46.226.162.170"),
        group_hourly=int(os.environ.get("GROUP_HOURLY", "20")),
        group_daily=int(os.environ.get("GROUP_DAILY", "150")),
        group_react_chance=float(os.environ.get("GROUP_REACT_CHANCE", "0.35")),
        group_text_chance=float(os.environ.get("GROUP_TEXT_CHANCE", "0.05")),
        extra_chance=float(os.environ.get("EXTRA_CHANCE", "0.15")),
    )
