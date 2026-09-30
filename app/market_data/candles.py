"""Candle representation and tick aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.market_data.timeutil import GRANULARITY_SECONDS, floor_time


@dataclass
class Bar:
    time: datetime  # open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    complete: bool = True
    bid_close: float | None = None
    ask_close: float | None = None

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class CandleAggregator:
    """Builds intraday mid-price candles from streamed ticks.

    Used for the live, still-forming candle. Completed candles used for decisions come from
    the broker's candle endpoint, which is authoritative.
    """

    granularity: str
    current: Bar | None = None
    completed: list[Bar] = field(default_factory=list)
    max_completed: int = 500

    def __post_init__(self) -> None:
        if self.granularity not in GRANULARITY_SECONDS:
            raise ValueError(f"unsupported granularity {self.granularity}")

    def on_tick(self, time: datetime, mid: float, bid: float | None = None, ask: float | None = None) -> Bar | None:
        """Add a tick. Returns the candle that just completed, if any."""
        start = floor_time(time, self.granularity)
        finished: Bar | None = None
        if self.current is not None and start > self.current.time:
            self.current.complete = True
            finished = self.current
            self.completed.append(finished)
            if len(self.completed) > self.max_completed:
                self.completed.pop(0)
            self.current = None
        if self.current is None:
            self.current = Bar(start, mid, mid, mid, mid, 1, False, bid, ask)
        elif start == self.current.time:
            c = self.current
            c.high = max(c.high, mid)
            c.low = min(c.low, mid)
            c.close = mid
            c.volume += 1
            c.bid_close = bid
            c.ask_close = ask
        # ticks older than the current candle are ignored
        return finished
