"""Deterministic Trend Following setup: a 4h channel breakout in the direction of the 4h trend.

The approach with the most published support for FX majors is slow time-series momentum: trade in
the direction the market has been moving, hold for days, accept a low win rate for larger wins.
This is its simplest systematic form on the timeframes the engine already tracks (a Donchian
channel breakout on 4h candles, filtered by the 4h moving-average trend).

Long setup (short is the mirror image):
  1. Trend: on the 4h chart EMA50 is above EMA200.
  2. Breakout: the latest completed 4h candle closed above the highest high of the TREND_CHANNEL_BARS
     4h candles before it.
  3. Fresh: the 4h candle before it had not closed above its own channel (the first close beyond), and
     the signal is acted on within TREND_ENTRY_WINDOW_MINUTES of that 4h candle closing.
  4. Plan: entry at the ask; stop TREND_STOP_ATR x ATR(4h) below the entry; target TREND_TARGET_R x the
     risk. Stop distance and risk:reward must pass the experiment's limits (4h stops are wide: raise
     MAX_STOP_PIPS for this strategy).
"""

from __future__ import annotations

from datetime import timedelta

from app.config.settings import Settings
from app.db.enums import Direction
from app.strategy.base import Condition, StrategyResult, TradePlan
from app.technicals.engine import TechnicalState

SETUP_NAME = "TREND_FOLLOWING"
H4 = timedelta(hours=4)


def evaluate_trend_following(tech: TechnicalState, bid: float, ask: float, settings: Settings) -> StrategyResult:
    s = settings
    h4 = tech.timeframes["H4"]
    n = s.trend_channel_bars
    bars = h4.recent_bars
    atr = h4.atr14 or 0.0
    if len(bars) < n + 2 or atr <= 0 or h4.ema50 is None or h4.ema200 is None:
        return StrategyResult(
            SETUP_NAME, None, False, [Condition("trend", False, "insufficient 4h data")], None, ["INSUFFICIENT_DATA"]
        )

    conditions: list[Condition] = []
    failures: list[str] = []
    last, prev = bars[-1], bars[-2]
    channel = bars[-n - 1 : -1]
    hi, lo = max(b.high for b in channel), min(b.low for b in channel)
    prev_channel = bars[-n - 2 : -2]
    prev_hi, prev_lo = max(b.high for b in prev_channel), min(b.low for b in prev_channel)
    context = {
        "channel_high": round(hi, 6),
        "channel_low": round(lo, 6),
        "channel_bars_h4": n,
        "h4_close": round(last.close, 6),
        "h4_ema50": round(h4.ema50, 6),
        "h4_ema200": round(h4.ema200, 6),
        "atr_h4_pips": tech.pips(atr),
    }

    # 1. Trend from the moving averages.
    if h4.ema50 > h4.ema200:
        trend: Direction | None = Direction.BUY
    elif h4.ema50 < h4.ema200:
        trend = Direction.SELL
    else:
        trend = None
    conditions.append(
        Condition("trend", trend is not None, f"4h EMA50 {h4.ema50:.5f} vs EMA200 {h4.ema200:.5f}"
                  + ("" if trend is None else f": {'up' if trend == Direction.BUY else 'down'}trend"))
    )
    if trend is None:
        failures.append("NO_HTF_TREND")

    # 2. Close beyond the channel, in the trend's direction.
    if last.close > hi:
        direction: Direction | None = Direction.BUY
    elif last.close < lo:
        direction = Direction.SELL
    else:
        direction = None
    if direction is None:
        conditions.append(Condition("channel_breakout", False, f"4h close {last.close:.5f} inside {lo:.5f}-{hi:.5f}"))
        failures.append("NO_CHANNEL_BREAKOUT")
        return StrategyResult(SETUP_NAME, str(trend) if trend else None, False, conditions, None, failures, context)
    long = direction == Direction.BUY
    edge = hi if long else lo
    conditions.append(
        Condition("channel_breakout", True,
                  f"4h close {last.close:.5f} {'above' if long else 'below'} the {n}-candle "
                  f"{'high' if long else 'low'} {edge:.5f}")
    )
    with_trend = trend == direction
    conditions.append(
        Condition("with_trend", with_trend,
                  "breakout in the trend's direction" if with_trend else "breakout against the 4h trend")
    )
    if not with_trend and trend is not None:
        failures.append("HTF_OPPOSES")

    # 3. Fresh: first 4h close beyond the channel, acted on soon after that candle closed.
    first = prev.close <= prev_hi if long else prev.close >= prev_lo
    age = tech.as_of - (last.time + H4)
    in_window = timedelta(0) <= age <= timedelta(minutes=s.trend_entry_window_minutes)
    fresh = first and in_window
    conditions.append(
        Condition("fresh_signal", fresh,
                  ("first close beyond the channel" if first else "the previous 4h candle had already closed beyond it")
                  + f"; 4h candle closed {max(age.total_seconds(), 0) / 60:.0f} min ago "
                  f"(window {s.trend_entry_window_minutes} min)")
    )
    if not fresh:
        failures.append("STALE_SIGNAL")

    # 4. Plan.
    plan = _build_plan(tech, direction, bid, ask, atr, s)
    stop_ok = s.min_stop_pips <= plan.risk_pips <= s.max_stop_pips
    conditions.append(
        Condition("stop_distance", stop_ok, f"{plan.risk_pips} pips (allowed {s.min_stop_pips:g}-{s.max_stop_pips:g})")
    )
    if not stop_ok:
        failures.append("STOP_TOO_WIDE" if plan.risk_pips > s.max_stop_pips else "STOP_TOO_TIGHT")
    rr_ok = round(plan.risk_reward, 3) >= s.min_risk_reward
    conditions.append(
        Condition("risk_reward", rr_ok, f"{plan.risk_reward:.2f}R to {plan.target_source} (min {s.min_risk_reward})")
    )
    if not rr_ok:
        failures.append("POOR_RISK_REWARD")

    candidate = all(c.passed for c in conditions)
    return StrategyResult(SETUP_NAME, str(direction), candidate, conditions, plan, failures, context)


def _build_plan(tech: TechnicalState, direction: Direction, bid: float, ask: float, atr: float, s: Settings) -> TradePlan:
    pip = tech.pip_size
    sign = 1 if direction == Direction.BUY else -1
    entry = ask if sign > 0 else bid
    risk = atr * s.trend_stop_atr
    stop = entry - sign * risk
    target = entry + sign * risk * s.trend_target_r
    return TradePlan(
        direction=str(direction),
        setup=SETUP_NAME,
        entry=round(entry, 6),
        stop_loss=round(stop, 6),
        take_profit=round(target, 6),
        risk_pips=round(risk / pip, 1),
        reward_pips=round(risk * s.trend_target_r / pip, 1),
        risk_reward=round(s.trend_target_r, 2),
        stop_source=f"ATR_H4_{s.trend_stop_atr:g}",
        target_source=f"FIXED_{s.trend_target_r:g}R",
    )
