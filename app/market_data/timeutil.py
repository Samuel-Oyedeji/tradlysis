"""Time helpers: RFC3339 parsing, FX market hours and candle boundaries."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")

GRANULARITY_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D": 86400,
    "W": 604800,
}

_FRACTION_RE = re.compile(r"\.(\d+)")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str) -> datetime:
    """Parse ISO/RFC3339 timestamps into aware UTC datetimes (naive values are taken as UTC)."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    value = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def is_fx_market_open(now: datetime) -> bool:
    """Retail FX trades from Sunday 17:00 to Friday 17:00 New York time."""
    ny = now.astimezone(NEW_YORK)
    weekday = ny.weekday()  # Monday=0 ... Sunday=6
    if weekday == 5:  # Saturday
        return False
    if weekday == 4 and ny.hour >= 17:  # Friday after close
        return False
    if weekday == 6 and ny.hour < 17:  # Sunday before open
        return False
    return True


def trading_day(now: datetime) -> date:
    """FX trading day, rolling over at 17:00 New York time."""
    ny = now.astimezone(NEW_YORK)
    if ny.hour >= 17:
        ny = ny + timedelta(days=1)
    return ny.date()


def floor_time(now: datetime, granularity: str) -> datetime:
    """Start of the (UTC-aligned) intraday candle containing ``now``. Intraday only."""
    seconds = GRANULARITY_SECONDS[granularity]
    if seconds > GRANULARITY_SECONDS["H1"]:
        raise ValueError("floor_time is only valid for granularities up to H1")
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)


def next_boundary(now: datetime, granularity: str) -> datetime:
    return floor_time(now, granularity) + timedelta(seconds=GRANULARITY_SECONDS[granularity])
