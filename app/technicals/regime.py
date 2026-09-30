"""Volatility and trend regime classification."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from app.technicals.indicators import percentile_rank
from app.technicals.structure import Trend


class VolatilityRegime(StrEnum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    UNKNOWN = "UNKNOWN"


class TrendRegime(StrEnum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    UNKNOWN = "UNKNOWN"


def volatility_regime(atr_series: Sequence[float | None], lookback: int = 100) -> tuple[VolatilityRegime, float | None]:
    values = [v for v in atr_series if v is not None]
    if len(values) < 20:
        return VolatilityRegime.UNKNOWN, None
    window = values[-lookback:]
    current = window[-1]
    pct = percentile_rank(window, current)
    if pct >= 95:
        regime = VolatilityRegime.EXTREME
    elif pct >= 75:
        regime = VolatilityRegime.HIGH
    elif pct <= 25:
        regime = VolatilityRegime.LOW
    else:
        regime = VolatilityRegime.NORMAL
    return regime, round(pct, 1)


def trend_regime(trend: Trend) -> TrendRegime:
    return {
        Trend.BULLISH: TrendRegime.TRENDING_UP,
        Trend.BEARISH: TrendRegime.TRENDING_DOWN,
        Trend.NEUTRAL: TrendRegime.RANGING,
    }.get(trend, TrendRegime.UNKNOWN)
