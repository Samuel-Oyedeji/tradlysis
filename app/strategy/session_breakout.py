"""Deterministic London (session) Breakout setup: trade the first break of the overnight range.

FX is quiet overnight (the Asian session) and volatile when London opens. The setup marks the high and
low of the quiet hours and trades the first 15m close beyond them during the London morning. Published
tests of the plain version (fixed-pip stops) lose money; this one sizes the stop from the range, filters
ranges by width in ATR, and targets a fixed multiple of the risk, so each choice can be tested.

All hours are London time (Europe/London, so the session follows British summer time).

Long setup (short is the mirror image):
  1. Range: the high and low of today's completed 1h candles from SESSION_RANGE_START_HOUR to
     SESSION_RANGE_END_HOUR, between SESSION_MIN_RANGE_ATR and SESSION_MAX_RANGE_ATR x ATR(1h).
  2. Session: the latest 15m candle closed between SESSION_RANGE_END_HOUR and SESSION_ENTRY_END_HOUR.
  3. Break: it closed above the range high by at least SESSION_BUFFER_ATR x ATR(15m).
  4. Fresh: no earlier 15m candle since the range ended closed above the range high.
  5. Plan: entry at the ask; stop SESSION_STOP_RANGE_FRAC of the range height back inside the range
     (0.5 = the middle of the range, at least MIN_STOP_PIPS); target SESSION_TARGET_R x the risk.
     Stop distance and risk:reward must pass the experiment's limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config.settings import Settings
from app.db.enums import Direction
from app.strategy.base import Condition, StrategyResult, TradePlan
from app.technicals.engine import TechnicalState

SETUP_NAME = "LONDON_BREAKOUT"
LONDON = ZoneInfo("Europe/London")
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)


def london(t: datetime) -> datetime:
    return t.astimezone(LONDON)


def evaluate_session_breakout(tech: TechnicalState, bid: float, ask: float, settings: Settings) -> StrategyResult:
    s = settings
    h1, m15 = tech.timeframes["H1"], tech.timeframes["M15"]
    atr_h1, atr_m15 = h1.atr14 or 0.0, m15.atr14 or 0.0
    if not m15.recent_bars or not h1.recent_bars or atr_h1 <= 0 or atr_m15 <= 0:
        return StrategyResult(
            SETUP_NAME, None, False, [Condition("session", False, "insufficient 1h/15m data")], None,
            ["INSUFFICIENT_DATA"],
        )
    conditions: list[Condition] = []
    failures: list[str] = []

    # 2. Session window (checked first: outside it nothing else matters).
    last = m15.recent_bars[-1]
    closed_at = london(last.time + M15)
    day = closed_at.date()
    start = datetime(day.year, day.month, day.day, s.session_range_start_hour, tzinfo=LONDON)
    range_end = datetime(day.year, day.month, day.day, s.session_range_end_hour, tzinfo=LONDON)
    entry_end = datetime(day.year, day.month, day.day, s.session_entry_end_hour, tzinfo=LONDON)
    in_session = range_end < closed_at <= entry_end and closed_at.weekday() < 5
    conditions.append(
        Condition("session", in_session,
                  f"15m candle closed {closed_at:%H:%M} London (entries {s.session_range_end_hour:02d}:00-"
                  f"{s.session_entry_end_hour:02d}:00, weekdays)")
    )
    if not in_session:
        failures.append("OUTSIDE_SESSION")
        return StrategyResult(SETUP_NAME, None, False, conditions, None, failures)

    # 1. The overnight range from today's 1h candles.
    rng = [b for b in h1.recent_bars if start <= london(b.time) and london(b.time + H1) <= range_end]
    expected = s.session_range_end_hour - s.session_range_start_hour
    if len(rng) < max(1, expected - 1):  # allow one missing candle (e.g. a broker gap)
        conditions.append(Condition("range_width", False, f"{len(rng)} of {expected} range candles available"))
        failures.append("INSUFFICIENT_DATA")
        return StrategyResult(SETUP_NAME, None, False, conditions, None, failures)
    hi, lo = max(b.high for b in rng), min(b.low for b in rng)
    height = hi - lo
    height_atr = height / atr_h1
    width_ok = s.session_min_range_atr <= height_atr <= s.session_max_range_atr
    conditions.append(
        Condition("range_width", width_ok,
                  f"{s.session_range_start_hour:02d}:00-{s.session_range_end_hour:02d}:00 range {lo:.5f}-{hi:.5f}: "
                  f"{tech.pips(height)} pips = {height_atr:.1f} x ATR(1h) "
                  f"(allowed {s.session_min_range_atr:g}-{s.session_max_range_atr:g})")
    )
    if not width_ok:
        failures.append("RANGE_TOO_NARROW" if height_atr < s.session_min_range_atr else "RANGE_TOO_WIDE")
    context = {
        "range_high": round(hi, 6),
        "range_low": round(lo, 6),
        "range_height_pips": tech.pips(height),
        "range_height_atr": round(height_atr, 2),
        "session_date": day.isoformat(),
    }

    # 3. Break beyond the range.
    buffer = atr_m15 * s.session_buffer_atr
    if last.close > hi + buffer:
        direction = Direction.BUY
    elif last.close < lo - buffer:
        direction = Direction.SELL
    else:
        conditions.append(
            Condition("breakout_close", False, f"15m close {last.close:.5f} inside {lo - buffer:.5f}-{hi + buffer:.5f}")
        )
        failures.append("NO_BREAKOUT")
        return StrategyResult(SETUP_NAME, None, False, conditions, None, failures, context)
    long = direction == Direction.BUY
    edge = hi if long else lo
    context["breakout_level"] = round(edge, 6)
    conditions.append(
        Condition("breakout_close", True,
                  f"15m close {last.close:.5f} {'above' if long else 'below'} {edge:.5f} by "
                  f"{tech.pips(abs(last.close - edge))} pips (min {tech.pips(buffer)})")
    )

    # 4. Fresh: the first close beyond the range since it ended.
    earlier = [b for b in m15.recent_bars[:-1] if london(b.time) >= range_end]
    beyond = [b for b in earlier if (b.close > edge if long else b.close < edge)]
    fresh = not beyond
    conditions.append(
        Condition("fresh_breakout", fresh,
                  "first close beyond the range today" if fresh
                  else f"{len(beyond)} earlier 15m close(s) since {s.session_range_end_hour:02d}:00 were already beyond it")
    )
    if not fresh:
        failures.append("LATE_BREAKOUT")

    # 5. Plan.
    plan = _build_plan(tech, direction, bid, ask, edge, height, s)
    stop_ok = plan.risk_pips <= s.max_stop_pips
    conditions.append(Condition("stop_distance", stop_ok, f"{plan.risk_pips} pips (max {s.max_stop_pips:g})"))
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
    sign = 1 if direction == Direction.BUY else -1
    entry = ask if sign > 0 else bid
    notes: list[str] = []
    stop = edge - sign * height * s.session_stop_range_frac
    if (entry - stop) * sign < s.min_stop_pips * pip:
        stop = entry - sign * s.min_stop_pips * pip
        notes.append("stop widened to minimum distance")
    risk = abs(entry - stop)
    target = entry + sign * risk * s.session_target_r
    return TradePlan(
        direction=str(direction),
        setup=SETUP_NAME,
        entry=round(entry, 6),
        stop_loss=round(stop, 6),
        take_profit=round(target, 6),
        risk_pips=round(risk / pip, 1),
        reward_pips=round(risk * s.session_target_r / pip, 1),
        risk_reward=round(s.session_target_r, 2),
        stop_source=f"INSIDE_RANGE_{s.session_stop_range_frac:g}",
        target_source=f"FIXED_{s.session_target_r:g}R",
        notes=notes,
    )
