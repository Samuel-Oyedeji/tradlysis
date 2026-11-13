"""Deterministic "Structure, liquidity & POI" setup (a smart-money-concepts style method).

Price hunts the stops below a low, then breaks the structure that was against it; the trade is the
first return to the zone the move started from. Read on the structure timeframe (SMC_STRUCTURE_TF,
1h by default); the entry is timed on 15m candles. Like every strategy it only proposes a fixed plan:
the model may confirm or decline it and the risk engine sizes it and has the final say.

Long setup (short is the mirror image):
  1. Sweep: a swing low (the *protected low*) went below the swing low before it.
  2. Main break of structure: after it, a candle closed above the last swing high before it (the
     previous lower high). Smaller breaks inside the down-leg do not count.
  3. Protected: no candle since has traded below the protected low (15m included).
  4. Swing size: from the protected low to the highest high since (the *swing high*) is at least
     SMC_MIN_SWING_ATR x ATR.
  5. Structural liquidity: the lowest internal swing low (a low under its neighbour on each side) inside
     the up-move, between the protected low and the swing high; it sits in the lower SMC_LIQUIDITY_MAX_FRAC of the swing (0.5 = at/below the 50%).
  6. POI, a candle from just before the protected low up to the break; its high-low is the zone.
     SMC_POI_MODE picks it:
       LIQUIDITY: the bearish candle (close < open) whose high is the closest one below the structural
                  liquidity, at most SMC_POI_MAX_GAP_ATR x ATR below it, and in the discount half.
       DISCOUNT:  the bearish candle before the liquidity with the highest low that is still below
                  both the liquidity and the 50% level (the zone may stick out above them).
       ORIGIN:    the last bearish candle at the base of the move (up to SMC_SWING_BARS candles after the
                  protected low): the order block; the protected-low candle itself if there is none.
  7. Entry: the latest 15m candle traded into the POI and closed above its low, and it is the first
     visit to the POI since the break.
  8. With the 1h structure, the 4h trend is not against the trade (SMC_HTF_FILTER).
  9. Plan: entry at the ask; stop SMC_STOP_BUFFER_ATR x ATR below the protected low (at least
     MIN_STOP_PIPS); target the swing high (or SMC_TARGET_R x the risk), capped at STRATEGY_MAX_TARGET_R.
     Stop distance and risk:reward must pass the experiment's limits.

Shorts run the same rules on the price series turned upside down.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.config.settings import Settings
from app.db.enums import Direction
from app.market_data.candles import Bar
from app.strategy.base import Condition, StrategyResult, TradePlan
from app.technicals.engine import TechnicalState
from app.technicals.structure import Swing, Trend, find_swings

SETUP_NAME = "STRUCTURE_POI"
TF_LENGTH = {"H1": timedelta(hours=1), "H4": timedelta(hours=4)}
POI_BEFORE_LOW_BARS = 3  # how many candles before the protected low may hold the POI candle


def flip(bars: list[Bar]) -> list[Bar]:
    """The same candles upside down (price -> -price), so a short is a long on the flipped series."""
    return [Bar(b.time, -b.open, -b.low, -b.high, -b.close) for b in bars]


@dataclass
class _Structure:
    """A bullish swing structure on (possibly flipped) candles; indices are into the structure candles."""

    low: Swing  # the protected low (it swept the swing low before it)
    swept: Swing  # the swing low it swept
    lower_high: Swing  # the high whose break is the main break of structure
    bos_index: int  # first candle that closed above lower_high


def _find_structure(bars: list[Bar], swings: list[Swing], start: int) -> _Structure | None:
    """The most recent sweep + main break of structure whose low is still protected (on completed candles)."""
    lows = [s for s in swings if s.kind == "LOW"]
    highs = [s for s in swings if s.kind == "HIGH"]
    for i in range(len(lows) - 1, 0, -1):
        low, swept = lows[i], lows[i - 1]
        if low.index < start:
            break
        if low.price >= swept.price:
            continue  # no sweep of the previous low
        before = [h for h in highs if h.index < low.index]
        if not before:
            continue
        lower_high = before[-1]
        if any(b.low < low.price for b in bars[low.index + 1 :]):
            continue  # the low has since been broken: not protected
        bos = next((j for j in range(low.index + 1, len(bars)) if bars[j].close > lower_high.price), None)
        if bos is None:
            continue
        return _Structure(low, swept, lower_high, bos)
    return None


def evaluate_structure_poi(tech: TechnicalState, bid: float, ask: float, settings: Settings) -> StrategyResult:
    s = settings
    tf_name = s.smc_structure_tf
    tfs = tech.timeframes[tf_name]
    m15 = tech.timeframes["M15"]
    bars = tfs.bars or tfs.recent_bars
    m15_bars = m15.bars or m15.recent_bars
    atr = tfs.atr14 or 0.0
    n = s.smc_swing_bars
    if len(bars) < 4 * n + 10 or not m15_bars or atr <= 0:
        return StrategyResult(
            SETUP_NAME, None, False, [Condition("structure", False, f"insufficient {tf_name}/M15 data")], None,
            ["INSUFFICIENT_DATA"],
        )

    start = max(0, len(bars) - s.smc_lookback_bars)
    flipped = flip(bars)
    long_s = _find_structure(bars, find_swings(bars, n, n), start)
    short_s = _find_structure(flipped, find_swings(flipped, n, n), start)
    if long_s is None and short_s is None:
        return StrategyResult(
            SETUP_NAME, None, False,
            [Condition("structure", False,
                       f"no sweep + break of structure with a protected low in the last {s.smc_lookback_bars} "
                       f"{tf_name} candles")],
            None, ["NO_STRUCTURE"],
        )
    # The most recent break of structure decides the direction.
    long = short_s is None or (long_s is not None and long_s.bos_index >= short_s.bos_index)
    st = long_s if long else short_s
    assert st is not None
    direction = Direction.BUY if long else Direction.SELL
    sign = 1 if long else -1
    b = bars if long else flipped  # work in "long" space; real price = sign * value
    m = m15_bars if long else flip(m15_bars)
    internal = find_swings(b, 1, 1)  # small swings inside the move: where the liquidity sits

    def real(v: float) -> float:
        return sign * v

    conditions: list[Condition] = []
    failures: list[str] = []
    word = ("low", "high") if long else ("high", "low")
    conditions.append(
        Condition(
            "structure", True,
            f"{tf_name} swing {word[0]} {real(st.low.price):.5f} swept {real(st.swept.price):.5f}; "
            f"close beyond the previous {word[1]} {real(st.lower_high.price):.5f} "
            f"at {b[st.bos_index].time:%Y-%m-%d %H:%M}",
        )
    )

    # Swing extreme (the target side) and size.
    hi_idx = max(range(st.low.index, len(b)), key=lambda j: b[j].high)
    hi = b[hi_idx].high
    height = hi - st.low.price
    size_ok = height >= s.smc_min_swing_atr * atr
    conditions.append(
        Condition("swing_size", size_ok,
                  f"{tech.pips(height)} pips = {height / atr:.1f} x ATR({tf_name}) (min {s.smc_min_swing_atr:g})")
    )
    if not size_ok:
        failures.append("SWING_TOO_SMALL")
    eq = st.low.price + height / 2

    # Protected: nothing since (completed structure candles were checked in _find_structure; add 15m).
    step = TF_LENGTH[tf_name]
    last = m[-1]
    done = [j for j in range(st.bos_index + 1, len(b)) if b[j].time + step <= last.time]
    cutoff = (b[done[-1]].time if done else b[st.bos_index].time) + step
    recent_m15 = [x for x in m if x.time >= cutoff]
    protected = all(x.low > st.low.price for x in recent_m15)
    conditions.append(
        Condition("protected", protected,
                  f"{word[0]} {real(st.low.price):.5f} " + ("intact" if protected else "broken on 15m"))
    )
    if not protected:
        failures.append("STRUCTURE_BROKEN")

    context = {
        "structure_timeframe": tf_name,
        "protected_level": round(real(st.low.price), 6),
        "swept_level": round(real(st.swept.price), 6),
        "broken_level": round(real(st.lower_high.price), 6),
        "break_time": b[st.bos_index].time.isoformat(),
        "swing_extreme": round(real(hi), 6),
        "equilibrium_50pct": round(real(eq), 6),
    }

    # Structural liquidity: the deepest (internal) swing low inside the move.
    inside = [w for w in internal if w.kind == "LOW" and st.low.index < w.index < hi_idx]
    liq = min(inside, key=lambda w: w.price) if inside else None
    if liq is None:
        conditions.append(Condition("liquidity", False, f"no swing {word[0]} inside the move"))
        failures.append("NO_LIQUIDITY")
    else:
        frac = (liq.price - st.low.price) / height if height > 0 else 1.0
        liq_ok = frac <= s.smc_liquidity_max_frac
        conditions.append(
            Condition("liquidity", liq_ok,
                      f"swing {word[0]} {real(liq.price):.5f} at {frac:.0%} of the swing "
                      f"(max {s.smc_liquidity_max_frac:.0%})")
        )
        if not liq_ok:
            failures.append("LIQUIDITY_TOO_HIGH" if long else "LIQUIDITY_TOO_LOW")
        context["liquidity_level"] = round(real(liq.price), 6)

    # POI (SMC_POI_MODE): LIQUIDITY = the bearish candle just below the liquidity, in the discount half;
    # DISCOUNT = the highest bearish candle that reaches into the discount half below the liquidity;
    # ORIGIN = the last bearish candle at the base of the move (the order block; else the protected-low candle).
    poi: Bar | None = None
    if liq is not None:
        poi = _pick_poi(b, st, liq, eq, atr, s)
        if poi is None:
            kind, half = ("bearish", "discount") if long else ("bullish", "premium")
            detail = (f"no {kind} candle in the {half} half within {s.smc_poi_max_gap_atr:g} ATR of the liquidity"
                      if s.smc_poi_mode == "LIQUIDITY" else f"no {kind} candle reaching the {half} half")
            conditions.append(Condition("poi", False, detail))
            failures.append("NO_POI")
        else:
            zone = sorted((real(poi.low), real(poi.high)))
            conditions.append(
                Condition("poi", True, f"{tf_name} candle {poi.time:%Y-%m-%d %H:%M} zone {zone[0]:.5f}-{zone[1]:.5f}"
                          f" ({s.smc_poi_mode.lower()})")
            )
            context["poi_low"], context["poi_high"] = round(zone[0], 6), round(zone[1], 6)

    # Entry: the latest 15m candle taps the POI for the first time and closes back above its low.
    if poi is not None:
        earlier = any(b[j].low <= poi.high for j in done) or any(
            x.low <= poi.high for x in recent_m15 if x.time < last.time
        )
        fresh = not earlier
        conditions.append(
            Condition("first_tap", fresh, "first return to the POI" if fresh else "the POI was already visited")
        )
        if not fresh:
            failures.append("POI_ALREADY_TAPPED")
        tapped = last.low <= poi.high and last.close >= poi.low
        conditions.append(
            Condition("at_poi", tapped,
                      f"15m {word[0]} {real(last.low):.5f}, close {real(last.close):.5f}"
                      + ("" if tapped else " (not in the POI)" if last.low > poi.high else " (closed through it)"))
        )
        if not tapped:
            failures.append("NOT_AT_POI" if last.low > poi.high else "POI_BROKEN")

    # Higher timeframe (top-down) filter.
    if tf_name == "H1" and s.smc_htf_filter:
        h4 = tech.timeframes["H4"].trend
        htf_ok = h4 != (Trend.BEARISH if long else Trend.BULLISH)
        conditions.append(Condition("htf_not_opposing", htf_ok, f"H4 trend is {h4}"))
        if not htf_ok:
            failures.append("HTF_OPPOSES")

    plan: TradePlan | None = None
    if poi is not None:
        plan = _build_plan(tech, direction, bid, ask, real(st.low.price), real(hi), atr, s)
        stop_ok = s.min_stop_pips <= plan.risk_pips <= s.max_stop_pips
        conditions.append(
            Condition("stop_distance", stop_ok,
                      f"{plan.risk_pips} pips (allowed {s.min_stop_pips:g}-{s.max_stop_pips:g})")
        )
        if not stop_ok:
            failures.append("STOP_TOO_WIDE" if plan.risk_pips > s.max_stop_pips else "STOP_TOO_TIGHT")
        rr_ok = round(plan.risk_reward, 3) >= s.min_risk_reward
        conditions.append(
            Condition("risk_reward", rr_ok,
                      f"{plan.risk_reward:.2f}R to {plan.target_source} (min {s.min_risk_reward})")
        )
        if not rr_ok:
            failures.append("POOR_RISK_REWARD")

    candidate = plan is not None and all(c.passed for c in conditions)
    return StrategyResult(SETUP_NAME, str(direction), candidate, conditions, plan, failures, context)


def _pick_poi(b: list[Bar], st: _Structure, liq: Swing, eq: float, atr: float, s: Settings) -> Bar | None:
    """The POI candle (long space) for SMC_POI_MODE, or None."""
    lo_i = max(0, st.low.index - POI_BEFORE_LOW_BARS)
    bearish = [j for j in range(lo_i, st.bos_index + 1) if b[j].close < b[j].open]
    if s.smc_poi_mode == "ORIGIN":
        base = [j for j in bearish if j <= st.low.index + s.smc_swing_bars]
        return b[base[-1]] if base else b[st.low.index]
    if s.smc_poi_mode == "DISCOUNT":
        cands = [b[j] for j in bearish if j < liq.index and b[j].low < liq.price and b[j].low <= eq]
        return max(cands, key=lambda x: (x.low, x.time)) if cands else None
    cands = [
        b[j] for j in bearish
        if b[j].high <= min(liq.price, eq) and liq.price - b[j].high <= s.smc_poi_max_gap_atr * atr
    ]
    return max(cands, key=lambda x: (x.high, x.time)) if cands else None


def _build_plan(
    tech: TechnicalState, direction: Direction, bid: float, ask: float, protected: float, extreme: float,
    atr: float, s: Settings,
) -> TradePlan:
    pip = tech.pip_size
    sign = 1 if direction == Direction.BUY else -1
    entry = ask if sign > 0 else bid
    notes: list[str] = []
    stop = protected - sign * atr * s.smc_stop_buffer_atr
    if (entry - stop) * sign < s.min_stop_pips * pip:
        stop = entry - sign * s.min_stop_pips * pip
        notes.append("stop widened to minimum distance")
    risk = abs(entry - stop)
    if s.smc_target_r > 0:
        target, source = entry + sign * risk * s.smc_target_r, f"FIXED_{s.smc_target_r:g}R"
    else:
        target, source = extreme, "SWING_HIGH" if sign > 0 else "SWING_LOW"
    cap = entry + sign * risk * s.strategy_max_target_r
    if (target - cap) * sign > 0:
        target, source = cap, f"CAPPED_{s.strategy_max_target_r:g}R"
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
        stop_source="BEYOND_PROTECTED_" + ("LOW" if sign > 0 else "HIGH"),
        target_source=source,
        notes=notes,
    )
