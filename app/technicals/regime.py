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


class MarketRegimeLabel(StrEnum):
    """Overall regime (conversation regimes A-E). Only strong trends suit the trend-pullback setup."""

    STRONG_UPTREND = "STRONG_UPTREND"
    STRONG_DOWNTREND = "STRONG_DOWNTREND"
    BREAKOUT_UP = "BREAKOUT_UP"
    BREAKOUT_DOWN = "BREAKOUT_DOWN"
    COMPRESSION = "COMPRESSION"
    RANGE = "RANGE"
    EVENT_RISK = "EVENT_RISK"
    UNCLEAR = "UNCLEAR"


def classify_market_regime(
    *,
    h4_trend: Trend,
    h1_trend: Trend,
    h4_structure_bullish: bool,
    h4_structure_bearish: bool,
    h1_close: float,
    h1_prior_high: float | None,
    h1_prior_low: float | None,
    volatility: VolatilityRegime,
    news_blackout: bool,
) -> MarketRegimeLabel:
    """Deterministic priority: event risk > strong trend > breakout > compression > range > unclear.

    * Strong trend: H4 and H1 agree and H4 swing structure is not against them.
    * Breakout: the last H1 close is outside the prior 20 H1 bars' range.
    * Compression: low volatility (ATR in the bottom quartile of its recent history).
    * Range: neither H4 nor H1 is trending.
    """
    if news_blackout:
        return MarketRegimeLabel.EVENT_RISK
    if h4_trend == Trend.BULLISH and h1_trend == Trend.BULLISH and not h4_structure_bearish:
        return MarketRegimeLabel.STRONG_UPTREND
    if h4_trend == Trend.BEARISH and h1_trend == Trend.BEARISH and not h4_structure_bullish:
        return MarketRegimeLabel.STRONG_DOWNTREND
    if h1_prior_high is not None and h1_close > h1_prior_high:
        return MarketRegimeLabel.BREAKOUT_UP
    if h1_prior_low is not None and h1_close < h1_prior_low:
        return MarketRegimeLabel.BREAKOUT_DOWN
    if volatility == VolatilityRegime.LOW:
        return MarketRegimeLabel.COMPRESSION
    if h4_trend == Trend.NEUTRAL and h1_trend == Trend.NEUTRAL:
        return MarketRegimeLabel.RANGE
    return MarketRegimeLabel.UNCLEAR
