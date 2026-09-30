"""Provider interfaces for the news engine. Add a paid provider by implementing these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass
class CalendarEvent:
    provider: str
    external_id: str
    title: str
    currency: str
    impact: str  # LOW | MEDIUM | HIGH | HOLIDAY | UNKNOWN
    event_time: datetime
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Article:
    provider: str
    external_id: str
    title: str
    currency: str | None
    summary: str | None
    url: str | None
    published_at: datetime | None
    raw: dict[str, Any] = field(default_factory=dict)


class CalendarProvider(Protocol):
    name: str

    async def fetch(self) -> list[CalendarEvent]: ...


class ArticleProvider(Protocol):
    name: str

    async def fetch(self) -> list[Article]: ...
