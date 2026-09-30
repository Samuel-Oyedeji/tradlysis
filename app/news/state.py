"""Pure functions that turn stored news facts/interpretations into the snapshot's news state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

_IMPACT_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


@dataclass
class EventView:
    title: str
    currency: str
    impact: str
    event_time: datetime
    forecast: str | None = None
    previous: str | None = None
    actual: str | None = None
    surprise: float | None = None
    surprise_direction: str | None = None


@dataclass
class InterpretationView:
    currency: str
    currency_bias: str
    tone: str
    confidence: float
    created_at: datetime
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class NewsState:
    risk: str  # low | medium | high
    blackout: bool
    blackout_events: list[dict[str, Any]]
    next_high_impact: dict[str, Any] | None
    upcoming: list[dict[str, Any]]
    recent: list[dict[str, Any]]
    currency_bias: dict[str, dict[str, Any]]
    calendar_fresh: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "blackout": self.blackout,
            "blackout_events": self.blackout_events,
            "next_high_impact_event": self.next_high_impact,
            "upcoming_events": self.upcoming,
            "recent_events": self.recent,
            "currency_bias": self.currency_bias,
            "calendar_fresh": self.calendar_fresh,
        }


def _relevant(currency: str, currencies: Sequence[str]) -> bool:
    return currency in currencies or currency in ("ALL", "")


def _event_dict(e: EventView, now: datetime) -> dict[str, Any]:
    return {
        "title": e.title,
        "currency": e.currency,
        "impact": e.impact,
        "time": e.event_time.isoformat(),
        "minutes_until": round((e.event_time - now).total_seconds() / 60),
        "forecast": e.forecast,
        "previous": e.previous,
        "actual": e.actual,
        "surprise": e.surprise,
        "surprise_direction": e.surprise_direction,
    }


def build_news_state(
    events: Sequence[EventView],
    interpretations: Sequence[InterpretationView],
    now: datetime,
    currencies: Sequence[str],
    *,
    blocking_impacts: Sequence[str] = ("HIGH",),
    blackout_before_minutes: int = 30,
    blackout_after_minutes: int = 30,
    bias_lookback_hours: int = 72,
    calendar_fresh: bool = True,
) -> NewsState:
    relevant = sorted((e for e in events if _relevant(e.currency, currencies)), key=lambda e: e.event_time)

    blackout_events = [
        e
        for e in relevant
        if e.impact in blocking_impacts
        and now - timedelta(minutes=blackout_after_minutes)
        <= e.event_time
        <= now + timedelta(minutes=blackout_before_minutes)
    ]
    upcoming = [e for e in relevant if now < e.event_time <= now + timedelta(hours=24)]
    recent = [e for e in relevant if now - timedelta(hours=24) <= e.event_time <= now]
    next_high = next((e for e in upcoming if e.impact == "HIGH"), None)

    if blackout_events:
        risk = "high"
    elif any(
        e.impact == "HIGH" and e.event_time <= now + timedelta(hours=4) for e in upcoming
    ) or any(
        _IMPACT_RANK.get(e.impact, 0) >= 2 and abs((e.event_time - now).total_seconds()) <= 1800
        for e in relevant
    ):
        risk = "medium"
    else:
        risk = "low"
    if not calendar_fresh:
        # Without a current calendar we cannot rule out an imminent event.
        risk = "high" if risk == "high" else "unknown"

    return NewsState(
        risk=risk,
        blackout=bool(blackout_events),
        blackout_events=[_event_dict(e, now) for e in blackout_events],
        next_high_impact=_event_dict(next_high, now) if next_high else None,
        upcoming=[_event_dict(e, now) for e in upcoming if _IMPACT_RANK.get(e.impact, 0) >= 2][:8],
        recent=[_event_dict(e, now) for e in recent if _IMPACT_RANK.get(e.impact, 0) >= 2][-8:],
        currency_bias=aggregate_bias(interpretations, now, currencies, bias_lookback_hours),
        calendar_fresh=calendar_fresh,
    )


def aggregate_bias(
    interpretations: Sequence[InterpretationView],
    now: datetime,
    currencies: Sequence[str],
    lookback_hours: int,
) -> dict[str, dict[str, Any]]:
    """Confidence- and recency-weighted average of interpretations per currency.

    BULLISH = +1, BEARISH = -1, NEUTRAL = 0; weight = confidence x linear decay over the lookback.
    """
    out: dict[str, dict[str, Any]] = {}
    horizon = timedelta(hours=lookback_hours)
    for cur in currencies:
        items = [i for i in interpretations if i.currency == cur and now - i.created_at <= horizon]
        if not items:
            out[cur] = {"bias": "NEUTRAL", "score": 0.0, "confidence": 0.0, "items": 0}
            continue
        num = 0.0
        den = 0.0
        for i in items:
            decay = max(0.0, 1.0 - (now - i.created_at) / horizon)
            w = i.confidence * decay
            sign = {"BULLISH": 1.0, "BEARISH": -1.0}.get(i.currency_bias, 0.0)
            num += sign * w
            den += w
        score = num / den if den else 0.0
        bias = "BULLISH" if score > 0.25 else "BEARISH" if score < -0.25 else "NEUTRAL"
        out[cur] = {
            "bias": bias,
            "score": round(score, 3),
            "confidence": round(min(1.0, den / len(items)), 3),
            "items": len(items),
        }
    return out
