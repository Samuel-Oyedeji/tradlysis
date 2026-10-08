"""Backtester: replay months of Capital.com history through an experiment's deterministic rules.

    python -m app.backtest                                   # every enabled experiment, last 90 days
    python -m app.backtest --experiment gbpusd-breakout --days 180
    python -m app.backtest --experiment eurusd-breakout --vary breakout_min_touches=1,2
    python -m app.backtest --set min_risk_reward=1.5 --trades

It answers "how often would this experiment trade, and how would those trades have done?" fast,
instead of waiting weeks for live data. For every 15-minute candle it builds the same technical
state the engine builds (``compute_technical_state`` on the candles complete at that time), runs
the experiment's strategy, and sends every setup through the real risk engine (``risk.evaluate``:
spread limit, stop distance, R:R, sizing, cooldown, loss breakers) with the experiment's capital as
a simulated account. Approved trades fill at the candle's closing bid/ask and run to their stop or
target on the following candles.

What it leaves out (so live results will differ):
  * the model: every setup is treated as if the model agreed (live, it may answer WAIT);
  * the news blackout (no historical calendar);
  * a stop and a target touched in the same 15-minute candle count as a loss (conservative),
    and the spread is held at its value when the trade opened.

It only reads: candles from the broker (never orders) and the configuration from the database.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import itertools
import json
import secrets
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

import app.engine as engine_mod
from app.broker.types import InstrumentInfo
from app.config.settings import Settings, get_settings
from app.config.store import (
    ENV_ONLY_KEYS,
    FIELD_BY_KEY,
    SECRET_KEYS,
    ConfigError,
    Configuration,
    ExperimentConfig,
    _error_text,
    load_configuration,
    single_experiment,
)
from app.market_data.candles import Bar
from app.market_data.timeutil import GRANULARITY_SECONDS, is_fx_market_open, trading_day, utcnow
from app.risk.conversion import conversion_rates
from app.risk.engine import OpenTradeRisk, RiskContext, evaluate
from app.strategy.base import StrategyResult
from app.technicals.engine import compute_technical_state

MAX_DAYS = 365
MAX_VARIANTS = 12
DEFAULT_CAPITAL = Decimal("10000")
# History needed before the first replayed candle for the engine's 300-bar windows (with weekends).
WARMUP = {"M15": timedelta(days=7), "H1": timedelta(days=21), "H4": timedelta(days=80),
          "D": timedelta(days=120), "W": timedelta(days=40)}
MIN_BARS = 210  # the engine skips a cycle with fewer M15/H1/H4 bars
# Keys a variant may not change: bootstrap values, secrets and what defines the experiment.
FIXED_KEYS = frozenset(ENV_ONLY_KEYS) | SECRET_KEYS | {"instrument", "experiment_name"}


# ---------------------------------------------------------------------- simulation


@dataclass
class SimTrade:
    direction: str
    opened: datetime
    entry: float
    stop_loss: float
    take_profit: float
    units: int
    half_spread: float
    risk_pips: float
    closed: datetime | None = None
    exit: float | None = None
    outcome: str = "OPEN"  # TAKE_PROFIT | STOP_LOSS | OPEN (still open when the replay ended)
    r: float = 0.0
    pnl: float = 0.0  # account currency
    best_r: float = 0.0  # max favourable excursion
    worst_r: float = 0.0  # max adverse excursion
    ambiguous: bool = False  # stop and target in the same candle (counted as the stop)

    @property
    def long(self) -> bool:
        return self.direction == "BUY"

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    def r_at(self, price: float) -> float:
        return ((price - self.entry) if self.long else (self.entry - price)) / self.risk if self.risk else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction, "opened": self.opened.isoformat(), "entry": self.entry,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit, "units": self.units,
            "risk_pips": self.risk_pips, "closed": self.closed.isoformat() if self.closed else None,
            "exit": self.exit, "outcome": self.outcome, "r": round(self.r, 2), "pnl": round(self.pnl, 2),
            "best_r": round(self.best_r, 2), "worst_r": round(self.worst_r, 2), "ambiguous": self.ambiguous,
        }


def step_trade(t: SimTrade, bar: Bar) -> tuple[str, float] | None:
    """Advance an open trade through one mid-price candle. Returns (outcome, exit price) if it closed.

    A long closes at the bid (mid - half spread), a short at the ask. A candle that opens beyond a
    level fills at its open (gap); one that touches both the stop and the target counts as the stop.
    """
    hs = -t.half_spread if t.long else t.half_spread
    o, h, lo = bar.open + hs, bar.high + hs, bar.low + hs
    if t.long:
        if o <= t.stop_loss:
            return "STOP_LOSS", o
        if o >= t.take_profit:
            return "TAKE_PROFIT", o
        hit_stop, hit_target = lo <= t.stop_loss, h >= t.take_profit
        best, worst = h, lo
    else:
        if o >= t.stop_loss:
            return "STOP_LOSS", o
        if o <= t.take_profit:
            return "TAKE_PROFIT", o
        hit_stop, hit_target = h >= t.stop_loss, lo <= t.take_profit
        best, worst = lo, h
    if hit_stop:
        t.ambiguous = hit_target
        return "STOP_LOSS", t.stop_loss
    if hit_target:
        return "TAKE_PROFIT", t.take_profit
    t.best_r = max(t.best_r, t.r_at(best))
    t.worst_r = min(t.worst_r, t.r_at(worst))
    return None


@dataclass
class Run:
    """One experiment (or one variant of it) being replayed, with its simulated account."""

    label: str
    slug: str
    strategy: str
    instrument: str
    settings: Settings
    capital: Decimal
    variant: dict[str, str] = field(default_factory=dict)
    # simulated account
    balance: Decimal = Decimal(0)
    peak: Decimal = Decimal(0)
    day: Any = None
    day_start: Decimal = Decimal(0)
    daily_halt: Any = None  # trading day the daily-loss breaker stopped
    position: SimTrade | None = None
    last_entry: dict[str, datetime] = field(default_factory=dict)
    # results
    cycles: int = 0
    setups: int = 0
    rule_failures: Counter[str] = field(default_factory=Counter)
    near_misses: Counter[str] = field(default_factory=Counter)
    risk_blocks: Counter[str] = field(default_factory=Counter)
    trades: list[SimTrade] = field(default_factory=list)
    breaker_trips: list[str] = field(default_factory=list)
    first: datetime | None = None
    last: datetime | None = None

    def __post_init__(self) -> None:
        self.balance = self.peak = self.day_start = self.capital

    def nav(self, mid: float, quote_rate: float | None) -> Decimal:
        t = self.position
        if t is None:
            return self.balance
        price = mid - t.half_spread if t.long else mid + t.half_spread
        upl = ((price - t.entry) if t.long else (t.entry - price)) * t.units * (quote_rate or 0)
        return self.balance + Decimal(str(round(upl, 6)))

    def close(self, outcome: str, price: float, when: datetime, quote_rate: float | None) -> None:
        t = self.position
        assert t is not None
        t.outcome, t.exit, t.closed = outcome, price, when
        t.r = t.r_at(price)
        t.pnl = ((price - t.entry) if t.long else (t.entry - price)) * t.units * (quote_rate or 0)
        self.balance += Decimal(str(round(t.pnl, 6)))
        self.peak = max(self.peak, self.balance)
        self.position = None


@dataclass
class History:
    instrument: str
    info: InstrumentInfo
    bars: dict[str, list[Bar]]  # M15, H1, H4, D, W, M ascending
    problems: list[str] = field(default_factory=list)  # history windows the broker refused


def _ends(granularity: str, bars: list[Bar]) -> list[datetime]:
    """When each candle completes (a month ends at the next month's start)."""
    if granularity == "M":
        return [datetime(b.time.year + b.time.month // 12, b.time.month % 12 + 1, 1, tzinfo=UTC) for b in bars]
    d = timedelta(seconds=GRANULARITY_SECONDS[granularity])
    return [b.time + d for b in bars]


def replay(
    history: History,
    runs: list[Run],
    start: datetime,
    end: datetime,
    *,
    account_currency: str,
    cross_rates: dict[str, float] | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Replay every M15 candle closing in (start, end] for ``runs`` (all on ``history.instrument``)."""
    bars = history.bars
    ends = {g: _ends(g, b) for g, b in bars.items()}
    counts = engine_mod.BARS_PER_TF
    m15 = bars["M15"]
    step = timedelta(minutes=15)
    total = max(1, sum(1 for b in m15 if start < b.time + step <= end))
    done = 0
    for i, bar in enumerate(m15):
        now = bar.time + step  # the engine decides once this candle has closed
        if now <= start:
            continue
        if now > end:
            break
        done += 1
        if progress and done % 200 == 0:
            progress(done / total)
        mid = bar.close
        bid, ask = bar.bid_close or mid, bar.ask_close or mid
        quote_rate, base_rate = conversion_rates(account_currency, history.instrument, mid, cross_rates)

        # Open trades run through this candle first (they were opened at an earlier close).
        for run in runs:
            t = run.position
            if t is not None and t.opened < now:
                hit = step_trade(t, bar)
                if hit:
                    run.close(hit[0], hit[1], now, quote_rate)

        if not is_fx_market_open(now - timedelta(seconds=1)):
            continue
        window: dict[str, list[Bar]] = {}
        for g, n in counts.items():
            k = bisect.bisect_right(ends[g], now) if g != "M15" else i + 1
            window[g] = bars[g][max(0, k - n) : k]
        if min(len(window[g]) for g in ("M15", "H1", "H4")) < MIN_BARS:
            continue
        tech = compute_technical_state(history.instrument, window, mid, history.info.pip_size, now)

        for run in runs:
            nav = run.nav(mid, quote_rate)
            day = trading_day(now)
            if day != run.day:
                run.day, run.day_start = day, nav
            run.peak = max(run.peak, nav)
            run.cycles += 1
            run.first = run.first or now
            run.last = now
            result = engine_mod.evaluate_strategy(run.strategy, tech, bid, ask, run.settings)
            if not result.candidate:
                run.rule_failures.update(result.failure_codes)
                failed = [c.name for c in result.conditions if not c.passed]
                if len(failed) == 1:
                    run.near_misses[failed[0]] += 1
                continue
            run.setups += 1
            _try_entry(run, history.info, result, now, bid, ask, nav, quote_rate, base_rate, account_currency)
    if progress:
        progress(1.0)


def _try_entry(
    run: Run, info: InstrumentInfo, result: StrategyResult, now: datetime, bid: float, ask: float,
    nav: Decimal, quote_rate: float | None, base_rate: float | None, account_currency: str,
) -> None:
    plan = result.trade_plan
    assert plan is not None
    t = run.position
    ctx = RiskContext(
        now=now, decision=plan.direction, decision_valid=True, confidence=1.0, trade_plan=plan,
        strategy_candidate=True, kill_switch_active=False, kill_switch_reason="",
        daily_breaker_tripped=run.daily_halt == run.day, drawdown_breaker_tripped=False,
        market_open=True, stream_connected=True, bid=bid, ask=ask, price_time=now, tradeable=True,
        instrument=info, account_currency=account_currency, nav=nav, balance=run.balance,
        margin_available=Decimal(10**12), account_state_time=now, day_start_nav=run.day_start,
        peak_nav=run.peak, quote_home_rate=quote_rate, base_home_rate=base_rate,
        open_trades=[OpenTradeRisk(info.name, t.units if t.long else -t.units, t.entry, t.stop_loss)] if t else [],
        last_entry_same_direction_at=run.last_entry.get(plan.direction),
    )
    risk = evaluate(ctx, run.settings)
    if risk.trip_daily_breaker and run.daily_halt != run.day:
        run.daily_halt = run.day
        run.breaker_trips.append(f"{now:%Y-%m-%d %H:%M} daily loss limit")
    if risk.trip_drawdown_breaker:
        # Live, this halts the experiment until someone resets it; here it is noted and re-based.
        run.breaker_trips.append(f"{now:%Y-%m-%d %H:%M} drawdown limit (re-based, as if reset)")
        run.peak = nav
    if not risk.approved:
        run.risk_blocks.update(risk.rejection_reasons)
        return
    assert risk.entry is not None and risk.stop_loss is not None and risk.take_profit is not None
    assert risk.units is not None
    run.position = SimTrade(
        direction=plan.direction, opened=now, entry=risk.entry, stop_loss=risk.stop_loss,
        take_profit=risk.take_profit, units=abs(risk.units), half_spread=(ask - bid) / 2,
        risk_pips=round(abs(risk.entry - risk.stop_loss) / info.pip_size, 1),
    )
    run.trades.append(run.position)
    run.last_entry[plan.direction] = now


# ---------------------------------------------------------------------- results


CHECK_FRACTION = 1 / 3  # the last third of a backtest is the check period
QUARTERS = 4


def segment(trades: list[SimTrade]) -> dict[str, Any]:
    """Results of the closed trades among ``trades``."""
    closed = [t for t in trades if t.outcome != "OPEN"]
    wins = [t for t in closed if t.r > 0]
    loss = -sum(t.r for t in closed if t.r <= 0)
    return {
        "trades": len(closed),
        "total_r": round(sum(t.r for t in closed), 2),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "profit_factor": round(sum(t.r for t in wins) / loss, 2) if loss else None,
    }


def periods(run: Run, start: datetime, end: datetime, split: datetime) -> dict[str, Any]:
    """Out-of-sample view: the tuning period (before ``split``), the check period (after) and quarters.

    Trades belong to the period they were opened in. Settings should be chosen on the tuning period
    only; the check period then shows whether the choice holds on data it was not chosen on.
    """
    step = (end - start) / QUARTERS
    quarters = []
    for i in range(QUARTERS):
        lo, hi = start + step * i, start + step * (i + 1)
        quarters.append({"start": lo.isoformat(), **segment([t for t in run.trades if lo <= t.opened < hi])})
    return {
        "tuning": segment([t for t in run.trades if t.opened < split]),
        "check": segment([t for t in run.trades if t.opened >= split]),
        "quarters": quarters,
        "positive_quarters": sum(1 for q in quarters if q["total_r"] > 0),
    }


def selection(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each experiment backtested with several variants: the best on the tuning period, and how
    it then did on the check period (its rank among the variants there)."""
    out = []
    for slug in dict.fromkeys(r["experiment"] for r in runs):
        group = [r for r in runs if r["experiment"] == slug]
        if len(group) < 2:
            continue
        best = max(group, key=lambda r: r["tuning"]["total_r"])
        by_check = sorted(group, key=lambda r: -r["check"]["total_r"])
        out.append({
            "experiment": slug,
            "label": best["label"],
            "variant": best["variant"],
            "tuning_r": best["tuning"]["total_r"],
            "check_r": best["check"]["total_r"],
            "check_trades": best["check"]["trades"],
            "check_rank": by_check.index(best) + 1,
            "variants": len(group),
            "holds": best["check"]["total_r"] > 0 and by_check.index(best) < (len(group) + 1) // 2,
            # holds: still profitable and among the better half; mixed: profitable but others did better;
            # fails: lost money on the period it was not chosen on
            "outcome": "fails" if best["check"]["total_r"] <= 0
            else "holds" if by_check.index(best) < (len(group) + 1) // 2 else "mixed",
        })
    return out


def summarize(run: Run, days: int) -> dict[str, Any]:
    closed = [t for t in run.trades if t.outcome != "OPEN"]
    wins = [t for t in closed if t.r > 0]
    gross_win = sum(t.r for t in wins)
    gross_loss = -sum(t.r for t in closed if t.r <= 0)
    cum = peak = max_dd = 0.0
    streak = worst_streak = 0
    for t in closed:
        cum += t.r
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        streak = streak + 1 if t.r <= 0 else 0
        worst_streak = max(worst_streak, streak)
    pnl = sum(t.pnl for t in closed)
    hours = [(t.closed - t.opened).total_seconds() / 3600 for t in closed if t.closed]
    return {
        "label": run.label,
        "experiment": run.slug,
        "instrument": run.instrument,
        "strategy": run.strategy,
        "variant": run.variant,
        "capital": float(run.capital),
        "risk_per_trade_pct": run.settings.risk_per_trade_pct,
        "cycles": run.cycles,
        "first": run.first.isoformat() if run.first else None,
        "last": run.last.isoformat() if run.last else None,
        "setups": run.setups,
        "trades": len(run.trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed), 3) if closed else None,
        "total_r": round(sum(t.r for t in closed), 2),
        "avg_r": round(sum(t.r for t in closed) / len(closed), 2) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "max_drawdown_r": round(max_dd, 2),
        "worst_losing_streak": worst_streak,
        "pnl": round(pnl, 2),
        "return_pct": round(pnl / float(run.capital) * 100, 2) if run.capital else None,
        "trades_per_week": round(len(run.trades) / (days / 7), 2) if days else None,
        "avg_hours_held": round(sum(hours) / len(hours), 1) if hours else None,
        "ambiguous_candles": sum(1 for t in closed if t.ambiguous),
        "rule_failures": dict(run.rule_failures.most_common()),
        "near_misses": dict(run.near_misses.most_common()),
        "risk_blocks": dict(run.risk_blocks.most_common()),
        "breaker_trips": run.breaker_trips,
        "trade_list": [t.to_dict() for t in run.trades],
    }


def _top(counter: dict[str, int], n: int = 6) -> str:
    return ", ".join(f"{k} {v}" for k, v in list(counter.items())[:n]) or "none"


def _fmt(v: float | None, spec: str = "", none: str = "–") -> str:
    return none if v is None else format(v, spec)


def render(result: dict[str, Any], *, trades: bool = False) -> str:
    """The backtest as text."""
    head = (
        f"Tradlysis backtest · {result['start'][:16]} → {result['end'][:16]} UTC ({result['days']} days) · "
        "rules + risk engine only (no model, no news blackout)"
    )
    lines = [head]
    for instrument, tfs in result.get("history", {}).items():
        lines.append(f"  {instrument} candles: " + ", ".join(
            f"{g} {v['candles']} from {(v['first'] or '–')[:10]}" for g, v in tfs.items() if isinstance(v, dict)
        ))
    for w in result.get("warnings", []):
        lines.append(f"  WARNING: {w}")
    lines.append("")
    runs = result["runs"]
    split = (result.get("split") or "")[:10]
    if len({r["experiment"] for r in runs}) < len(runs):
        lines.append(f"Variants (tuning = before {split}, check = from {split}; choose on tuning, trust the check):")
        lines.append(f"  {'run':<58} {'trades':>6} {'/wk':>5} {'win%':>5} {'total R':>8} {'PF':>5} {'maxDD R':>8}"
                     f" {'tuning R':>9} {'check R':>8}")
        for r in runs:
            win = _fmt(r["win_rate"] and r["win_rate"] * 100, ".0f")
            lines.append(
                f"  {r['label'][:58]:<58} {r['trades']:>6} {_fmt(r['trades_per_week'], '.1f'):>5} {win:>5} "
                f"{r['total_r']:>+8.1f} {_fmt(r['profit_factor'], '.2f'):>5} {r['max_drawdown_r']:>8.1f}"
                + (f" {r['tuning']['total_r']:>+9.1f} {r['check']['total_r']:>+8.1f}" if "tuning" in r else "")
            )
        for sel in result.get("selection", []):
            lines.append(
                f"  → best on the tuning period: {sel['label']} ({sel['tuning_r']:+.1f}R); on the check period "
                f"{sel['check_r']:+.1f}R over {sel['check_trades']} trades, #{sel['check_rank']} of {sel['variants']}: "
                + {"holds": "holds up",
                   "mixed": "still profitable, but another variant did better there (the choice is likely luck)",
                   "fails": "does NOT hold up (likely luck)"}[sel.get("outcome", "holds" if sel["holds"] else "fails")]
            )
        lines.append("")
    for r in runs:
        lines.append(f"■ {r['label']}  ({r['instrument']}, {r['strategy']})")
        lines.append(f"  cycles {r['cycles']} · setups {r['setups']} · trades {r['trades']} "
                     f"({_fmt(r['trades_per_week'], '.1f')}/week)")
        if r["closed"]:
            lines.append(
                f"  results: {r['wins']} won / {r['losses']} lost ({r['win_rate'] * 100:.0f}%) · total {r['total_r']:+.1f}R · "
                f"avg {r['avg_r']:+.2f}R · profit factor {_fmt(r['profit_factor'], '.2f', '∞')} · "
                f"max drawdown {r['max_drawdown_r']:.1f}R · worst losing streak {r['worst_losing_streak']}"
            )
            lines.append(
                f"  money: {r['pnl']:+.2f} on {r['capital']:g} ({r['return_pct']:+.2f}%, "
                f"{r['risk_per_trade_pct']}% risk per trade) · avg held {_fmt(r['avg_hours_held'], '.1f')}h"
                + (f" · {r['ambiguous_candles']} stop+target candle(s) counted as losses" if r["ambiguous_candles"] else "")
            )
        if "quarters" in r:
            lines.append(
                f"  out of sample: tuning {r['tuning']['total_r']:+.1f}R ({r['tuning']['trades']} trades) · check "
                f"{r['check']['total_r']:+.1f}R ({r['check']['trades']} trades) · by quarter "
                + " ".join(f"{q['total_r']:+.1f}" for q in r["quarters"])
                + f" (positive in {r['positive_quarters']} of {len(r['quarters'])})"
            )
        lines.append(f"  rules that blocked: {_top(r['rule_failures'])}")
        lines.append(f"  near misses (one rule failed): {_top(r['near_misses'])}")
        if r["risk_blocks"]:
            lines.append(f"  setups the risk engine refused: {_top(r['risk_blocks'])}")
        for trip in r["breaker_trips"]:
            lines.append(f"  breaker: {trip}")
        if trades:
            for t in r["trade_list"]:
                lines.append(
                    f"    {t['opened'][:16]} {t['direction']:<4} {t['entry']} sl {t['stop_loss']} tp {t['take_profit']} "
                    f"({t['risk_pips']} pips) → {t['outcome']} {t['r']:+.2f}R {t['pnl']:+.2f}"
                    f" (best {t['best_r']:+.1f}R, worst {t['worst_r']:+.1f}R)"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


# ---------------------------------------------------------------------- inputs


def parse_assignments(items: list[str], *, multi: bool) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip().lower()
        if not sep or not key:
            raise ConfigError(f"expected key=value, got {item!r}")
        if key in FIXED_KEYS or key not in Settings.model_fields:
            raise ConfigError(f"{key!r} is not a setting a backtest can change")
        out[key] = [v.strip() for v in value.split(",")] if multi else value.strip()
    return out


def variants(vary: dict[str, list[str]]) -> list[dict[str, str]]:
    if not vary:
        return [{}]
    keys = list(vary)
    combos = [dict(zip(keys, values, strict=True)) for values in itertools.product(*(vary[k] for k in keys))]
    if len(combos) > MAX_VARIANTS:
        raise ConfigError(f"{len(combos)} variants; at most {MAX_VARIANTS}")
    return combos


def applies(key: str, strategy: str) -> bool:
    """Whether a setting matters to a strategy (strategy-specific settings name their strategies)."""
    info = FIELD_BY_KEY.get(key)
    return info is None or not info.strategies or strategy in info.strategies


def build_runs(
    experiments: list[ExperimentConfig], sets: dict[str, str], vary: dict[str, list[str]]
) -> list[Run]:
    runs = []
    for exp in experiments:
        # Settings only some strategies use (e.g. the London hours) leave the others' runs alone.
        mine_set = {k: v for k, v in sets.items() if applies(k, exp.strategy)}
        mine_vary = {k: v for k, v in vary.items() if applies(k, exp.strategy)}
        for variant in variants(mine_vary):
            changes = {**mine_set, **variant}
            values = exp.settings.model_dump()
            values.update(changes)
            values["trading_enabled"] = True  # the question is what the rules would do
            try:
                settings = Settings.model_validate(values)
            except ValidationError as exc:
                raise ConfigError(f"{exp.slug}: {_error_text(exc)}") from None
            label = exp.slug + (" [" + ", ".join(f"{k}={v}" for k, v in changes.items()) + "]" if changes else "")
            runs.append(Run(label, exp.slug, exp.strategy, exp.instrument, settings,
                            exp.capital or DEFAULT_CAPITAL, dict(changes)))
    return runs


def select_experiments(config: Configuration, slugs: list[str] | None) -> list[ExperimentConfig]:
    experiments = config.experiments or single_experiment(config.settings).experiments
    if not slugs:
        chosen = [e for e in experiments if e.enabled] or experiments
    else:
        known = {e.slug: e for e in experiments}
        missing = [s for s in slugs if s not in known]
        if missing:
            raise ConfigError(f"unknown experiment(s): {', '.join(missing)} (known: {', '.join(known)})")
        chosen = [known[s] for s in slugs]
    return chosen


async def fetch_history(client: Any, instrument: str, start: datetime, end: datetime) -> History:
    from app.broker.capital import monthly_bars

    info = await client.get_instrument(instrument)
    bars = {}
    problems: list[str] = []
    for g in ("M15", "H1", "H4", "D", "W"):
        got = await client.get_candles_between(g, start - WARMUP[g], end, instrument, problems)
        bars[g] = [b for b in got if b.time + timedelta(seconds=GRANULARITY_SECONDS[g]) <= end]
    bars["M"] = monthly_bars(bars["D"], end)
    return History(instrument, info, bars, problems)


def history_warnings(instrument: str, runs: list[Run], start: datetime, problems: list[str]) -> list[str]:
    """Say plainly when the replay could not cover the period (so "no trades" is never a data gap)."""
    out = []
    name = instrument.replace("_", "/")
    first = min((r.first for r in runs if r.first), default=None)
    if first is None:
        out.append(f"{name}: no candle could be replayed: the broker returned too little history "
                   f"(the engine needs {MIN_BARS} candles on M15, H1 and H4 before each one).")
    elif first - start > timedelta(days=3):
        out.append(f"{name}: the replay starts on {first:%Y-%m-%d}, not {start:%Y-%m-%d}: "
                   "the broker returned no earlier history.")
    if problems:
        out.append(f"{name}: the broker refused {len(problems)} history request(s), e.g. {problems[0]}.")
    return out


# ---------------------------------------------------------------------- entry points


async def backtest(
    config: Configuration,
    client: Any,
    *,
    slugs: list[str] | None = None,
    days: int = 90,
    sets: dict[str, str] | None = None,
    vary: dict[str, list[str]] | None = None,
    end: datetime | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Run the backtest and return the results (``render`` turns them into text)."""
    if not 1 <= days <= MAX_DAYS:
        raise ConfigError(f"days must be 1-{MAX_DAYS}")
    runs = build_runs(select_experiments(config, slugs), sets or {}, vary or {})
    end = end or utcnow()
    end = end.replace(minute=end.minute - end.minute % 15, second=0, microsecond=0)
    start = end - timedelta(days=days)
    for r in runs:
        client.register_market(r.instrument, r.settings.broker_epic)
    account = await client.get_account()
    say = progress or (lambda _msg: None)
    coverage: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    for instrument in dict.fromkeys(r.instrument for r in runs):
        mine = [r for r in runs if r.instrument == instrument]
        say(f"{instrument}: downloading candles")
        history = await fetch_history(client, instrument, start, end)
        coverage[instrument] = {
            g: {"candles": len(b), "first": b[0].time.isoformat() if b else None}
            for g, b in history.bars.items() if g in ("M15", "H1", "H4")
        }
        coverage[instrument]["refused_requests"] = len(history.problems)
        cross: dict[str, float] = {}
        base, _, quote = instrument.partition("_")
        if account.currency not in (base, quote):
            for ccy in (base, quote):
                rate = await client.get_conversion_rate(ccy, account.currency)
                if rate:
                    cross[ccy] = rate
        say(f"{instrument}: replaying {len(history.bars['M15'])} M15 candles")
        await asyncio.to_thread(
            replay, history, mine, start, end, account_currency=account.currency, cross_rates=cross,
            progress=lambda f, i=instrument: say(f"{i}: {f:.0%} replayed"),
        )
        warnings += history_warnings(instrument, mine, start, history.problems)
    split = end - (end - start) * CHECK_FRACTION
    summaries = [{**summarize(r, days), **periods(r, start, end, split)} for r in runs]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "split": split.isoformat(),
        "days": days,
        "account_currency": account.currency,
        "history": coverage,
        "warnings": warnings,
        "selection": selection(summaries),
        "runs": summaries,
    }


def make_client(settings: Settings, experiments: list[ExperimentConfig]) -> Any:
    from app.broker.capital import CapitalClient

    settings.require_broker_credentials()
    first = experiments[0] if experiments else None
    return CapitalClient(
        settings.capital_base_url, settings.capital_api_key, settings.capital_identifier,
        settings.capital_api_password, account_id=settings.capital_account_id,
        instrument=first.instrument if first else settings.instrument,
        epic=first.settings.broker_epic if first else settings.broker_epic,
        stream_url=settings.capital_stream_url,
    )


class CandleFileClient:
    """Stands in for the broker with candles saved from ``GET /api/data/candles`` (one JSON file per
    instrument and granularity, named ``<INSTRUMENT>_<GRANULARITY>.json``), so a strategy can be
    backtested on real history where the broker cannot be reached. Account currency: USD."""

    def __init__(self, directory: str, currency: str = "USD") -> None:
        from pathlib import Path

        self.dir = Path(directory)
        self.currency = currency
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _load(self, instrument: str, granularity: str) -> dict[str, Any] | None:
        key = (instrument, granularity)
        if key not in self._cache:
            path = self.dir / f"{instrument}_{granularity}.json"
            self._cache[key] = json.loads(path.read_text()) if path.exists() else None  # type: ignore[assignment]
        return self._cache[key]

    def register_market(self, instrument: str, epic: str = "") -> str:
        return epic or instrument.replace("_", "")

    async def get_account(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(currency=self.currency)

    async def get_instrument(self, instrument: str | None = None) -> InstrumentInfo:
        data = next((d for g in ("M15", "H1", "H4", "D", "W") if (d := self._load(instrument or "", g))), None)
        if data is None:
            raise ConfigError(f"no saved candles for {instrument} in {self.dir}")
        return InstrumentInfo(**data["info"])

    async def get_candles_between(
        self, granularity: str, start: datetime, end: datetime, instrument: str | None = None,
        problems: list[str] | None = None,
    ) -> list[Bar]:
        from app.market_data.timeutil import parse_time

        data = self._load(instrument or "", granularity)
        if data is None:
            return []
        bars = [Bar(parse_time(t), o, h, lo, c, 0, True, bc, ac) for t, o, h, lo, c, bc, ac in data["candles"]]
        return [b for b in bars if start <= b.time < end]

    async def get_conversion_rate(self, currency: str, account_currency: str) -> float | None:
        return None

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------- jobs (dashboard + data API)


def job_params(experiments: list[str] | None, days: int, sets: dict[str, str], vary: dict[str, str]) -> dict[str, Any]:
    """Validated, normalised parameters of a backtest started over HTTP (raises ConfigError)."""
    if not 1 <= days <= MAX_DAYS:
        raise ConfigError(f"days must be 1-{MAX_DAYS}")
    s = parse_assignments([f"{k}={v}" for k, v in sets.items()], multi=False)
    v = parse_assignments([f"{k}={x}" for k, x in vary.items()], multi=True)
    clash = set(s) & set(v)
    if clash:
        raise ConfigError(f"{', '.join(sorted(clash))}: set and compared at once")
    variants(v)
    return {"experiment": sorted(set(experiments or [])), "days": days, "set": s, "vary": v}


class BacktestBusy(Exception):
    """Another backtest is running."""


@dataclass
class BacktestJob:
    id: str
    key: str
    params: dict[str, Any]
    started_at: datetime = field(default_factory=utcnow)
    status: str = "running"  # running | done | failed
    progress: str = "starting"
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None

    def view(self, *, text: bool = True, trades: bool = False, result: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "status": self.status, "params": self.params, "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None, "progress": self.progress,
        }
        if self.error:
            out["error"] = self.error
        if self.result is not None and result:
            out["result"] = self.result
            if text:
                out["text"] = render(self.result, trades=trades)
        elif self.result is not None:
            out["summary"] = [
                {k: r[k] for k in ("label", "experiment", "trades", "total_r", "profit_factor")}
                for r in self.result["runs"]
            ]
        return out


class BacktestJobs:
    """Backtests started from the dashboard or the data API: one runs at a time (each one logs in to
    the broker and downloads months of candles), and the last few results are kept in memory."""

    KEEP = 10

    def __init__(self, reuse: timedelta = timedelta(minutes=30)) -> None:
        self.reuse = reuse
        self._jobs: dict[str, BacktestJob] = {}

    def get(self, job_id: str) -> BacktestJob | None:
        return self._jobs.get(job_id)

    def recent(self) -> list[BacktestJob]:
        return list(reversed(self._jobs.values()))

    def start(self, params: dict[str, Any], load_config: Callable[[], Awaitable[Configuration]]) -> BacktestJob:
        """Start a backtest, or return the running / recently finished one with the same parameters."""
        key = json.dumps(params, sort_keys=True)
        now = utcnow()
        for job in self.recent():
            if job.key == key and (
                job.status == "running"
                or (job.status == "done" and job.finished_at and now - job.finished_at <= self.reuse)
            ):
                return job
        if any(j.status == "running" for j in self._jobs.values()):
            raise BacktestBusy("another backtest is running; try again when it finishes")
        job = BacktestJob(secrets.token_hex(6), key, params)
        self._jobs[job.id] = job
        while len(self._jobs) > self.KEEP:
            del self._jobs[next(iter(self._jobs))]
        job.task = asyncio.create_task(self._run(job, load_config))
        return job

    async def _run(self, job: BacktestJob, load_config: Callable[[], Awaitable[Configuration]]) -> None:
        def note(msg: str) -> None:
            job.progress = msg

        p = job.params
        try:
            config = await load_config()
            slugs = p["experiment"] or None
            client = make_client(config.settings, select_experiments(config, slugs))
            try:
                job.result = await backtest(config, client, slugs=slugs, days=p["days"], sets=p["set"],
                                            vary=p["vary"], progress=note)
            finally:
                await client.aclose()
            job.status, job.progress = "done", "done"
        except Exception as exc:  # reported to whoever polls the job
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}" if not isinstance(exc, ConfigError) else str(exc)
        finally:
            job.finished_at = utcnow()


async def run_from_db(
    base: Settings, *, slugs: list[str] | None, days: int, sets: dict[str, str], vary: dict[str, list[str]],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    from app.db.session import Database

    db = Database(base)
    try:
        config = await load_configuration(db, base)
    finally:
        await db.dispose()
    client = make_client(config.settings, select_experiments(config, slugs))
    try:
        return await backtest(config, client, slugs=slugs, days=days, sets=sets, vary=vary, progress=progress)
    finally:
        await client.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay history through experiments' rules and risk engine")
    parser.add_argument("--experiment", action="append", help="experiment slug (repeatable; default: all enabled)")
    parser.add_argument("--days", type=int, default=90, help=f"how far back (default 90, max {MAX_DAYS})")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="change a setting")
    parser.add_argument("--vary", action="append", default=[], metavar="KEY=V1,V2",
                        help="compare values of a setting (repeatable: every combination)")
    parser.add_argument("--trades", action="store_true", help="list every trade")
    parser.add_argument("--json", action="store_true", help="print the results as JSON")
    args = parser.parse_args()
    try:
        sets = parse_assignments(args.set, multi=False)
        vary = parse_assignments(args.vary, multi=True)
        result = asyncio.run(run_from_db(
            get_settings(), slugs=args.experiment, days=args.days, sets=sets, vary=vary,
            progress=lambda m: print(m, file=sys.stderr),
        ))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2) if args.json else render(result, trades=args.trades))
    return 0


if __name__ == "__main__":
    sys.exit(main())
