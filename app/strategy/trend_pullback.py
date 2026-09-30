"""Deterministic Trend Pullback setup detection and trade planning.

The strategy decides *whether the predefined setup is technically present* and, if so,
proposes entry / stop-loss / take-profit from structure. The LLM only confirms or declines
the setup; it can never change these prices. The risk engine sizes the position and has
final authority.

Long setup (short is the mirror image):
  1. H4 trend is bullish (higher-timeframe direction).
  2. H1 trend is not bearish (generally agrees with H4).
  3. Within the last N M15 bars price pulled back into a support level or the M15 EMA50.
  4. The pullback was meaningful (depth >= 1 x ATR(M15)).
  5. Momentum confirms continuation: last M15 candle bullish, closes above EMA20 (M15),
     RSI rising and not overbought.
  6. The resulting plan has an acceptable stop distance and risk/reward.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.config.settings import Settings
from app.db.enums import Direction
from app.market_data.candles import Bar
from app.technicals.engine import TechnicalState, TimeframeState
from app.technicals.levels import Level
from app.technicals.structure import Trend

SETUP_NAME = "TREND_PULLBACK"


@dataclass
class TradePlan:
    direction: str
    setup: str
    entry: float
    stop_loss: float
    take_profit: float
    risk_pips: float
    reward_pips: float
    risk_reward: float
    stop_source: str
    target_source: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradePlan:
        return cls(**data)


@dataclass
class Condition:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class StrategyResult:
    setup: str
    direction: str | None  # direction implied by the higher timeframe, if any
    candidate: bool  # all technical conditions passed
    conditions: list[Condition]
    trade_plan: TradePlan | None
    failure_codes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup,
            "direction": self.direction,
            "candidate": self.candidate,
            "conditions": [asdict(c) for c in self.conditions],
            "trade_plan": self.trade_plan.to_dict() if self.trade_plan else None,
            "failure_codes": self.failure_codes,
        }

    def condition(self, name: str) -> Condition | None:
        return next((c for c in self.conditions if c.name == name), None)


def evaluate_trend_pullback(
    tech: TechnicalState, bid: float, ask: float, settings: Settings
) -> StrategyResult:
    h4 = tech.timeframes["H4"]
    h1 = tech.timeframes["H1"]
    m15 = tech.timeframes["M15"]
    conditions: list[Condition] = []
    failures: list[str] = []

    # 1. Higher-timeframe direction ---------------------------------------------------
    if h4.trend == Trend.BULLISH:
        direction = Direction.BUY
    elif h4.trend == Trend.BEARISH:
        direction = Direction.SELL
    else:
        conditions.append(Condition("htf_trend", False, f"H4 trend is {h4.trend}"))
        return StrategyResult(SETUP_NAME, None, False, conditions, None, ["NO_HTF_TREND"])
    long = direction == Direction.BUY
    conditions.append(Condition("htf_trend", True, f"H4 trend is {h4.trend} ({h4.structure})"))

    # 2. H1 agreement -------------------------------------------------------------------
    opposing = Trend.BEARISH if long else Trend.BULLISH
    h1_ok = h1.trend not in (opposing, Trend.UNKNOWN)
    strict = h1.trend == h4.trend
    conditions.append(
        Condition(
            "h1_alignment",
            h1_ok,
            f"H1 trend is {h1.trend}" + (" (fully aligned)" if strict else ""),
        )
    )
    if not h1_ok:
        failures.append("H1_CONFLICT")

    # 3/4. Pullback into a level ------------------------------------------------------
    atr_m15 = m15.atr14 or 0.0
    pullback = _find_pullback(m15, tech.levels, long, settings)
    conditions.append(Condition("pullback_to_level", pullback.touched is not None, pullback.detail))
    if pullback.touched is None:
        failures.append("NO_PULLBACK_TO_LEVEL")
    depth_ok = atr_m15 > 0 and pullback.depth >= atr_m15
    conditions.append(
        Condition(
            "pullback_depth",
            depth_ok,
            f"depth {tech.pips(pullback.depth)} pips vs ATR(M15) {tech.pips(atr_m15)} pips",
        )
    )
    if not depth_ok:
        failures.append("PULLBACK_TOO_SHALLOW")

    # 5. Momentum confirmation ----------------------------------------------------------
    mom_ok, mom_detail = _momentum_confirms(m15, long)
    conditions.append(Condition("momentum", mom_ok, mom_detail))
    if not mom_ok:
        failures.append("NO_MOMENTUM_CONFIRMATION")

    # 6. Trade plan ---------------------------------------------------------------------
    plan: TradePlan | None = None
    if pullback.extreme is not None and atr_m15 > 0:
        plan = _build_plan(tech, direction, bid, ask, pullback, atr_m15, settings)
        stop_ok = plan.risk_pips <= settings.max_stop_pips
        conditions.append(
            Condition(
                "stop_distance",
                stop_ok,
                f"{plan.risk_pips} pips (max {settings.max_stop_pips})",
            )
        )
        if not stop_ok:
            failures.append("STOP_TOO_WIDE")
        rr_ok = plan.risk_reward >= settings.min_risk_reward
        conditions.append(
            Condition(
                "risk_reward",
                rr_ok,
                f"{plan.risk_reward:.2f}R to {plan.target_source} (min {settings.min_risk_reward})",
            )
        )
        if not rr_ok:
            failures.append("POOR_RISK_REWARD")

    candidate = all(c.passed for c in conditions) and plan is not None
    return StrategyResult(SETUP_NAME, str(direction), candidate, conditions, plan, failures)


# --------------------------------------------------------------------------- helpers


@dataclass
class _Pullback:
    extreme: float | None  # pullback low (long) / high (short)
    depth: float
    touched: str | None  # description of the level touched
    touched_level: Level | None
    detail: str


def _find_pullback(m15: TimeframeState, levels: list[Level], long: bool, settings: Settings) -> _Pullback:
    n = settings.strategy_pullback_lookback_bars
    bars: list[Bar] = m15.recent_bars
    if len(bars) < n + 2 or not m15.atr14:
        return _Pullback(None, 0.0, None, None, "insufficient M15 data")
    window = bars[-n:]
    tolerance = m15.atr14 * settings.strategy_level_tolerance_atr

    if long:
        idx = min(range(len(window)), key=lambda i: window[i].low)
        extreme = window[idx].low
        before = bars[-2 * n : len(bars) - n + idx + 1]
        pre_extreme = max(b.high for b in before)
        depth = pre_extreme - extreme
    else:
        idx = max(range(len(window)), key=lambda i: window[i].high)
        extreme = window[idx].high
        before = bars[-2 * n : len(bars) - n + idx + 1]
        pre_extreme = min(b.low for b in before)
        depth = extreme - pre_extreme

    # Static levels: the pullback extreme came within tolerance of a level. Levels are
    # labelled relative to the *current* price, so a zone price is still inside may carry
    # either label; any level within tolerance of the extreme counts.
    touched_level: Level | None = None
    best = tolerance
    for lv in levels:
        dist = lv.distance_to(extreme)
        if dist <= best:
            best = dist
            touched_level = lv

    if touched_level is not None:
        desc = f"{touched_level.source} {touched_level.price_low:.5f}-{touched_level.price_high:.5f}"
        return _Pullback(extreme, depth, desc, touched_level, f"pullback extreme {extreme:.5f} tested {desc}")

    # Dynamic level: M15 EMA50.
    if m15.ema50 is not None and abs(extreme - m15.ema50) <= tolerance:
        desc = f"M15_EMA50 {m15.ema50:.5f}"
        return _Pullback(extreme, depth, desc, None, f"pullback extreme {extreme:.5f} tested {desc}")

    return _Pullback(extreme, depth, None, None, f"pullback extreme {extreme:.5f} not at a level")


def _momentum_confirms(m15: TimeframeState, long: bool) -> tuple[bool, str]:
    bars = m15.recent_bars
    if not bars or m15.ema20 is None or m15.rsi14 is None or m15.rsi14_prev is None:
        return False, "insufficient data"
    last = bars[-1]
    if long:
        checks = {
            "bullish_candle": last.close > last.open,
            "close_above_ema20": last.close > m15.ema20,
            "rsi_rising": m15.rsi14 > m15.rsi14_prev,
            "rsi_not_overbought": 45.0 <= m15.rsi14 <= 70.0,
        }
    else:
        checks = {
            "bearish_candle": last.close < last.open,
            "close_below_ema20": last.close < m15.ema20,
            "rsi_falling": m15.rsi14 < m15.rsi14_prev,
            "rsi_not_oversold": 30.0 <= m15.rsi14 <= 55.0,
        }
    ok = all(checks.values())
    detail = ", ".join(f"{k}={'y' if v else 'n'}" for k, v in checks.items()) + f", rsi={m15.rsi14:.1f}"
    return ok, detail


def _build_plan(
    tech: TechnicalState,
    direction: Direction,
    bid: float,
    ask: float,
    pullback: _Pullback,
    atr_m15: float,
    settings: Settings,
) -> TradePlan:
    pip = tech.pip_size
    long = direction == Direction.BUY
    buffer = atr_m15 * settings.strategy_stop_buffer_atr
    notes: list[str] = []
    assert pullback.extreme is not None

    if long:
        entry = ask
        anchor = pullback.extreme
        if pullback.touched_level is not None:
            anchor = min(anchor, pullback.touched_level.price_low)
        stop = anchor - buffer
        if (entry - stop) / pip < settings.min_stop_pips:
            stop = entry - settings.min_stop_pips * pip
            notes.append("stop widened to minimum distance")
    else:
        entry = bid
        anchor = pullback.extreme
        if pullback.touched_level is not None:
            anchor = max(anchor, pullback.touched_level.price_high)
        stop = anchor + buffer
        if (stop - entry) / pip < settings.min_stop_pips:
            stop = entry + settings.min_stop_pips * pip
            notes.append("stop widened to minimum distance")

    risk = abs(entry - stop)

    # Target: the nearest opposing level beyond entry, else a fixed R multiple; capped.
    target_source = f"FIXED_{settings.strategy_fallback_target_r:g}R"
    target = entry + (1 if long else -1) * risk * settings.strategy_fallback_target_r
    opposing = [
        lv
        for lv in tech.levels
        if (long and lv.price_low > entry) or (not long and lv.price_high < entry)
    ]
    if opposing:
        lv = min(opposing, key=lambda x: abs(x.price - entry))
        edge_buffer = atr_m15 * 0.1
        target = (lv.price_low - edge_buffer) if long else (lv.price_high + edge_buffer)
        target_source = lv.source
    max_target = entry + (1 if long else -1) * risk * settings.strategy_max_target_r
    if (long and target > max_target) or (not long and target < max_target):
        target = max_target
        target_source = f"CAPPED_{settings.strategy_max_target_r:g}R"

    reward = (target - entry) if long else (entry - target)
    rr = reward / risk if risk > 0 else 0.0
    return TradePlan(
        direction=str(direction),
        setup=SETUP_NAME,
        entry=round(entry, 6),
        stop_loss=round(stop, 6),
        take_profit=round(target, 6),
        risk_pips=round(risk / pip, 1),
        reward_pips=round(reward / pip, 1),
        risk_reward=round(rr, 2),
        stop_source="PULLBACK_EXTREME" + ("+LEVEL" if pullback.touched_level else ""),
        target_source=target_source,
        notes=notes,
    )
