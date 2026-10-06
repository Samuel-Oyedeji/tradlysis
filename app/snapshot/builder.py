"""Market Snapshot: the single structured state handed to the decision layer.

The snapshot is stored verbatim in ``decision_requests.snapshot`` so every decision can be
audited against exactly what the model saw.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.market_data.state import PriceTick
from app.news.state import NewsState
from app.strategy.base import StrategyResult
from app.strategy.registry import SPEC_BY_SETUP
from app.technicals.engine import TechnicalState, TimeframeState
from app.technicals.levels import Level, sorted_by_distance
from app.technicals.regime import MarketRegimeLabel

SNAPSHOT_VERSION = "snapshot-v2"
TF_KEYS = {"H4": "4h", "H1": "1h", "M15": "15m"}


def _r(x: float | None, nd: int = 5) -> float | None:
    return None if x is None else round(x, nd)


def _tf(s: TimeframeState, tech: TechnicalState) -> dict[str, Any]:
    out: dict[str, Any] = {
        "candle_time": s.candle_time.isoformat(),
        "trend": str(s.trend),
        "structure": str(s.structure),
        "close": _r(s.close),
        "ema20": _r(s.ema20),
        "ema50": _r(s.ema50),
        "ema200": _r(s.ema200),
        "rsi14": _r(s.rsi14, 1),
        "rsi14_prev": _r(s.rsi14_prev, 1),
        "atr14_pips": tech.pips(s.atr14) if s.atr14 else None,
        "last_swing_high": _r(s.last_swing_high),
        "last_swing_low": _r(s.last_swing_low),
        "price_vs_ema200": None
        if s.ema200 is None
        else ("above" if s.close > s.ema200 else "below"),
    }
    if s.timeframe == "M15":
        out["last_candles"] = [
            {"t": b.time.isoformat(), "o": _r(b.open), "h": _r(b.high), "l": _r(b.low), "c": _r(b.close)}
            for b in s.recent_bars[-6:]
        ]
    return out


def _level(lv: Level, price: float, tech: TechnicalState) -> dict[str, Any]:
    return {
        "source": lv.source,
        "low": _r(lv.price_low),
        "high": _r(lv.price_high),
        "touches": lv.touches,
        "strength": lv.strength,
        "distance_pips": tech.pips(lv.distance_to(price)),
    }


def build_snapshot(
    *,
    tech: TechnicalState,
    tick: PriceTick,
    news: NewsState,
    strategy: StrategyResult,
    decision_time: datetime,
    market_regime: MarketRegimeLabel | None = None,
) -> dict[str, Any]:
    price = tick.mid
    m15_atr = tech.timeframes["M15"].atr14
    ns = tech.nearest_support
    nr = tech.nearest_resistance
    return {
        "version": SNAPSHOT_VERSION,
        "pair": tech.instrument,
        "decision_time": decision_time.isoformat(),
        "price": {
            "bid": _r(tick.bid),
            "ask": _r(tick.ask),
            "mid": _r(price),
            "spread_pips": tick.spread_pips(tech.pip_size),
            "time": tick.time.isoformat(),
            "tradeable": tick.tradeable,
        },
        "timeframes": {TF_KEYS[tf]: _tf(s, tech) for tf, s in tech.timeframes.items()},
        "levels": {
            "support": [_level(lv, price, tech) for lv in sorted_by_distance(tech.levels, price, "SUPPORT")],
            "resistance": [
                _level(lv, price, tech) for lv in sorted_by_distance(tech.levels, price, "RESISTANCE")
            ],
            "nearest_support_distance_atr": None
            if ns is None or not m15_atr
            else round(ns.distance_to(price) / m15_atr, 2),
            "nearest_resistance_distance_atr": None
            if nr is None or not m15_atr
            else round(nr.distance_to(price) / m15_atr, 2),
        },
        "volatility": {
            "regime": str(tech.volatility_regime),
            "atr_h1_pips": tech.pips(tech.timeframes["H1"].atr14) if tech.timeframes["H1"].atr14 else None,
            "atr_h1_percentile": tech.atr_percentile,
        },
        "market_regime": None
        if market_regime is None
        else {
            "label": str(market_regime),
            # Whether this regime suits the experiment's setup (see app/strategy/registry.py).
            "suits_setup": market_regime in SPEC_BY_SETUP[strategy.setup].suitable_regimes
            if strategy.setup in SPEC_BY_SETUP
            else None,
        },
        "regimes": {TF_KEYS[tf]: str(r) for tf, r in tech.trend_regimes.items()},
        "news": news.to_dict(),
        "setup_check": strategy.to_dict(),
    }
