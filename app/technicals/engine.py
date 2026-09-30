"""Technical Analysis Engine: turns candles into a deterministic technical state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MarketRegime, SupportResistance, TechnicalSnapshot
from app.market_data.candles import Bar
from app.technicals.indicators import atr, ema, last_value, rsi
from app.technicals.levels import (
    Level,
    assign_kinds,
    cluster_swings,
    nearest,
    psychological_levels,
    reference_levels,
)
from app.technicals.regime import (
    MarketRegimeLabel,
    TrendRegime,
    VolatilityRegime,
    classify_market_regime,
    trend_regime,
    volatility_regime,
)
from app.technicals.structure import Structure, Swing, Trend, classify_structure, find_swings, trend_direction

EMA_SLOPE_LOOKBACK = 5
SWING_WINDOW = {"M15": 3, "H1": 2, "H4": 2}
ZONE_TOLERANCE_ATR = 0.3


@dataclass
class TimeframeState:
    timeframe: str
    candle_time: datetime
    open: float
    high: float
    low: float
    close: float
    ema20: float | None
    ema50: float | None
    ema200: float | None
    ema50_prev: float | None
    rsi14: float | None
    rsi14_prev: float | None
    atr14: float | None
    trend: Trend
    structure: Structure
    swings: list[Swing] = field(default_factory=list)
    atr_series: list[float | None] = field(default_factory=list)
    recent_bars: list[Bar] = field(default_factory=list)

    @property
    def last_swing_high(self) -> float | None:
        highs = [s for s in self.swings if s.kind == "HIGH"]
        return highs[-1].price if highs else None

    @property
    def last_swing_low(self) -> float | None:
        lows = [s for s in self.swings if s.kind == "LOW"]
        return lows[-1].price if lows else None


@dataclass
class TechnicalState:
    instrument: str
    as_of: datetime
    price: float
    pip_size: float
    timeframes: dict[str, TimeframeState]
    levels: list[Level]
    nearest_support: Level | None
    nearest_resistance: Level | None
    volatility_regime: VolatilityRegime
    atr_percentile: float | None
    trend_regimes: dict[str, TrendRegime]

    def pips(self, distance: float) -> float:
        return round(distance / self.pip_size, 1)

    def market_regime(self, news_blackout: bool) -> MarketRegimeLabel:
        h4, h1 = self.timeframes["H4"], self.timeframes["H1"]
        prior = h1.recent_bars[-21:-1]
        return classify_market_regime(
            h4_trend=h4.trend,
            h1_trend=h1.trend,
            h4_structure_bullish=h4.structure == Structure.BULLISH,
            h4_structure_bearish=h4.structure == Structure.BEARISH,
            h1_close=h1.close,
            h1_prior_high=max((b.high for b in prior), default=None),
            h1_prior_low=min((b.low for b in prior), default=None),
            volatility=self.volatility_regime,
            news_blackout=news_blackout,
        )


def compute_timeframe_state(timeframe: str, bars: Sequence[Bar]) -> TimeframeState:
    if not bars:
        raise ValueError(f"no bars for {timeframe}")
    closes = [b.close for b in bars]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)
    r = rsi(closes, 14)
    a = atr(bars, 14)
    swings = find_swings(bars, SWING_WINDOW.get(timeframe, 2), SWING_WINDOW.get(timeframe, 2))
    structure = classify_structure(swings)
    last = bars[-1]
    ema50_now = last_value(e50)
    ema50_prev = last_value(e50, EMA_SLOPE_LOOKBACK)
    trend = trend_direction(last.close, last_value(e20), ema50_now, ema50_prev, structure)
    return TimeframeState(
        timeframe=timeframe,
        candle_time=last.time,
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        ema20=last_value(e20),
        ema50=ema50_now,
        ema200=last_value(e200),
        ema50_prev=ema50_prev,
        rsi14=last_value(r),
        rsi14_prev=last_value(r, 1),
        atr14=last_value(a),
        trend=trend,
        structure=structure,
        swings=swings,
        atr_series=a,
        recent_bars=list(bars[-30:]),
    )


def compute_technical_state(
    instrument: str,
    bars_by_tf: dict[str, Sequence[Bar]],
    price: float,
    pip_size: float,
    as_of: datetime,
) -> TechnicalState:
    """Compute the full technical state.

    ``bars_by_tf`` must contain complete candles for M15, H1 and H4 (ascending); D, W and M
    are optional and used for previous day/week/month levels.
    """
    tfs = {tf: compute_timeframe_state(tf, bars_by_tf[tf]) for tf in ("H4", "H1", "M15")}

    h1 = tfs["H1"]
    tolerance = (h1.atr14 or (price * 0.0005)) * ZONE_TOLERANCE_ATR
    h1_bars = bars_by_tf["H1"]
    h4_bars = bars_by_tf["H4"]
    zones = cluster_swings(
        {
            "H1": [s for s in tfs["H1"].swings if s.index >= len(h1_bars) - 200],
            "H4": [s for s in tfs["H4"].swings if s.index >= len(h4_bars) - 150],
        },
        tolerance,
        total_bars_by_tf={"H1": len(h1_bars), "H4": len(h4_bars)},
    )
    refs = reference_levels(bars_by_tf.get("D", []), bars_by_tf.get("W", []), bars_by_tf.get("M", []))
    levels = assign_kinds(zones + refs + psychological_levels(price, pip_size), price)

    vol_regime, pct = volatility_regime(h1.atr_series)
    return TechnicalState(
        instrument=instrument,
        as_of=as_of,
        price=price,
        pip_size=pip_size,
        timeframes=tfs,
        levels=levels,
        nearest_support=nearest(levels, price, "SUPPORT"),
        nearest_resistance=nearest(levels, price, "RESISTANCE"),
        volatility_regime=vol_regime,
        atr_percentile=pct,
        trend_regimes={tf: trend_regime(s.trend) for tf, s in tfs.items()},
    )


async def persist_technical_state(session: AsyncSession, state: TechnicalState) -> None:
    """Store per-timeframe snapshots, current S/R levels and regimes."""
    for tf, s in state.timeframes.items():
        values: dict[str, Any] = {
            "instrument": state.instrument,
            "timeframe": tf,
            "candle_time": s.candle_time,
            "close": s.close,
            "ema20": s.ema20,
            "ema50": s.ema50,
            "ema200": s.ema200,
            "rsi14": s.rsi14,
            "atr14": s.atr14,
            "trend": str(s.trend),
            "structure": str(s.structure),
            "last_swing_high": s.last_swing_high,
            "last_swing_low": s.last_swing_low,
            "details": {
                "ema50_prev": s.ema50_prev,
                "rsi14_prev": s.rsi14_prev,
                "open": s.open,
                "high": s.high,
                "low": s.low,
            },
        }
        stmt = insert(TechnicalSnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument", "timeframe", "candle_time"],
            set_={k: stmt.excluded[k] for k in values if k not in ("instrument", "timeframe", "candle_time")},
        )
        await session.execute(stmt)

        session.add(
            MarketRegime(
                instrument=state.instrument,
                timeframe=tf,
                computed_at=state.as_of,
                trend_regime=str(state.trend_regimes[tf]),
                volatility_regime=str(state.volatility_regime) if tf == "H1" else "N/A",
                atr=s.atr14,
                atr_percentile=state.atr_percentile if tf == "H1" else None,
                details={"structure": str(s.structure), "trend": str(s.trend)},
            )
        )

    for lv in state.levels:
        session.add(
            SupportResistance(
                instrument=state.instrument,
                computed_at=state.as_of,
                kind=lv.kind,
                source=lv.source,
                timeframe=",".join(lv.timeframes) or None,
                price_low=lv.price_low,
                price_high=lv.price_high,
                price=lv.price,
                touches=lv.touches,
                strength=lv.strength,
            )
        )
