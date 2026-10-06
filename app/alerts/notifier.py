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

# Readable Telegram headings for the events that are sent there; anything else falls back to
# the level emoji and the event type in words.
_TITLES = {
    "SETUP": "🔎 Setup found",
    "TRADE_REJECTED": "⛔ Trade rejected by risk engine",
    "ORDER_FILLED": "✅ Order filled",
    "ORDER_REJECTED": "❌ Order rejected by broker",
    "ORDER_FAILED": "❌ Order failed",
    "ORDER_HTTP_ERROR": "❌ Order request error",
    "ORDER_BLOCKED": "⛔ Order blocked",
    "ORDER_NOT_FOUND": "❌ Order not found at broker",
    "ORDER_ADOPTED": "🔁 Reconciliation: order matched to broker position",
    "SLIPPAGE_EXCEEDED": "⚠️ Slippage too high, closing",
    "TRADE_CLOSED": "🏁 Trade closed",
    "FLATTENED": "🧹 All positions closed",
    "UNEXPECTED_POSITION": "🚨 Reconciliation: unknown position at broker",
    "CLOSE_DETAILS_MISSING": "❌ Reconciliation: close details missing",
    "RECONCILE_FAILED": "❌ Reconciliation failing",
    "RECONCILE_RECOVERED": "✅ Reconciliation recovered",
    "DAILY_LOSS_BREAKER": "🚨 Daily loss limit hit",
    "DRAWDOWN_BREAKER": "🚨 Drawdown limit hit",
    "STALE_PRICES": "⚠️ No fresh prices",
    "PRICES_RECOVERED": "✅ Prices flowing again",
    "ENGINE_STARTED": "▶️ Engine started",
    "ENGINE_STOPPED": "⏹ Engine stopped",
}


class Notifier:
    """Single place for "something happened" signals.

    Every event is logged and persisted (the dashboard shows them). Only ERROR/CRITICAL events,
    or events passed ``alert=True``, are also sent to Telegram, so the chat carries setups,
    executions, reconciliation results and real problems rather than routine noise such as
    stream reconnects. Pass ``alert=False`` to keep an error off Telegram. Sends are
    de-duplicated by ``dedup_key``.
    """

    def __init__(
        self,
        db: Database | None,
        telegram: TelegramSender | None,
        dedup_seconds: int = 300,
        label: str = "",
        experiment: str | None = None,
        *,
        _last_sent: dict[str, float] | None = None,
    ) -> None:
        self.db = db
        self.telegram = telegram
        self.dedup_seconds = dedup_seconds
        self.label = label
        # Events about one experiment carry its slug in details["experiment"] (the dashboard filters on it).
        self.experiment = experiment
        self._last_sent: dict[str, float] = {} if _last_sent is None else _last_sent

    def for_experiment(self, slug: str, name: str) -> Notifier:
        """A notifier for one experiment: its events are tagged and its alerts name it."""
        label = f"{self.label} {name}".strip()
        return Notifier(self.db, self.telegram, self.dedup_seconds, label, slug, _last_sent=self._last_sent)

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
        if self.experiment:
            details = {"experiment": self.experiment, **details}
            dedup_key = f"{self.experiment}:{dedup_key or event_type}"
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

        should_alert = alert if alert is not None else level in (EventLevel.ERROR, EventLevel.CRITICAL)
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
        title = _TITLES.get(event_type) or f"{_LEVEL_EMOJI.get(level, '')} {event_type.replace('_', ' ').capitalize()}"
        text = f"{prefix}{title}\n{message}"
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
