"""Why are no trades being taken? A read-only funnel per experiment from what the engine recorded.

    python -m app.diagnose            # last 7 days
    python -m app.diagnose --days 3

For every experiment it follows the 15-minute cycles down the pipeline (rules -> model -> risk
engine -> order) and names the step where they stop, with the most common reasons at each step.
It only reads the database; it never talks to the broker or the model.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config.settings import Settings, get_settings
from app.config.store import ExperimentConfig, load_configuration, single_experiment
from app.db.control import experiment_controls, get_all_controls
from app.db.enums import ControlKey
from app.db.models import Decision, DecisionRequest, Order, RiskCheck, SystemEvent, Trade
from app.db.session import Database
from app.market_data.timeutil import utcnow

# Engine problems that stop cycles before any decision is recorded.
CYCLE_PROBLEMS = ("CANDLE_NOT_READY", "INSUFFICIENT_HISTORY", "NO_PRICE", "CYCLE_FAILED", "ENGINE_WAITING",
                  "TASK_CRASHED", "RECONCILE_FAILED", "STALE_PRICES", "DECISION_INVALID", "LLM_DISABLED")


@dataclass
class Funnel:
    cycles: int = 0
    first: datetime | None = None
    last: datetime | None = None
    setups: int = 0
    rule_failures: Counter[str] = field(default_factory=Counter)
    near_misses: Counter[str] = field(default_factory=Counter)  # setups that failed exactly one rule
    model_calls: int = 0
    decisions: Counter[str] = field(default_factory=Counter)  # BUY/SELL/WAIT by the model
    model_wait_reasons: Counter[str] = field(default_factory=Counter)
    invalid: Counter[str] = field(default_factory=Counter)  # model errors / invalid answers
    trade_confidences: list[float] = field(default_factory=list)
    prefilter_on_setup: Counter[str] = field(default_factory=Counter)  # setups stopped before the model
    risk_checks: int = 0
    approved: int = 0
    risk_rejections: Counter[str] = field(default_factory=Counter)
    risk_details: dict[str, str] = field(default_factory=dict)  # last detail per failed check
    orders: Counter[str] = field(default_factory=Counter)
    order_rejects: Counter[str] = field(default_factory=Counter)
    trades: int = 0


async def build_funnel(db: Database, slug: str, since: datetime) -> Funnel:
    f = Funnel()
    async with db.session() as s:
        rows = (
            await s.execute(
                select(DecisionRequest, Decision, RiskCheck)
                .outerjoin(Decision, Decision.request_id == DecisionRequest.id)
                .outerjoin(RiskCheck, RiskCheck.request_id == DecisionRequest.id)
                .where(DecisionRequest.experiment == slug, DecisionRequest.candle_time >= since)
                .order_by(DecisionRequest.candle_time)
            )
        ).all()
        rc_ids = [rc.id for _, _, rc in rows if rc is not None]
        orders = (await s.scalars(select(Order).where(Order.risk_check_id.in_(rc_ids)))).all() if rc_ids else []
        f.trades = len(
            (await s.scalars(select(Trade.id).where(Trade.experiment == slug, Trade.open_time >= since))).all()
        )
    for req, dec, rc in rows:
        f.cycles += 1
        f.first = f.first or req.candle_time
        f.last = req.candle_time
        strat = req.strategy_result or {}
        failed = [c.get("name", "") for c in strat.get("conditions", []) if not c.get("passed")]
        if strat.get("candidate"):
            f.setups += 1
        else:
            f.rule_failures.update(strat.get("failure_codes") or ["NO_SETUP"])
            if len(failed) == 1:
                f.near_misses[failed[0]] += 1
        if dec is None:
            continue
        if dec.source == "PREFILTER":
            if strat.get("candidate"):
                f.prefilter_on_setup.update(dec.reason_codes or ["?"])
            continue
        f.model_calls += 1
        if dec.source == "ERROR" or not dec.valid:
            f.invalid.update(dec.reason_codes or ["ERROR"])
            continue
        f.decisions[dec.decision] += 1
        if dec.decision == "WAIT":
            f.model_wait_reasons.update(dec.reason_codes or [])
        elif dec.confidence is not None:
            f.trade_confidences.append(dec.confidence)
        if rc is not None:
            f.risk_checks += 1
            if rc.approved:
                f.approved += 1
            for name in rc.rejection_reasons or []:
                f.risk_rejections[name] += 1
                detail = next((c.get("detail", "") for c in rc.checks or [] if c.get("name") == name), "")
                f.risk_details[name] = str(detail)
    for o in orders:
        f.orders[o.status] += 1
        if o.reject_reason:
            f.order_rejects[o.reject_reason[:80]] += 1
    return f


def _top(c: Counter[str], n: int = 5) -> str:
    return ", ".join(f"{k} {v}" for k, v in c.most_common(n)) or "–"


def verdict(f: Funnel, e: ExperimentConfig) -> str:
    """Where the cycles stop, in one sentence."""
    if not e.enabled:
        return "switched off on the Config page."
    if f.cycles == 0:
        return "no cycles recorded: the engine is not running this experiment (see the engine section)."
    if f.setups == 0:
        top = f.rule_failures.most_common(1)[0][0] if f.rule_failures else "?"
        return f"the strategy rules found no setup in {f.cycles} cycles (most common blocker: {top})."
    if f.model_calls == 0:
        return f"{f.setups} setup(s) were stopped before the model ({_top(f.prefilter_on_setup, 2)})."
    trades = f.decisions["BUY"] + f.decisions["SELL"]
    if trades == 0:
        return f"the model answered WAIT to all {f.model_calls} setup(s)" + (
            f" ({sum(f.invalid.values())} invalid/error answers)" if f.invalid else ""
        ) + "."
    if f.approved == 0:
        return f"the risk engine rejected all {f.risk_checks} trade signal(s) ({_top(f.risk_rejections, 3)})."
    if not f.orders["FILLED"]:
        return f"{f.approved} approved trade(s) never filled ({_top(f.orders, 3)}; {_top(f.order_rejects, 2)})."
    return f"{f.orders['FILLED']} order(s) filled, {f.trades} trade(s) opened: the pipeline works end to end."


def report(f: Funnel, e: ExperimentConfig) -> list[str]:
    s = e.settings
    span = f"{f.first:%a %d %b %H:%M} – {f.last:%a %d %b %H:%M} UTC" if f.first and f.last else "none"
    lines = [
        f"■ {e.slug}  ({e.instrument}, {e.strategy}, {'enabled' if e.enabled else 'OFF'})",
        f"  settings: min R:R {s.min_risk_reward}, min confidence {s.min_decision_confidence}, max spread "
        f"{s.max_spread_pips} pips, stops {s.min_stop_pips:g}-{s.max_stop_pips:g} pips, model "
        f"{s.openrouter_model} ({s.llm_call_policy.value})",
        f"  1. cycles recorded:   {f.cycles}  ({span})",
        f"  2. setups found:      {f.setups}"
        + (f"   · rules that blocked the rest: {_top(f.rule_failures, 6)}" if f.rule_failures else ""),
    ]
    if f.near_misses:
        lines.append(f"     near misses (only one rule failed): {_top(f.near_misses, 6)}")
    if f.prefilter_on_setup:
        lines.append(f"     setups stopped before the model: {_top(f.prefilter_on_setup)}")
    conf = (
        f", BUY/SELL confidence {min(f.trade_confidences):.2f}-{max(f.trade_confidences):.2f}"
        if f.trade_confidences else ""
    )
    lines.append(
        f"  3. model calls:       {f.model_calls}   · BUY {f.decisions['BUY']}, SELL {f.decisions['SELL']}, "
        f"WAIT {f.decisions['WAIT']}{conf}"
        + (f", invalid/errors: {_top(f.invalid)}" if f.invalid else "")
    )
    if f.model_wait_reasons:
        lines.append(f"     model's reasons for WAIT: {_top(f.model_wait_reasons, 6)}")
    lines.append(f"  4. risk checks:       {f.risk_checks}   · approved {f.approved}"
                 + (f" · failed: {_top(f.risk_rejections, 6)}" if f.risk_rejections else ""))
    for name, _ in f.risk_rejections.most_common(3):
        lines.append(f"     {name}: {f.risk_details.get(name, '')[:110]}")
    lines.append(f"  5. orders:            {sum(f.orders.values())}"
                 + (f"   · {_top(f.orders)}" if f.orders else "")
                 + (f" · rejected/failed: {_top(f.order_rejects, 3)}" if f.order_rejects else ""))
    lines.append(f"  6. trades opened:     {f.trades}")
    lines.append(f"  → stops at: {verdict(f, e)}")
    return lines


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    secs = (utcnow() - datetime.fromisoformat(iso)).total_seconds()
    return f"{secs:.0f}s ago" if secs < 120 else f"{secs / 60:.0f} min ago" if secs < 7200 else f"{secs / 3600:.1f} h ago"


async def engine_section(db: Database, slugs: list[str], since: datetime) -> list[str]:
    async with db.session() as s:
        controls = await get_all_controls(s)
        events = (
            await s.scalars(
                select(SystemEvent)
                .where(SystemEvent.created_at >= since, SystemEvent.event_type.in_(CYCLE_PROBLEMS))
                .order_by(SystemEvent.created_at.desc())
            )
        ).all()
    hb = controls.get(str(ControlKey.ENGINE_HEARTBEAT)) or {}
    lines = [f"Engine: {hb.get('state', 'no heartbeat')} · last heartbeat {_age(hb.get('at'))}"
             + (f" · reason: {hb['reason']}" if hb.get("reason") else "")]
    running = sorted((hb.get("experiments") or {}).keys())
    lines.append(f"  experiments in the running engine: {', '.join(running) or 'none'}")
    for i, m in sorted((hb.get("markets") or {}).items()):
        lines.append(
            f"  {i}: stream {'live' if m.get('stream_connected') else 'DOWN'}, market "
            f"{'open' if m.get('market_open') else 'closed'}, spread {m.get('spread_pips')} pips, "
            f"broker status {m.get('broker_market_status')}"
        )
    if (controls.get(str(ControlKey.KILL_SWITCH)) or {}).get("active"):
        lines.append("  ! account-wide kill switch is ON")
    for slug in slugs:
        mine = experiment_controls(controls, slug)
        flags = [name for name, key in (("kill switch", "kill_switch"), ("daily-loss breaker", "daily_loss_breaker"),
                                        ("drawdown breaker", "drawdown_breaker"))
                 if (mine.get(key) or {}).get("active") or (mine.get(key) or {}).get("tripped")]
        if flags:
            lines.append(f"  ! {slug}: {', '.join(flags)} ON")
    counts = Counter(e.event_type for e in events)
    if counts:
        lines.append(f"  problems in the period: {_top(counts, 8)}")
        latest = {}
        for e in events:
            latest.setdefault(e.event_type, e)
        for e in list(latest.values())[:4]:
            lines.append(f"    {e.event_type} ({_age(e.created_at.isoformat())}): {e.message[:110]}")
    return lines


async def run(settings: Settings, days: int) -> int:
    db = Database(settings)
    try:
        config = await load_configuration(db, settings)
        experiments = config.experiments or single_experiment(config.settings).experiments
        since = utcnow() - timedelta(days=days)
        print(f"Tradlysis diagnosis · last {days} day(s) · {utcnow():%Y-%m-%d %H:%M} UTC\n")
        print("\n".join(await engine_section(db, [e.slug for e in experiments], since)))
        for e in experiments:
            print()
            print("\n".join(report(await build_funnel(db, e.slug, since), e)))
    finally:
        await db.dispose()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Show where each experiment's decision cycles stop")
    parser.add_argument("--days", type=int, default=7, help="how far back to look (default 7)")
    args = parser.parse_args()
    return asyncio.run(run(get_settings(), args.days))


if __name__ == "__main__":
    sys.exit(main())
