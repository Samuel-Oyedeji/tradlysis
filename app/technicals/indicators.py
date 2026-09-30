"""Deterministic indicator calculations (pure functions, no I/O)."""

from __future__ import annotations

from collections.abc import Sequence

from app.market_data.candles import Bar


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average seeded with the SMA of the first ``period`` values."""
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    prev = sum(values[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def sma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def true_ranges(bars: Sequence[Bar]) -> list[float]:
    trs: list[float] = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            trs.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return trs


def atr(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """Wilder's Average True Range."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs = true_ranges(bars)
    # The first TR has no previous close; start averaging from index 1.
    prev = sum(trs[1 : period + 1]) / period
    out[period] = prev
    for i in range(period + 1, len(bars)):
        prev = (prev * (period - 1) + trs[i]) / period
        out[i] = prev
    return out


def last_value(series: Sequence[float | None], offset: int = 0) -> float | None:
    """Value ``offset`` bars before the end (0 = latest)."""
    idx = len(series) - 1 - offset
    if idx < 0:
        return None
    return series[idx]


def percentile_rank(values: Sequence[float], value: float) -> float:
    """Percentage of ``values`` strictly below ``value`` (0-100)."""
    if not values:
        return 50.0
    below = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    return 100.0 * (below + 0.5 * equal) / len(values)
