"""ForexFactory weekly calendar JSON feed (free, no API key).

Feed: https://nfs.faireconomy.media/ff_calendar_thisweek.json
Each item: {"title", "country" (currency code), "date" (ISO with offset), "impact"
("High"/"Medium"/"Low"/"Holiday"/"Non-Economic"), "forecast", "previous"}; some mirrors
also include "actual". The publisher asks clients not to poll aggressively, so the engine
polls every NEWS_CALENDAR_POLL_MINUTES (default 30).
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

from app.market_data.timeutil import parse_time
from app.news.providers.base import CalendarEvent

log = logging.getLogger(__name__)

_IMPACT = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "holiday": "HOLIDAY"}


class ForexFactoryCalendar:
    name = "forexfactory"

    def __init__(self, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.url = url
        self._client = client or httpx.AsyncClient(
            timeout=20.0, headers={"User-Agent": "tradlysis/1.0 (+calendar poller)"}
        )

    async def fetch(self) -> list[CalendarEvent]:
        resp = await self._client.get(self.url)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            raise ValueError("unexpected calendar payload")
        return [e for e in (parse_item(item) for item in data) if e is not None]


def parse_item(item: dict[str, Any]) -> CalendarEvent | None:
    try:
        title = str(item["title"]).strip()
        currency = str(item.get("country", "")).strip().upper()
        when = parse_time(str(item["date"]))
    except (KeyError, ValueError) as exc:
        log.debug("Skipping calendar item %r: %s", item, exc)
        return None
    impact = _IMPACT.get(str(item.get("impact", "")).strip().lower(), "UNKNOWN")
    key = f"{title}|{currency}|{when.isoformat()}"
    return CalendarEvent(
        provider=ForexFactoryCalendar.name,
        external_id=hashlib.sha1(key.encode()).hexdigest(),
        title=title,
        currency=currency,
        impact=impact,
        event_time=when,
        forecast=_blank_to_none(item.get("forecast")),
        previous=_blank_to_none(item.get("previous")),
        actual=_blank_to_none(item.get("actual")),
        raw=item,
    )


def _blank_to_none(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None
