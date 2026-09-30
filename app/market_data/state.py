"""In-memory live market state fed by the price stream."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.broker.types import Quote
from app.market_data.candles import CandleAggregator
from app.market_data.timeutil import is_fx_market_open


@dataclass
class PriceTick:
    instrument: str
    time: datetime
    bid: float
    ask: float
    tradeable: bool = True

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def spread_pips(self, pip_size: float) -> float:
        return round(self.spread / pip_size, 2)

    @classmethod
    def from_quote(cls, quote: Quote, tradeable: bool = True) -> PriceTick:
        return cls(instrument=quote.instrument, time=quote.time, bid=quote.bid, ask=quote.ask, tradeable=tradeable)


@dataclass
class MarketState:
    instrument: str
    pip_size: float
    last_tick: PriceTick | None = None
    last_tick_received_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    stream_connected: bool = False
    stream_connected_since: datetime | None = None
    reconnects: int = 0
    # Market status reported by the broker (Capital.com "TRADEABLE"); refreshed by the market service.
    broker_tradeable: bool = True
    broker_market_status: str | None = None
    aggregators: dict[str, CandleAggregator] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for g in ("M1", "M5", "M15"):
            self.aggregators.setdefault(g, CandleAggregator(g))

    def on_tick(self, tick: PriceTick, received_at: datetime) -> None:
        if self.last_tick is not None and tick.time < self.last_tick.time:
            return  # out-of-order
        self.last_tick = tick
        self.last_tick_received_at = received_at
        for agg in self.aggregators.values():
            agg.on_tick(tick.time, tick.mid, tick.bid, tick.ask)

    def on_heartbeat(self, at: datetime) -> None:
        self.last_heartbeat_at = at

    def price_age_seconds(self, now: datetime) -> float | None:
        if self.last_tick is None:
            return None
        return (now - self.last_tick.time).total_seconds()

    def is_price_stale(self, now: datetime, max_age_seconds: float) -> bool:
        """Stale if no price yet, the stream is down, or the last price is too old.

        While the FX market is closed prices legitimately stop, so staleness is only
        meaningful during market hours (trading is blocked anyway when the market is closed).
        """
        if self.last_tick is None or not self.stream_connected:
            return True
        age = self.price_age_seconds(now)
        return age is None or age > max_age_seconds

    def is_heartbeat_stale(self, now: datetime, max_age_seconds: float = 20.0) -> bool:
        if self.last_heartbeat_at is None:
            return True
        return (now - self.last_heartbeat_at).total_seconds() > max_age_seconds

    def market_open(self, now: datetime) -> bool:
        return is_fx_market_open(now)

    def status(self, now: datetime) -> dict[str, Any]:
        t = self.last_tick
        return {
            "instrument": self.instrument,
            "stream_connected": self.stream_connected,
            "stream_connected_since": self.stream_connected_since.isoformat()
            if self.stream_connected_since
            else None,
            "reconnects": self.reconnects,
            "market_open": self.market_open(now),
            "bid": t.bid if t else None,
            "ask": t.ask if t else None,
            "spread_pips": t.spread_pips(self.pip_size) if t else None,
            "tradeable": t.tradeable if t else None,
            "broker_market_status": self.broker_market_status,
            "price_time": t.time.isoformat() if t else None,
            "price_age_seconds": self.price_age_seconds(now),
            "last_heartbeat_at": self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None,
        }
