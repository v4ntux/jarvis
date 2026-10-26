"""The owner's clock. Moscow by default (OWNER_TZ overrides when the owner travels)."""
import datetime as dt
import os

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.environ.get("OWNER_TZ", "Europe/Moscow"))
except Exception:  # Windows without the tzdata package: Moscow has no daylight saving, a fixed offset is exact
    TZ = dt.timezone(dt.timedelta(hours=3), "MSK")


def now() -> dt.datetime:
    return dt.datetime.now(TZ)


def day_start(ts: float | None = None) -> float:
    """Last midnight on the owner's clock: daily limits reset here."""
    moment = dt.datetime.fromtimestamp(ts, TZ) if ts is not None else now()
    return moment.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def human(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, TZ).strftime("%d.%m %H:%M")
