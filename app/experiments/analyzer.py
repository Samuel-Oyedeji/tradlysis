"""Experiment Analyzer: periodically summarises the live experiment. Never trades.

Run continuously:  python -m app.experiments.analyzer
Run once & print:  python -m app.experiments.analyzer --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.config.settings import Settings, get_settings
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
from app.logging_setup import setup_logging
from app.market_data.timeutil import utcnow

log = logging.getLogger("tradlysis.analyzer")

CONFIDENCE_BUCKETS = [(0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


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
        "execution": {
            "avg_slippage_pips": round(sum(slippages) / len(slippages), 3) if slippages else None,
            "max_slippage_pips": round(max(slippages), 3) if slippages else None,
            "avg_spread_pips_at_decision": round(sum(spreads) / len(spreads), 3) if spreads else None,
            "max_spread_pips_at_decision": round(max(spreads), 3) if spreads else None,
            "avg_llm_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        },
    }


# ---------------------------------------------------------------------- DB loading


async def analyze(db: Database, settings: Settings, pip_size: float = 0.0001) -> dict[str, Any]:
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
        navs = (await s.scalars(select(AccountSnapshot.nav).order_by(AccountSnapshot.taken_at))).all()

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
            )
        )

    metrics = compute_metrics(
        requests=[
            {"snapshot": r.snapshot or {}, "strategy_result": r.strategy_result, "llm_called": r.llm_called}
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


async def run_once(db: Database, settings: Settings) -> dict[str, Any]:
    metrics = await analyze(db, settings)
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
    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)
    db = Database(settings)
    try:
        await auto_create_tables(db, settings)
        if args.once:
            print(json.dumps(await run_once(db, settings), indent=2, default=str))
            return
        while True:
            try:
                m = await run_once(db, settings)
                log.info(
                    "Analysis stored: %s opportunities, %s closed trades",
                    m["opportunities"],
                    m["closed_trades"].get("trades", 0),
                )
            except Exception:
                log.exception("analysis failed")
            await asyncio.sleep(settings.analyzer_interval_minutes * 60)
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
