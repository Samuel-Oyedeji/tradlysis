"""Market structure: swing points, HH/HL/LH/LL classification and trend direction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from app.market_data.candles import Bar


class Trend(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class Structure(StrEnum):
    BULLISH = "higher_highs_higher_lows"
    BEARISH = "lower_highs_lower_lows"
    EXPANDING = "higher_highs_lower_lows"
    CONTRACTING = "lower_highs_higher_lows"
    UNKNOWN = "insufficient_data"


@dataclass(frozen=True)
class Swing:
    index: int
    time: datetime
    price: float
    kind: str  # "HIGH" | "LOW"


def find_swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal pivots: a swing high is higher than ``left`` bars before and ``right`` bars after.

    Only bars with ``right`` confirmed bars after them can be swings, so the most recent
    ``right`` bars never produce a swing (no look-ahead).
    """
    swings: list[Swing] = []
    n = len(bars)
    for i in range(left, n - right):
        h = bars[i].high
        lo = bars[i].low
        if all(h > bars[j].high for j in range(i - left, i)) and all(
            h >= bars[j].high for j in range(i + 1, i + right + 1)
        ):
            swings.append(Swing(i, bars[i].time, h, "HIGH"))
        if all(lo < bars[j].low for j in range(i - left, i)) and all(
            lo <= bars[j].low for j in range(i + 1, i + right + 1)
        ):
            swings.append(Swing(i, bars[i].time, lo, "LOW"))
    return swings


def classify_structure(swings: Sequence[Swing]) -> Structure:
    highs = [s for s in swings if s.kind == "HIGH"]
    lows = [s for s in swings if s.kind == "LOW"]
    if len(highs) < 2 or len(lows) < 2:
        return Structure.UNKNOWN
    hh = highs[-1].price > highs[-2].price
    hl = lows[-1].price > lows[-2].price
    if hh and hl:
        return Structure.BULLISH
    if not hh and not hl:
        return Structure.BEARISH
    if hh and not hl:
        return Structure.EXPANDING
    return Structure.CONTRACTING


def trend_direction(
    close: float,
    ema_fast: float | None,
    ema_slow: float | None,
    ema_slow_prev: float | None,
    structure: Structure,
) -> Trend:
    """Deterministic trend label.

    Bullish: fast EMA above slow EMA, price above slow EMA, slow EMA not falling and the
    swing structure not bearish. Bearish is the mirror image. Anything else is neutral.
    """
    if ema_fast is None or ema_slow is None or ema_slow_prev is None:
        return Trend.UNKNOWN
    slope = ema_slow - ema_slow_prev
    if ema_fast > ema_slow and close > ema_slow and slope >= 0 and structure != Structure.BEARISH:
        return Trend.BULLISH
    if ema_fast < ema_slow and close < ema_slow and slope <= 0 and structure != Structure.BULLISH:
        return Trend.BEARISH
    return Trend.NEUTRAL
