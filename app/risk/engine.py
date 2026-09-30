"""Deterministic Risk Engine.

Final authority over whether an order may be submitted. The engine:
  * evaluates every check (even after one fails) so each rejection is fully explained;
  * computes the position size itself (the LLM has no say in size or limits);
  * re-validates the trade plan against the *current* price, not the price at decision time.

``evaluate`` is a pure function over a :class:`RiskContext`; gathering the context
(broker state, DB flags, prices) is done by the orchestrator.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.broker.oanda import InstrumentInfo
from app.config.settings import Settings
from app.db.enums import Direction
from app.strategy.trend_pullback import TradePlan


@dataclass
class OpenTradeRisk:
    instrument: str
    units: int  # signed
    entry: float
    stop_loss: float | None


@dataclass
class RiskContext:
    now: datetime
    decision: str  # BUY | SELL | WAIT
    decision_valid: bool
    confidence: float | None
    trade_plan: TradePlan | None
    strategy_candidate: bool

    # controls
    kill_switch_active: bool
    kill_switch_reason: str
    daily_breaker_tripped: bool
    drawdown_breaker_tripped: bool

    # market
    market_open: bool
    stream_connected: bool
    bid: float | None
    ask: float | None
    price_time: datetime | None
    tradeable: bool
    instrument: InstrumentInfo

    # account (from broker, via reconciliation)
    account_currency: str
    nav: Decimal | None
    balance: Decimal | None
    margin_available: Decimal | None
    account_state_time: datetime | None
    day_start_nav: Decimal | None
    peak_nav: Decimal | None
    quote_home_rate: float | None  # account-currency value of 1 unit of quote currency (loss side)
    base_home_rate: float | None  # account-currency value of 1 unit of base currency (position value)

    # exposure
    open_trades: list[OpenTradeRisk] = field(default_factory=list)
    unresolved_orders: int = 0
    last_entry_same_direction_at: datetime | None = None

    # news
    news_blackout: bool = False
    news_blackout_titles: list[str] = field(default_factory=list)
    calendar_fresh: bool = True


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class RiskResult:
    approved: bool
    checks: list[CheckResult]
    direction: str | None = None
    units: int | None = None  # signed
    risk_amount: Decimal | None = None
    risk_pct: float | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    nav: Decimal | None = None
    trip_daily_breaker: bool = False
    trip_drawdown_breaker: bool = False

    @property
    def rejection_reasons(self) -> list[str]:
        return [c.name for c in self.checks if not c.passed]

    def checks_json(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.checks]


def evaluate(ctx: RiskContext, settings: Settings) -> RiskResult:
    checks: list[CheckResult] = []

    def add(name: str, passed: bool, detail: str = "") -> bool:
        checks.append(CheckResult(name, bool(passed), detail))
        return bool(passed)

    inst = ctx.instrument
    pip = inst.pip_size
    result = RiskResult(approved=False, checks=checks, nav=ctx.nav)

    # --- Global controls -------------------------------------------------------------
    add("KILL_SWITCH", not ctx.kill_switch_active, ctx.kill_switch_reason or "inactive")
    add("TRADING_ENABLED", settings.trading_enabled, "TRADING_ENABLED setting")
    add("DRAWDOWN_BREAKER", not ctx.drawdown_breaker_tripped, "tripped" if ctx.drawdown_breaker_tripped else "ok")
    add("DAILY_LOSS_BREAKER", not ctx.daily_breaker_tripped, "tripped" if ctx.daily_breaker_tripped else "ok")

    # --- Account health & loss limits ------------------------------------------------------
    age = (ctx.now - ctx.account_state_time).total_seconds() if ctx.account_state_time else None
    healthy = (
        ctx.nav is not None
        and ctx.nav > 0
        and age is not None
        and age <= settings.max_account_state_age_seconds
    )
    add("ACCOUNT_HEALTH", healthy, f"nav={ctx.nav} state_age_s={None if age is None else round(age)}")

    if ctx.nav is not None and ctx.day_start_nav:
        daily_pct = float((ctx.nav - ctx.day_start_nav) / ctx.day_start_nav * 100)
        ok = daily_pct > -settings.max_daily_loss_pct
        add("MAX_DAILY_LOSS", ok, f"{daily_pct:.2f}% today (limit -{settings.max_daily_loss_pct}%)")
        result.trip_daily_breaker = not ok
    else:
        add("MAX_DAILY_LOSS", False, "start-of-day NAV unknown")

    if ctx.nav is not None and ctx.peak_nav:
        dd_pct = float((ctx.peak_nav - ctx.nav) / ctx.peak_nav * 100)
        ok = dd_pct < settings.max_drawdown_pct
        add("MAX_DRAWDOWN", ok, f"{dd_pct:.2f}% from peak (limit {settings.max_drawdown_pct}%)")
        result.trip_drawdown_breaker = not ok
    else:
        add("MAX_DRAWDOWN", False, "peak NAV unknown")

    # --- Decision validity ------------------------------------------------------------------
    is_trade = ctx.decision in (Direction.BUY, Direction.SELL)
    add("DECISION_VALID", ctx.decision_valid and is_trade, f"decision={ctx.decision} valid={ctx.decision_valid}")
    conf_ok = ctx.confidence is not None and ctx.confidence >= settings.min_decision_confidence
    add("MIN_CONFIDENCE", conf_ok, f"{ctx.confidence} (min {settings.min_decision_confidence})")

    plan = ctx.trade_plan
    plan_ok = plan is not None and ctx.strategy_candidate and plan.direction == ctx.decision
    add(
        "TRADE_PLAN_MATCHES",
        plan_ok,
        "no deterministic plan"
        if plan is None
        else f"plan={plan.direction} decision={ctx.decision} candidate={ctx.strategy_candidate}",
    )

    # --- Market conditions ----------------------------------------------------------------
    add("MARKET_OPEN", ctx.market_open and ctx.tradeable, f"open={ctx.market_open} tradeable={ctx.tradeable}")
    price_age = (ctx.now - ctx.price_time).total_seconds() if ctx.price_time else None
    fresh = ctx.stream_connected and price_age is not None and price_age <= settings.stale_price_seconds
    add("PRICE_FRESH", fresh, f"stream={ctx.stream_connected} age_s={None if price_age is None else round(price_age, 1)}")
    spread_pips = None if ctx.bid is None or ctx.ask is None else (ctx.ask - ctx.bid) / pip
    add(
        "SPREAD_LIMIT",
        spread_pips is not None and spread_pips <= settings.max_spread_pips,
        f"{None if spread_pips is None else round(spread_pips, 2)} pips (max {settings.max_spread_pips})",
    )

    # --- News -------------------------------------------------------------------------------
    add(
        "NEWS_BLACKOUT",
        not ctx.news_blackout,
        ", ".join(ctx.news_blackout_titles) if ctx.news_blackout else "no blocking events",
    )
    if settings.news_require_fresh_calendar:
        add("NEWS_CALENDAR_FRESH", ctx.calendar_fresh, "calendar fresh" if ctx.calendar_fresh else "calendar stale")

    # --- Exposure / duplicates -------------------------------------------------------------------
    same_inst = [t for t in ctx.open_trades if t.instrument == inst.name]
    add(
        "EXISTING_POSITION",
        len(same_inst) == 0 and len(ctx.open_trades) < settings.max_open_trades,
        f"{len(ctx.open_trades)} open trade(s), {len(same_inst)} on {inst.name}",
    )
    add("NO_UNRESOLVED_ORDERS", ctx.unresolved_orders == 0, f"{ctx.unresolved_orders} unresolved")
    if ctx.last_entry_same_direction_at is not None:
        mins = (ctx.now - ctx.last_entry_same_direction_at).total_seconds() / 60
        add(
            "SIGNAL_COOLDOWN",
            mins >= settings.signal_cooldown_minutes,
            f"last {ctx.decision} entry {mins:.0f} min ago (cooldown {settings.signal_cooldown_minutes})",
        )
    else:
        add("SIGNAL_COOLDOWN", True, "no recent entry in this direction")

    # --- Trade geometry & sizing (needs a plan and a live price) -------------------------------------
    if plan is not None and ctx.bid is not None and ctx.ask is not None and is_trade:
        long = ctx.decision == Direction.BUY
        entry = ctx.ask if long else ctx.bid
        sl = round(plan.stop_loss, inst.display_precision)
        tp = round(plan.take_profit, inst.display_precision)
        # Round away float noise (1.1026 - 1.1006 = 0.0020000000000000018) before sizing.
        nd = inst.display_precision + 2
        risk_dist = round((entry - sl) if long else (sl - entry), nd)
        reward_dist = round((tp - entry) if long else (entry - tp), nd)
        risk_pips = risk_dist / pip
        sides_ok = risk_dist > 0 and reward_dist > 0
        add("STOP_TARGET_SIDES", sides_ok, f"entry={entry} sl={sl} tp={tp}")
        add(
            "STOP_DISTANCE",
            sides_ok and settings.min_stop_pips <= risk_pips <= settings.max_stop_pips,
            f"{risk_pips:.1f} pips (range {settings.min_stop_pips}-{settings.max_stop_pips})",
        )
        rr = reward_dist / risk_dist if risk_dist > 0 else 0.0
        add("MIN_RISK_REWARD", round(rr, 3) >= settings.min_risk_reward, f"{rr:.2f} (min {settings.min_risk_reward})")
        result.entry, result.stop_loss, result.take_profit, result.risk_reward = entry, sl, tp, round(rr, 2)
        result.direction = ctx.decision

        units, risk_amount, sizing_detail = _size_position(ctx, settings, risk_dist)
        add("POSITION_SIZE", units is not None and units >= inst.minimum_trade_size, sizing_detail)
        if units is not None and ctx.nav:
            result.units = units if long else -units
            result.risk_amount = risk_amount
            actual_risk = Decimal(str(units * risk_dist * (ctx.quote_home_rate or 0)))
            result.risk_pct = round(float(actual_risk / ctx.nav * 100), 4)

            open_risk, open_detail = _open_risk(ctx)
            total_pct = None if open_risk is None else float((open_risk + actual_risk) / ctx.nav * 100)
            add(
                "MAX_TOTAL_RISK",
                total_pct is not None and total_pct <= settings.max_total_risk_pct + 1e-9,
                open_detail
                if total_pct is None
                else f"{total_pct:.3f}% incl. new trade (max {settings.max_total_risk_pct}%)",
            )

            margin_ok, margin_detail = _margin_check(ctx, settings, units, entry)
            add("MARGIN", margin_ok, margin_detail)
    else:
        add("TRADE_GEOMETRY", False, "no plan, price or trade decision")

    result.approved = all(c.passed for c in checks)
    return result


def _size_position(ctx: RiskContext, settings: Settings, risk_dist: float) -> tuple[int | None, Decimal | None, str]:
    if ctx.nav is None or ctx.balance is None:
        return None, None, "account values unknown"
    if not ctx.quote_home_rate or ctx.quote_home_rate <= 0:
        return None, None, "quote->account currency conversion unavailable"
    if risk_dist <= 0:
        return None, None, "invalid stop distance"
    equity = min(ctx.nav, ctx.balance)
    risk_amount = equity * Decimal(str(settings.risk_per_trade_pct)) / Decimal(100)
    loss_per_unit = risk_dist * ctx.quote_home_rate
    raw_units = float(risk_amount) / loss_per_unit
    precision = ctx.instrument.trade_units_precision
    factor = 10**precision
    units = math.floor(raw_units * factor) / factor
    units_int = int(units) if precision == 0 else units
    return (
        int(units_int),
        risk_amount,
        f"{int(units_int)} units risking {risk_amount:.2f} {ctx.account_currency} "
        f"({settings.risk_per_trade_pct}% of {equity:.2f})",
    )


def _open_risk(ctx: RiskContext) -> tuple[Decimal | None, str]:
    total = Decimal(0)
    for t in ctx.open_trades:
        if t.stop_loss is None:
            return None, f"open trade on {t.instrument} has no stop-loss; exposure unbounded"
        # Conversion for other instruments is not available; V1 trades a single instrument.
        rate = ctx.quote_home_rate if t.instrument == ctx.instrument.name else None
        if rate is None:
            return None, f"cannot value risk of open trade on {t.instrument}"
        dist = (t.entry - t.stop_loss) if t.units > 0 else (t.stop_loss - t.entry)
        total += Decimal(str(max(dist, 0.0) * abs(t.units) * rate))
    return total, f"open risk {total:.2f}"


def _margin_check(ctx: RiskContext, settings: Settings, units: int, entry: float) -> tuple[bool, str]:
    if ctx.margin_available is None:
        return False, "margin available unknown"
    rate = ctx.base_home_rate
    if rate is None:
        rate = entry * (ctx.quote_home_rate or 0)
    if not rate:
        return False, "base->account currency conversion unavailable"
    required = Decimal(str(units * rate * ctx.instrument.margin_rate))
    limit = ctx.margin_available * Decimal(str(settings.max_margin_usage_pct)) / Decimal(100)
    return required <= limit, f"requires {required:.2f}, limit {limit:.2f}"
