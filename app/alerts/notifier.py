"""Records operational events in ``system_events`` and forwards important ones to Telegram."""

from __future__ import annotations

import logging
import time
from typing import Any

from app.alerts.telegram import TelegramSender
from app.db.enums import EventLevel
from app.db.models import SystemEvent
from app.db.session import Database

log = logging.getLogger("tradlysis.events")

_LEVEL_EMOJI = {
    EventLevel.INFO: "ℹ️",
    EventLevel.WARNING: "⚠️",
    EventLevel.ERROR: "❌",
    EventLevel.CRITICAL: "🚨",
}


class Notifier:
    """Single place for "something happened" signals.

    Every event is logged and persisted. Events at WARNING or above, or any event with
    ``alert=True``, are also sent to Telegram, de-duplicated by ``dedup_key`` so a flapping
    stream does not flood the chat.
    """

    def __init__(
        self,
        db: Database | None,
        telegram: TelegramSender | None,
        dedup_seconds: int = 300,
        label: str = "",
    ) -> None:
        self.db = db
        self.telegram = telegram
        self.dedup_seconds = dedup_seconds
        self.label = label
        self._last_sent: dict[str, float] = {}

    async def event(
        self,
        level: EventLevel,
        component: str,
        event_type: str,
        message: str,
        details: dict[str, Any] | None = None,
        *,
        alert: bool | None = None,
        dedup_key: str | None = None,
    ) -> None:
        details = details or {}
        log.log(_py_level(level), "[%s] %s: %s %s", component, event_type, message, details or "")
        if self.db is not None:
            try:
                async with self.db.session() as s:
                    s.add(
                        SystemEvent(
                            level=str(level),
                            component=component,
                            event_type=event_type,
                            message=message,
                            details=_jsonable(details),
                        )
                    )
            except Exception:  # never let logging break trading logic
                log.exception("Failed to persist system event %s", event_type)

        should_alert = alert if alert is not None else level != EventLevel.INFO
        if should_alert:
            await self._alert(level, component, event_type, message, dedup_key or event_type)

    async def _alert(
        self, level: EventLevel, component: str, event_type: str, message: str, key: str
    ) -> None:
        if self.telegram is None or not self.telegram.enabled:
            return
        now = time.monotonic()
        last = self._last_sent.get(key)
        if last is not None and now - last < self.dedup_seconds:
            return
        self._last_sent[key] = now
        prefix = f"{self.label} " if self.label else ""
        text = f"{_LEVEL_EMOJI.get(level, '')} {prefix}{component} · {event_type}\n{message}"
        await self.telegram.send(text)

    async def info(self, component: str, event_type: str, message: str, **kw: Any) -> None:
        await self.event(EventLevel.INFO, component, event_type, message, **kw)

    async def warning(self, component: str, event_type: str, message: str, **kw: Any) -> None:
        await self.event(EventLevel.WARNING, component, event_type, message, **kw)

    async def error(self, component: str, event_type: str, message: str, **kw: Any) -> None:
        await self.event(EventLevel.ERROR, component, event_type, message, **kw)

    async def critical(self, component: str, event_type: str, message: str, **kw: Any) -> None:
        await self.event(EventLevel.CRITICAL, component, event_type, message, **kw)


def _py_level(level: EventLevel) -> int:
    return {
        EventLevel.INFO: logging.INFO,
        EventLevel.WARNING: logging.WARNING,
        EventLevel.ERROR: logging.ERROR,
        EventLevel.CRITICAL: logging.CRITICAL,
    }[level]


def _jsonable(value: Any) -> Any:
    """Best-effort conversion to JSON-serialisable data."""
    import json

    return json.loads(json.dumps(value, default=str))
