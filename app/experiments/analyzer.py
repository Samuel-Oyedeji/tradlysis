"""Experiment Analyzer: periodically summarises every experiment. Never trades.

Run continuously:  python -m app.experiments.analyzer
Run once & print:  python -m app.experiments.analyzer --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.api.history import DECISION_BAR, HYPOTHETICAL_HORIZON, hypothetical_outcome, load_bars
from app.config.settings import Settings, get_settings
from app.config.store import import_env_once, load_configuration, single_experiment
from app.db.bootstrap import auto_create_tables
from app.db.enums import TradeState
from app.db.models import (
    AccountSnapshot,
    AnalysisReport,
    Decision,
    DecisionRequest,
    Order,
    RiskCheck,
    Trade,
)
from app.db.session import Database
from app.experiments.excursion import bot_close_reasons, close_category, summarize, trade_excursion
from app.logging_setup import setup_logging
from app.market_data.timeutil import utcnow

log = logging.getLogger("tradlysis.analyzer")

CONFIDENCE_BUCKETS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
RR_EDGES = (1.0, 1.2, 1.5, 2.0, 3.0)
# MIN_RISK_REWARD values the what-if replay of R:R-blocked setups is run at.
RR_WHAT_IF_THRESHOLDS = (1.0, 1.2, 1.5, 1.8)


@dataclass
class ClosedTrade:
    r: float | None
    pl: float
    direction: str
    close_reason: str | None
    confidence: float | None
    snapshot: dict[str, Any]
    strategy: dict[str, Any]
    requested_price: float | None
    fill_price: float | None
    unexpected: bool
    trade_id: int | None = None
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    outcome: str | None = None  # see app.experiments.excursion.close_category
    excursion: dict[str, Any] | None = None  # see app.experiments.excursion.Excursion


# ---------------------------------------------------------------------- pure metrics


def r_stats(rs: Sequence[float]) -> dict[str, Any]:
    if not rs:
        return {"trades": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(rs),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(rs), 3),
        "avg_winner_r": round(gross_win / len(wins), 3) if wins else None,
        "avg_loser_r": round(-gross_loss / len(losses), 3) if losses else None,
        "avg_r": round(sum(rs) / len(rs), 3),
        "total_r": round(sum(rs), 3),
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_r": round(max_drawdown(rs), 3),
    }


def max_drawdown(rs: Iterable[float]) -> float:
    """Largest peak-to-trough decline of the cumulative R curve (positive number)."""
    equity = peak = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return dd


def nav_drawdown_pct(navs: Sequence[float]) -> float | None:
    if not navs:
        return None
    peak = navs[0]
    dd = 0.0
    for v in navs:
        peak = max(peak, v)
        if peak > 0:
            dd = max(dd, (peak - v) / peak * 100)
    return round(dd, 3)


def confidence_bucket(c: float | None) -> str:
    if c is None:
        return "none"
    for lo, hi in CONFIDENCE_BUCKETS:
        if lo <= c < hi:
            return f"{lo:.1f}-{min(hi, 1.0):.1f}"
    return "other"


def group_r(trades: Sequence[ClosedTrade], key: Callable[[ClosedTrade], str]) -> dict[str, Any]:
    groups: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        if t.r is not None:
            groups[key(t)].append(t.r)
    return {k: r_stats(v) for k, v in sorted(groups.items())}


def _cond_detail(strategy: dict[str, Any], name: str) -> str:
    for c in strategy.get("conditions", []):
        if c.get("name") == name:
            return str(c.get("detail", ""))
    return ""


def h1_alignment_key(t: ClosedTrade) -> str:
    return "h1_fully_aligned" if "fully aligned" in _cond_detail(t.strategy, "h1_alignment") else "h1_neutral"


def pullback_level_key(t: ClosedTrade) -> str:
    detail = _cond_detail(t.strategy, "pullback_to_level")
    for token in ("SWING_CLUSTER", "PREV_DAY", "PREV_WEEK", "PREV_MONTH", "ROUND_NUMBER", "M15_EMA50"):
        if token in detail:
            return token
    return "other"


def planned_rr(strategy: dict[str, Any]) -> float | None:
    rr = (strategy.get("trade_plan") or {}).get("risk_reward")
    return float(rr) if rr is not None else None


def rr_bucket(rr: float | None) -> str:
    if rr is None:
        return "unknown"
    if rr < RR_EDGES[0]:
        return f"<{RR_EDGES[0]:.1f}R"
    for lo, hi in zip(RR_EDGES, RR_EDGES[1:], strict=False):
        if lo <= rr < hi:
            return f"{lo:.1f}-{hi:.1f}R"
    return f"{RR_EDGES[-1]:.1f}R+"


def failed_conditions(strategy: dict[str, Any] | None) -> list[str]:
    return [c.get("name", "") for c in (strategy or {}).get("conditions", []) if not c.get("passed")]


def near_misses(requests: Sequence[dict[str, Any]], cooldown: timedelta = timedelta(minutes=60)) -> dict[str, Any]:
    """Setups that failed exactly one rule, and a what-if replay of those blocked only by R:R.

    Each request may carry ``time`` (decision time) and ``hypothetical`` (the TP/SL walk-forward of
    its trade plan, see :func:`app.api.history.hypothetical_outcome`). For each threshold the replay
    keeps one trade at a time, like MAX_OPEN_TRADES=1: a setup is skipped while the previous one
    would still be open (or within ``cooldown`` of it when its outcome is unknown). Mid prices, no
    spread, and the model and risk engine could still have said no, so this is an upper bound.
    """
    single: Counter[str] = Counter()
    rr_only: list[dict[str, Any]] = []
    for r in requests:
        strategy = r.get("strategy_result") or {}
        if strategy.get("candidate"):
            continue
        failed = failed_conditions(strategy)
        if len(failed) != 1:
            continue
        single[failed[0]] += 1
        rr = planned_rr(strategy)
        if failed[0] == "risk_reward" and rr is not None and r.get("time") is not None:
            rr_only.append({"time": r["time"], "rr": rr, "hypothetical": r.get("hypothetical") or {}})
    rr_only.sort(key=lambda x: x["time"])

    what_if: dict[str, Any] = {}
    for threshold in RR_WHAT_IF_THRESHOLDS:
        busy_until: datetime | None = None
        rs: list[float] = []
        counts: Counter[str] = Counter()
        for x in rr_only:
            if round(x["rr"], 3) < threshold or (busy_until is not None and x["time"] < busy_until):
                continue
            h = x["hypothetical"]
            result = h.get("result") or "UNKNOWN"
            counts[result] += 1
            if result == "WOULD_WIN":
                rs.append(x["rr"])
            elif result == "WOULD_LOSE":
                rs.append(-1.0)
            if h.get("resolved_at"):
                busy_until = datetime.fromisoformat(h["resolved_at"]) + DECISION_BAR
            elif result in ("EXPIRED", "UNRESOLVED"):
                busy_until = x["time"] + HYPOTHETICAL_HORIZON
            else:
                busy_until = x["time"] + cooldown
        what_if[f"{threshold:g}"] = {
            "setups": sum(counts.values()),
            "would_win": counts["WOULD_WIN"],
            "would_lose": counts["WOULD_LOSE"],
            "not_resolved": sum(counts.values()) - counts["WOULD_WIN"] - counts["WOULD_LOSE"],
            "total_r": round(sum(rs), 2) if rs else None,
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        }
    return {
        "blocked_by_one_rule": dict(single.most_common()),
        "rr_only": {
            "candles": len(rr_only),
            "by_planned_rr": dict(Counter(rr_bucket(x["rr"]) for x in rr_only)),
            "what_if_min_rr": what_if,
        },
    }


def compute_metrics(
    *,
    requests: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    risk_checks: Sequence[dict[str, Any]],
    orders: Sequence[dict[str, Any]],
    trades: Sequence[ClosedTrade],
    open_trades: int,
    navs: Sequence[float],
    pip_size: float,
) -> dict[str, Any]:
    closed = [t for t in trades if not t.unexpected]
    rs = [t.r for t in closed if t.r is not None]

    rejection_reasons: Counter[str] = Counter()
    for rc in risk_checks:
        if not rc["approved"]:
            rejection_reasons.update(rc["rejection_reasons"] or [])
    wait_reasons: Counter[str] = Counter()
    for d in decisions:
        if d["decision"] == "WAIT":
            wait_reasons.update(d["reason_codes"] or [])

    slippages = []
    for t in closed:
        if t.fill_price is not None and t.requested_price is not None:
            adverse = (t.fill_price - t.requested_price) if t.direction == "BUY" else (t.requested_price - t.fill_price)
            slippages.append(adverse / pip_size)
    spreads = [
        r["snapshot"].get("price", {}).get("spread_pips")
        for r in requests
        if r["snapshot"].get("price", {}).get("spread_pips") is not None
    ]
    latencies = [d["latency_ms"] for d in decisions if d["latency_ms"] is not None]

    return {
        "opportunities": len(requests),
        "opportunities_by_regime": dict(
            Counter(str((r["snapshot"].get("market_regime") or {}).get("label", "UNKNOWN")) for r in requests)
        ),
        "setup_candidates": sum(1 for r in requests if (r["strategy_result"] or {}).get("candidate")),
        "llm_calls": sum(1 for r in requests if r["llm_called"]),
        "decisions": dict(Counter(d["decision"] for d in decisions)),
        "decision_sources": dict(Counter(d["source"] for d in decisions)),
        "invalid_model_responses": sum(1 for d in decisions if not d["valid"]),
        "risk_checks": len(risk_checks),
        "risk_approved": sum(1 for rc in risk_checks if rc["approved"]),
        "rejection_reasons": dict(rejection_reasons.most_common()),
        "wait_reasons": dict(wait_reasons.most_common(20)),
        "orders": dict(Counter(o["status"] for o in orders)),
        "open_trades": open_trades,
        "closed_trades": r_stats(rs),
        "realized_pl": round(sum(t.pl for t in closed), 2),
        "close_reasons": dict(Counter(t.close_reason or "UNKNOWN" for t in closed)),
        "unexpected_trades": sum(1 for t in trades if t.unexpected),
        "account_max_drawdown_pct": nav_drawdown_pct(navs),
        "by_setup_condition": {
            "h1_alignment": group_r(closed, h1_alignment_key),
            "pullback_level": group_r(closed, pullback_level_key),
        },
        "by_market_regime": {
            "overall": group_r(closed, lambda t: str((t.snapshot.get("market_regime") or {}).get("label", "UNKNOWN"))),
            "volatility": group_r(closed, lambda t: str(t.snapshot.get("volatility", {}).get("regime", "UNKNOWN"))),
            "h1_regime": group_r(closed, lambda t: str(t.snapshot.get("regimes", {}).get("1h", "UNKNOWN"))),
        },
        "by_news_risk": group_r(closed, lambda t: str(t.snapshot.get("news", {}).get("risk", "unknown"))),
        "by_confidence": group_r(closed, lambda t: confidence_bucket(t.confidence)),
        "by_direction": group_r(closed, lambda t: t.direction),
        "by_planned_rr": group_r(closed, lambda t: rr_bucket(planned_rr(t.strategy))),
        "near_misses": near_misses(requests),
        "trade_outcomes": trade_outcomes(closed),
        "execution": {
            "avg_slippage_pips": round(sum(slippages) / len(slippages), 3) if slippages else None,
            "max_slippage_pips": round(max(slippages), 3) if slippages else None,
            "avg_spread_pips_at_decision": round(sum(spreads) / len(spreads), 3) if spreads else None,
            "max_spread_pips_at_decision": round(max(spreads), 3) if spreads else None,
            "avg_llm_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        },
    }


def trade_outcomes(closed: Sequence[ClosedTrade], recent: int = 50) -> dict[str, Any]:
    """How trades ended and how far they went first (take-profit / stop-loss / closed manually ...)."""
    rows = [
        {
            "id": t.trade_id,
            "opened_at": t.opened_at.isoformat() if t.opened_at else None,
            "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            "hours": round((t.closed_at - t.opened_at).total_seconds() / 3600, 2) if t.closed_at and t.opened_at else None,
            "direction": t.direction,
            "outcome": t.outcome or "unknown",
            "r": t.r,
            "excursion": t.excursion,
        }
        for t in closed
    ]
    rows.sort(key=lambda r: r["closed_at"] or "", reverse=True)
    return {**summarize(rows), "trades": rows[:recent]}


# ---------------------------------------------------------------------- DB loading


async def analyze(
    db: Database, settings: Settings, pip_size: float = 0.0001, capital: Decimal | None = None
) -> dict[str, Any]:
    """Metrics for one experiment. Its drawdown in % is measured on its own equity (``capital`` plus
    realized P/L after each closed trade); without a capital, on the broker account's NAV."""
    exp = settings.experiment_name
    async with db.session() as s:
        reqs = (await s.scalars(select(DecisionRequest).where(DecisionRequest.experiment == exp))).all()
        req_ids = [r.id for r in reqs]
        decs = (await s.scalars(select(Decision).where(Decision.request_id.in_(req_ids)))).all() if req_ids else []
        rcs = (await s.scalars(select(RiskCheck).where(RiskCheck.request_id.in_(req_ids)))).all() if req_ids else []
        rc_ids = [rc.id for rc in rcs]
        ords = (await s.scalars(select(Order).where(Order.risk_check_id.in_(rc_ids)))).all() if rc_ids else []
        trades = (await s.scalars(select(Trade).where(Trade.experiment == exp))).all()
        unexpected = (await s.scalars(select(Trade).where(Trade.unexpected.is_(True)))).all()
        if capital is None:
            navs = (await s.scalars(select(AccountSnapshot.nav).order_by(AccountSnapshot.taken_at))).all()
        else:
            navs = [capital]
            for t in sorted((t for t in trades if t.state == TradeState.CLOSED and t.close_time),
                            key=lambda t: t.close_time):
                navs.append(navs[-1] + (t.realized_pl or 0))
        close_reasons = await bot_close_reasons(s)
        now = utcnow()
        hypo = await _rr_only_hypotheticals(s, settings.instrument, reqs)
        excursions: dict[int, dict[str, Any] | None] = {}
        for t in list(trades) + list(unexpected):
            if t.state == TradeState.CLOSED and t.id not in excursions:
                ex = await trade_excursion(s, t, now)
                excursions[t.id] = ex.to_dict() if ex else None

    req_by_id = {r.id: r for r in reqs}
    dec_by_req = {d.request_id: d for d in decs}
    rc_by_id = {rc.id: rc for rc in rcs}
    order_by_id = {o.id: o for o in ords}

    closed: list[ClosedTrade] = []
    for t in list(trades) + [u for u in unexpected if u.experiment != exp]:
        if t.state != TradeState.CLOSED:
            continue
        order = order_by_id.get(t.order_id) if t.order_id else None
        rc = rc_by_id.get(order.risk_check_id) if order and order.risk_check_id else None
        req = req_by_id.get(rc.request_id) if rc else None
        dec = dec_by_req.get(req.id) if req else None
        closed.append(
            ClosedTrade(
                r=t.r_multiple,
                pl=float(t.realized_pl or 0),
                direction=t.direction,
                close_reason=t.close_reason,
                confidence=dec.confidence if dec else None,
                snapshot=req.snapshot if req else {},
                strategy=req.strategy_result if req else {},
                requested_price=order.requested_price if order else None,
                fill_price=order.fill_price if order else None,
                unexpected=t.unexpected,
                trade_id=t.id,
                opened_at=t.open_time,
                closed_at=t.close_time,
                outcome=close_category(t, close_reasons),
                excursion=excursions.get(t.id),
            )
        )

    metrics = compute_metrics(
        requests=[
            {
                "snapshot": r.snapshot or {},
                "strategy_result": r.strategy_result,
                "llm_called": r.llm_called,
                "time": r.candle_time + DECISION_BAR,
                "hypothetical": hypo.get(r.id),
            }
            for r in reqs
        ],
        decisions=[
            {
                "decision": d.decision,
                "source": d.source,
                "valid": d.valid,
                "reason_codes": d.reason_codes,
                "latency_ms": d.latency_ms,
            }
            for d in decs
        ],
        risk_checks=[{"approved": rc.approved, "rejection_reasons": rc.rejection_reasons} for rc in rcs],
        orders=[{"status": o.status} for o in ords],
        trades=closed,
        open_trades=sum(1 for t in trades if t.state == TradeState.OPEN),
        navs=[float(n) for n in navs],
        pip_size=pip_size,
    )
    metrics["experiment"] = exp
    metrics["period_start"] = min((r.created_at for r in reqs), default=None)
    return metrics


async def _rr_only_hypotheticals(s: Any, instrument: str, reqs: Sequence[DecisionRequest]) -> dict[int, dict[str, Any]]:
    """Walk-forward TP/SL outcome of every setup whose only failed rule was risk_reward."""
    rows = [
        r for r in reqs
        if r.trade_plan and not (r.strategy_result or {}).get("candidate")
        and failed_conditions(r.strategy_result) == ["risk_reward"]
    ]
    if not rows:
        return {}
    starts = [r.candle_time + DECISION_BAR for r in rows]
    bars = await load_bars(s, instrument, min(starts), max(starts) + HYPOTHETICAL_HORIZON)
    times = [b.time for b in bars]
    out: dict[int, dict[str, Any]] = {}
    for r, start in zip(rows, starts, strict=True):
        h = hypothetical_outcome(r.trade_plan, start, bars[bisect_left(times, start):])
        if h is not None:
            out[r.id] = h
    return out


async def run_once(db: Database, settings: Settings, capital: Decimal | None = None) -> dict[str, Any]:
    metrics = await analyze(db, settings, capital=capital)
    period_start: datetime | None = metrics.pop("period_start")
    now = utcnow()
    async with db.session() as s:
        s.add(
            AnalysisReport(
                experiment=settings.experiment_name,
                period_start=period_start,
                period_end=now,
                metrics=json.loads(json.dumps(metrics, default=str)),
            )
        )
    metrics["period_start"] = period_start.isoformat() if period_start else None
    metrics["period_end"] = now.isoformat()
    return metrics


async def main() -> None:
    parser = argparse.ArgumentParser(description="Tradlysis experiment analyzer")
    parser.add_argument("--once", action="store_true", help="run a single analysis, print it and exit")
    parser.add_argument("--experiment", help="only this experiment (its id); default: all of them")
    args = parser.parse_args()
    base = get_settings()
    setup_logging(base.log_level)
    db = Database(base)
    try:
        await auto_create_tables(db, base)
        await import_env_once(db, base)
        while True:
            config = await load_configuration(db, base)
            experiments = config.experiments or single_experiment(config.settings).experiments
            if args.experiment:
                experiments = [e for e in experiments if e.slug == args.experiment]
            reports = {}
            for e in experiments:
                try:
                    m = await run_once(db, e.settings, capital=e.capital)
                    reports[e.slug] = m
                    log.info(
                        "Analysis stored for %s: %s opportunities, %s closed trades",
                        e.slug,
                        m["opportunities"],
                        m["closed_trades"].get("trades", 0),
                    )
                except Exception:
                    log.exception("analysis of %s failed", e.slug)
            if args.once:
                print(json.dumps(reports, indent=2, default=str))
                return
            await asyncio.sleep(config.settings.analyzer_interval_minutes * 60)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
