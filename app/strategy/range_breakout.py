"""Deterministic Range Breakout setup detection and trade planning.

Complements the trend pullback: it trades the regimes the pullback skips (ranges and compression
that resolve into a breakout). Like every strategy it only proposes a fixed plan; the model may
confirm or decline it and the risk engine sizes it and has the final say.

Long setup (short is the mirror image):
  1. Range: the last N completed H1 bars before the latest one span a range whose height is
     between BREAKOUT_MIN_RANGE_ATR and BREAKOUT_MAX_RANGE_ATR times the H1 ATR (not noise, not a
     trend leg).
  2. The range was tested: price visited both its high and its low at least
     BREAKOUT_MIN_TOUCHES times each (separate visits, within a quarter H1 ATR of the edge).
  3. Break: the latest M15 candle closed above the range high by at least
     BREAKOUT_BUFFER_ATR x ATR(M15).
  4. Strength: that candle's body is at least BREAKOUT_MIN_BODY_ATR x ATR(M15) and it closed in
     the upper third of its range.
  5. Fresh: none of the BREAKOUT_FRESH_BARS M15 candles before it closed above the range high
     (no chasing a break that already happened).
  6. The H4 trend is not bearish.
  7. Plan: entry at the ask; stop back inside the range, BREAKOUT_STOP_RANGE_FRAC of its height
     below the range high (at least MIN_STOP_PIPS); target the measured move,
     BREAKOUT_TARGET_RANGE_MULT x the height above the range high, capped at STRATEGY_MAX_TARGET_R.
     Stop distance and risk:reward must pass the experiment's limits.
"""

from __future__ import annotations

from app.config.settings import Settings
from app.db.enums import Direction
from app.market_data.candles import Bar
from app.strategy.base import Condition, StrategyResult, TradePlan
from app.technicals.engine import TechnicalState
from app.technicals.structure import Trend

SETUP_NAME = "RANGE_BREAKOUT"
# How close (in H1 ATR) a bar's high/low must come to the range edge to count as a test of it.
TOUCH_TOLERANCE_ATR = 0.25


def edge_visits(bars: list[Bar], edge: float, tolerance: float, high: bool) -> int:
    """Separate visits to a range edge: runs of consecutive bars that came within ``tolerance``."""
    visits, inside = 0, False
    for b in bars:
        near = b.high >= edge - tolerance if high else b.low <= edge + tolerance
        if near and not inside:
            visits += 1
        inside = near
    return visits


def evaluate_range_breakout(tech: TechnicalState, bid: float, ask: float, settings: Settings) -> StrategyResult:
    s = settings
    h4, h1, m15 = tech.timeframes["H4"], tech.timeframes["H1"], tech.timeframes["M15"]
    n = s.breakout_range_bars
    h1_bars, m15_bars = h1.recent_bars, m15.recent_bars
    atr_h1, atr_m15 = h1.atr14 or 0.0, m15.atr14 or 0.0
    if len(h1_bars) < n + 1 or len(m15_bars) < s.breakout_fresh_bars + 1 or atr_h1 <= 0 or atr_m15 <= 0:
        return StrategyResult(
            SETUP_NAME, None, False, [Condition("range_width", False, "insufficient H1/M15 data")], None,
            ["INSUFFICIENT_DATA"],
        )

    conditions: list[Condition] = []
    failures: list[str] = []

    # 1. Range of the completed H1 bars before the latest one (which may hold the break).
    rng = h1_bars[-n - 1 : -1]
    hi, lo = max(b.high for b in rng), min(b.low for b in rng)
    height = hi - lo
    height_atr = height / atr_h1
    width_ok = s.breakout_min_range_atr <= height_atr <= s.breakout_max_range_atr
    conditions.append(
        Condition(
            "range_width",
            width_ok,
            f"{n} H1 bars {lo:.5f}-{hi:.5f}: {tech.pips(height)} pips = {height_atr:.1f} x ATR(H1) "
            f"(allowed {s.breakout_min_range_atr:g}-{s.breakout_max_range_atr:g})",
        )
    )
    if not width_ok:
        failures.append("RANGE_TOO_NARROW" if height_atr < s.breakout_min_range_atr else "RANGE_TOO_WIDE")

    # 2. Both edges tested.
    tol = atr_h1 * TOUCH_TOLERANCE_ATR
    top, bottom = edge_visits(rng, hi, tol, True), edge_visits(rng, lo, tol, False)
    tested = top >= s.breakout_min_touches and bottom >= s.breakout_min_touches
    conditions.append(
        Condition("range_tested", tested, f"{top} visit(s) to the high, {bottom} to the low (min {s.breakout_min_touches})")
    )
    if not tested:
        failures.append("RANGE_NOT_TESTED")

    context = {
        "range_high": round(hi, 6),
        "range_low": round(lo, 6),
        "range_height_pips": tech.pips(height),
        "range_height_atr": round(height_atr, 2),
        "range_bars_h1": n,
        "visits_high": top,
        "visits_low": bottom,
    }

    # 3. Which edge (if any) the latest M15 candle closed beyond.
    last = m15_bars[-1]
    buffer = atr_m15 * s.breakout_buffer_atr
    if last.close > hi + buffer:
        direction = Direction.BUY
    elif last.close < lo - buffer:
        direction = Direction.SELL
    else:
        conditions.append(
            Condition("breakout_close", False, f"M15 close {last.close:.5f} inside {lo - buffer:.5f}-{hi + buffer:.5f}")
        )
        failures.append("NO_BREAKOUT")
        return StrategyResult(SETUP_NAME, None, False, conditions, None, failures, context)
    long = direction == Direction.BUY
    edge = hi if long else lo
    context["breakout_level"] = round(edge, 6)
    conditions.append(
        Condition(
            "breakout_close", True,
            f"M15 close {last.close:.5f} {'above' if long else 'below'} {edge:.5f} by "
            f"{tech.pips(abs(last.close - edge))} pips (min {tech.pips(buffer)})",
        )
    )

    # 4. Strength of the breaking candle.
    body = (last.close - last.open) if long else (last.open - last.close)
    span = last.high - last.low
    position = ((last.close - last.low) if long else (last.high - last.close)) / span if span > 0 else 0.0
    strong = body >= s.breakout_min_body_atr * atr_m15 and position >= 2 / 3
    conditions.append(
        Condition(
            "breakout_strength", strong,
            f"body {tech.pips(body)} pips (min {tech.pips(s.breakout_min_body_atr * atr_m15)}), "
            f"closed at {position:.0%} of the candle toward the break",
        )
    )
    if not strong:
        failures.append("WEAK_BREAKOUT_CANDLE")

    # 5. Fresh: no earlier M15 close beyond the edge.
    earlier = m15_bars[-s.breakout_fresh_bars - 1 : -1]
    beyond = [b for b in earlier if (b.close > edge if long else b.close < edge)]
    fresh = not beyond
    conditions.append(
        Condition(
            "fresh_breakout", fresh,
            "first close beyond the range" if fresh
            else f"{len(beyond)} of the previous {len(earlier)} M15 closes were already beyond it",
        )
    )
    if not fresh:
        failures.append("LATE_BREAKOUT")

    # 6. Higher timeframe not against the break.
    opposing = Trend.BEARISH if long else Trend.BULLISH
    htf_ok = h4.trend != opposing
    conditions.append(Condition("htf_not_opposing", htf_ok, f"H4 trend is {h4.trend}"))
    if not htf_ok:
        failures.append("HTF_OPPOSES")

    # 7. Plan.
    plan = _build_plan(tech, direction, bid, ask, edge, height, s)
    stop_ok = plan.risk_pips <= s.max_stop_pips
    conditions.append(Condition("stop_distance", stop_ok, f"{plan.risk_pips} pips (max {s.max_stop_pips})"))
    if not stop_ok:
        failures.append("STOP_TOO_WIDE")
    rr_ok = round(plan.risk_reward, 3) >= s.min_risk_reward
    conditions.append(
        Condition("risk_reward", rr_ok, f"{plan.risk_reward:.2f}R to {plan.target_source} (min {s.min_risk_reward})")
    )
    if not rr_ok:
        failures.append("POOR_RISK_REWARD")

    candidate = all(c.passed for c in conditions)
    return StrategyResult(SETUP_NAME, str(direction), candidate, conditions, plan, failures, context)


def _build_plan(
    tech: TechnicalState, direction: Direction, bid: float, ask: float, edge: float, height: float, s: Settings
) -> TradePlan:
    pip = tech.pip_size
    long = direction == Direction.BUY
    sign = 1 if long else -1
    entry = ask if long else bid
    notes: list[str] = []
    stop = edge - sign * height * s.breakout_stop_range_frac
    if (entry - stop) * sign < s.min_stop_pips * pip:
        stop = entry - sign * s.min_stop_pips * pip
        notes.append("stop widened to minimum distance")
    risk = abs(entry - stop)
    target = edge + sign * height * s.breakout_target_range_mult
    target_source = f"MEASURED_MOVE_{s.breakout_target_range_mult:g}X"
    max_target = entry + sign * risk * s.strategy_max_target_r
    if (target - max_target) * sign > 0:
        target = max_target
        target_source = f"CAPPED_{s.strategy_max_target_r:g}R"
    reward = (target - entry) * sign
    return TradePlan(
        direction=str(direction),
        setup=SETUP_NAME,
        entry=round(entry, 6),
        stop_loss=round(stop, 6),
        take_profit=round(target, 6),
        risk_pips=round(risk / pip, 1),
        reward_pips=round(reward / pip, 1),
        risk_reward=round(reward / risk, 2) if risk > 0 else 0.0,
        stop_source=f"INSIDE_RANGE_{s.breakout_stop_range_frac:g}",
        target_source=target_source,
        notes=notes,
    )
