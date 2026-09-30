"""Builders for hand-crafted technical states used by strategy/risk tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.market_data.candles import Bar
from app.technicals.engine import TechnicalState, TimeframeState
from app.technicals.levels import Level, assign_kinds, nearest
from app.technicals.regime import TrendRegime, VolatilityRegime
from app.technicals.structure import Structure, Trend

T0 = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)


def bars_from(points: list[tuple[float, float, float, float]]) -> list[Bar]:
    return [Bar(T0 + timedelta(minutes=15 * i), o, h, lo, c) for i, (o, h, lo, c) in enumerate(points)]


def tf(timeframe: str, trend: Trend, *, ema20=None, ema50=None, rsi=50.0, rsi_prev=50.0, atr=0.001, bars=None,
       structure=Structure.BULLISH) -> TimeframeState:
    bars = bars or bars_from([(1.1, 1.1, 1.1, 1.1)] * 12)
    last = bars[-1]
    return TimeframeState(
        timeframe=timeframe, candle_time=last.time, open=last.open, high=last.high, low=last.low, close=last.close,
        ema20=ema20, ema50=ema50, ema200=None, ema50_prev=ema50, rsi14=rsi, rsi14_prev=rsi_prev, atr14=atr,
        trend=trend, structure=structure, swings=[], atr_series=[], recent_bars=bars,
    )


def long_setup_bars() -> list[Bar]:
    """Rally to 1.1050, pull back to 1.1010, then a bullish confirmation candle closing 1.1025."""
    pts = []
    p = 1.0990
    for _ in range(10):  # impulse up
        pts.append((p, p + 0.0007, p - 0.0001, p + 0.0006))
        p += 0.0006
    # p ~ 1.1050
    for _ in range(7):  # pullback down
        pts.append((p, p + 0.0001, p - 0.0007, p - 0.0006))
        p -= 0.0006
    pts.append((1.1008 + 0.0004, 1.1013, 1.1010, 1.1012))  # low 1.1010
    pts.append((1.1012, 1.1016, 1.1011, 1.1014))
    pts.append((1.1014, 1.1027, 1.1013, 1.1025))  # bullish confirmation
    return bars_from(pts)


def mirror(bars: list[Bar], pivot: float = 2.2) -> list[Bar]:
    return [Bar(b.time, pivot - b.open, pivot - b.low, pivot - b.high, pivot - b.close) for b in bars]


def state(h4: TimeframeState, h1: TimeframeState, m15: TimeframeState, levels: list[Level], price: float) -> TechnicalState:
    levels = assign_kinds(levels, price)
    return TechnicalState(
        instrument="EUR_USD", as_of=T0, price=price, pip_size=0.0001,
        timeframes={"H4": h4, "H1": h1, "M15": m15}, levels=levels,
        nearest_support=nearest(levels, price, "SUPPORT"), nearest_resistance=nearest(levels, price, "RESISTANCE"),
        volatility_regime=VolatilityRegime.NORMAL, atr_percentile=50.0,
        trend_regimes={"H4": TrendRegime.TRENDING_UP, "H1": TrendRegime.TRENDING_UP, "M15": TrendRegime.RANGING},
    )


def long_state(*, h4=Trend.BULLISH, h1=Trend.BULLISH, rsi=55.0, rsi_prev=48.0, resistance=(1.1080, 1.1085)):
    levels = [Level("", "SWING_CLUSTER", 1.1005, 1.1012, 3, 4.0, ["H1"])]
    if resistance:
        levels.append(Level("", "SWING_CLUSTER", resistance[0], resistance[1], 2, 3.0, ["H4"]))
    m15 = tf("M15", Trend.NEUTRAL, ema20=1.1020, ema50=1.1000, rsi=rsi, rsi_prev=rsi_prev, atr=0.0010, bars=long_setup_bars())
    return state(tf("H4", h4, atr=0.004), tf("H1", h1, atr=0.002), m15, levels, 1.10255)


def short_state():
    pivot = 2.2
    levels = [
        Level("", "SWING_CLUSTER", pivot - 1.1012, pivot - 1.1005, 3, 4.0, ["H1"]),
        Level("", "SWING_CLUSTER", pivot - 1.1085, pivot - 1.1080, 2, 3.0, ["H4"]),
    ]
    m15 = tf("M15", Trend.NEUTRAL, ema20=pivot - 1.1020, ema50=pivot - 1.1000, rsi=45.0, rsi_prev=52.0, atr=0.0010,
             bars=mirror(long_setup_bars(), pivot), structure=Structure.BEARISH)
    return state(tf("H4", Trend.BEARISH, atr=0.004), tf("H1", Trend.BEARISH, atr=0.002), m15, levels, pivot - 1.10255)
