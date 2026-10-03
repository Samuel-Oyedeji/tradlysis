"""History: every trade taken and every setup that was stopped, each with a timeline tree.

An *item* is either
  * ``r-<decision_request_id>``: an opportunity where the deterministic rules found a setup
    or the model was asked (trades taken and setups stopped), or
  * ``t-<trade_id>``: a broker trade with no decision behind it (e.g. opened manually).

Everything here is read-only. The pure functions (``classify``, ``hypothetical_outcome``,
``build_timeline``) are unit-tested; the ``load_*`` functions only fetch rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BrokerTransaction,
    Candle,
    Decision,
    DecisionRequest,
    Order,
    RiskCheck,
    Trade,
)

DECISION_BAR = timedelta(minutes=15)
HYPOTHETICAL_HORIZON = timedelta(days=5)
PIP = 0.0001  # V1 trades EUR/USD only

# --------------------------------------------------------------------------- outcome


@dataclass
class Chain:
    """All rows belonging to one opportunity (any may be missing)."""

    request: DecisionRequest | None = None
    decision: Decision | None = None
    risk: RiskCheck | None = None
    order: Order | None = None
    trade: Trade | None = None
    transactions: list[BrokerTransaction] = field(default_factory=list)
    excursion: dict[str, Any] | None = None  # app.experiments.excursion.Excursion.to_dict()


@dataclass
class Outcome:
    category: str  # TRADED | STOPPED | PENDING
    code: str
    label: str
    stopped_at: str | None = None  # stage where the chain ended
    reasons: list[str] = field(default_factory=list)


def classify(chain: Chain) -> Outcome:
    t, o, rc, d = chain.trade, chain.order, chain.risk, chain.decision
    if t is not None:
        if t.state == "OPEN":
            return Outcome("TRADED", "OPEN", "Trade open")
        r = t.r_multiple
        pl = float(t.realized_pl or 0)
        value = r if r is not None else pl
        if value > 0:
            return Outcome("TRADED", "WON", f"Won {r:+.2f}R" if r is not None else "Won")
        if value < 0:
            return Outcome("TRADED", "LOST", f"Lost {r:+.2f}R" if r is not None else "Lost")
        return Outcome("TRADED", "BREAKEVEN", "Breakeven")
    if o is not None:
        if o.status in ("REJECTED", "CANCELLED", "FAILED"):
            reason = o.reject_reason or o.status
            return Outcome("STOPPED", "ORDER_FAILED", f"Order {o.status.lower()}", "order", [reason])
        if o.status == "FILLED":
            return Outcome("PENDING", "FILLED_AWAITING_SYNC", "Filled, awaiting reconciliation")
        return Outcome("PENDING", "ORDER_PENDING", f"Order {o.status.lower()}", "order")
    if rc is not None:
        if not rc.approved:
            reasons = list(rc.rejection_reasons or [])
            return Outcome("STOPPED", "STOPPED_BY_RISK", "Stopped by risk engine", "risk", reasons)
        return Outcome("STOPPED", "APPROVED_NOT_SENT", "Approved but no order recorded", "order")
    if d is not None:
        codes = list(d.reason_codes or [])
        if d.source == "ERROR":
            return Outcome("STOPPED", "MODEL_ERROR", "Model response invalid → WAIT", "decision",
                           [d.validation_error or "error"])
        if d.source == "PREFILTER":
            if "NEWS_BLACKOUT" in codes:
                return Outcome("STOPPED", "STOPPED_BY_NEWS", "Stopped by news blackout", "decision", codes)
            if "LLM_NOT_CONFIGURED" in codes:
                return Outcome("STOPPED", "MODEL_NOT_CONFIGURED", "Model not configured", "decision", codes)
            return Outcome("STOPPED", "NO_SETUP", "No setup", "setup", codes)
        if d.decision == "WAIT":
            return Outcome("STOPPED", "STOPPED_BY_MODEL", "Model said WAIT", "decision", codes)
    return Outcome("PENDING", "IN_PROGRESS", "In progress")


# --------------------------------------------------------------------------- hypothetical


@dataclass
class Bar:
    time: datetime
    high: float
    low: float
    close: float


def hypothetical_outcome(plan: dict[str, Any] | None, decided_at: datetime, bars: Sequence[Bar]) -> dict[str, Any] | None:
    """Walk forward through M15 candles after the decision: which was hit first, TP or SL?

    Mid prices are used, so this ignores spread and slippage; if both levels fall inside the
    same candle the order is unknown and the result is AMBIGUOUS.
    """
    if not plan:
        return None
    try:
        long = plan["direction"] == "BUY"
        sl = float(plan["stop_loss"])
        tp = float(plan["take_profit"])
        rr = float(plan.get("risk_reward") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    horizon = decided_at + HYPOTHETICAL_HORIZON
    n = 0
    for b in bars:
        if b.time < decided_at:
            continue
        if b.time > horizon:
            break
        n += 1
        hit_sl = b.low <= sl if long else b.high >= sl
        hit_tp = b.high >= tp if long else b.low <= tp
        if hit_sl and hit_tp:
            return {"result": "AMBIGUOUS", "label": "TP and SL touched in the same candle", "r": None,
                    "resolved_at": b.time.isoformat(), "bars": n}
        if hit_tp:
            return {"result": "WOULD_WIN", "label": f"Would have won +{rr:.2f}R", "r": rr,
                    "resolved_at": b.time.isoformat(), "bars": n}
        if hit_sl:
            return {"result": "WOULD_LOSE", "label": "Would have lost -1.00R", "r": -1.0,
                    "resolved_at": b.time.isoformat(), "bars": n}
    done = bars and bars[-1].time >= horizon
    return {"result": "EXPIRED" if done else "UNRESOLVED",
            "label": "Neither level hit within 5 days" if done else "Neither level hit yet",
            "r": None, "resolved_at": None, "bars": n}


# --------------------------------------------------------------------------- timeline tree


def _node(key: str, title: str, status: str, time: datetime | None = None, summary: str = "",
          details: list[tuple[str, Any]] | None = None, children: list[dict[str, Any]] | None = None,
          raw: Any = None) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "status": status,  # pass | fail | info | pending | win | loss | warn
        "time": time.isoformat() if time else None,
        "summary": summary,
        "details": [{"label": k, "value": v} for k, v in (details or []) if v is not None and v != ""],
        "children": children or [],
        "raw": raw,
    }


def _p(x: Any) -> str | None:
    return None if x is None else f"{float(x):.5f}"


def _m(x: Any) -> str | None:
    return None if x is None else f"{float(x):,.2f}"


# Close reasons the broker reports without a specific trigger: closed in the Capital.com platform or with "close all".
CLOSE_TITLES = {"CLOSED": "closed manually", "CLOSED_UNKNOWN": "closed (details unavailable)"}


def _r(x: Any, signed: bool = False) -> str | None:
    if x is None:
        return None
    return f"{float(x):+.2f}R" if signed else f"{float(x):.2f}R"


def build_timeline(chain: Chain, hypothetical: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    req, d, rc, o, t = chain.request, chain.decision, chain.risk, chain.order, chain.trade
    outcome = classify(chain)

    if req is not None:
        snap = req.snapshot or {}
        price = snap.get("price", {})
        decided_at = req.candle_time + DECISION_BAR
        nodes.append(_node(
            "candle", "15-minute candle closed", "info", decided_at,
            f"Bid {price.get('bid')} · Ask {price.get('ask')} · spread {price.get('spread_pips')} pips",
        ))

        strat = req.strategy_result or {}
        plan = req.trade_plan
        conds = [
            _node(f"cond-{c.get('name')}", str(c.get("name", "")).replace("_", " "),
                  "pass" if c.get("passed") else "fail", summary=str(c.get("detail", "")))
            for c in strat.get("conditions", [])
        ]
        plan_details = []
        if plan:
            plan_details = [
                ("Direction", plan.get("direction")), ("Entry", _p(plan.get("entry"))),
                ("Stop-loss", f"{_p(plan.get('stop_loss'))} ({plan.get('risk_pips')} pips, {plan.get('stop_source')})"),
                ("Take-profit", f"{_p(plan.get('take_profit'))} ({plan.get('reward_pips')} pips, {plan.get('target_source')})"),
                ("Risk:reward", f"1:{plan.get('risk_reward')}"),
            ]
        nodes.append(_node(
            "setup", "Trend-pullback setup " + ("found" if strat.get("candidate") else "not complete"),
            "pass" if strat.get("candidate") else "fail", req.created_at,
            (f"{strat.get('direction')} setup" if strat.get("direction") else "No higher-timeframe direction")
            + ("" if strat.get("candidate") else f" · failed: {', '.join(strat.get('failure_codes', []))}"),
            plan_details, conds,
        ))

        tfs = snap.get("timeframes", {})
        news = snap.get("news", {})
        regime = (snap.get("market_regime") or {}).get("label")
        ctx_children = [
            _node(f"tf-{k}", k.upper(), "info",
                  summary=f"{v.get('trend')} · {str(v.get('structure', '')).replace('_', ' ')} · RSI {v.get('rsi14')} · ATR {v.get('atr14_pips')} pips")
            for k, v in tfs.items()
        ]
        nxt = news.get("next_high_impact_event")
        news_bits = [f"risk {news.get('risk')}"]
        if news.get("blackout"):
            news_bits.append("BLACKOUT: " + ", ".join(e.get("title", "") for e in news.get("blackout_events", [])))
        if nxt:
            news_bits.append(f"next high-impact: {nxt.get('currency')} {nxt.get('title')} in {nxt.get('minutes_until')} min")
        bias = news.get("currency_bias") or {}
        if bias:
            news_bits.append("bias " + ", ".join(f"{c} {b.get('bias')}" for c, b in bias.items()))
        ctx_children.append(_node("news", "News", "warn" if news.get("blackout") else "info", summary=" · ".join(news_bits)))
        vol = snap.get("volatility", {})
        nodes.append(_node(
            "context", "Market context", "info", None,
            f"Regime {regime or 'n/a'} · volatility {vol.get('regime')} (ATR H1 {vol.get('atr_h1_pips')} pips)",
            children=ctx_children, raw=snap,
        ))

    if d is not None:
        codes = list(d.reason_codes or [])
        rationale = (d.raw_response or {}).get("rationale") if isinstance(d.raw_response, dict) else None
        if d.source == "PREFILTER":
            nodes.append(_node("decision", "Model not asked", "fail", d.created_at,
                               "Stopped before the model: " + ", ".join(codes)))
        elif d.source == "ERROR":
            nodes.append(_node("decision", "Model response rejected → WAIT", "fail", d.created_at,
                               d.validation_error or "", [("Latency", f"{d.latency_ms} ms" if d.latency_ms else None)],
                               raw=d.raw_response))
        else:
            is_trade = d.decision in ("BUY", "SELL")
            nodes.append(_node(
                "decision", f"Model decided {d.decision}", "pass" if is_trade else "fail", d.created_at,
                f"confidence {d.confidence:.2f}" if d.confidence is not None else "",
                [("Reasons", ", ".join(codes)), ("Rationale", rationale),
                 ("Model", req.model if req else None), ("Prompt", req.prompt_version if req else None),
                 ("Latency", f"{d.latency_ms} ms" if d.latency_ms else None)],
                raw=d.raw_response,
            ))

    if rc is not None:
        checks = [
            _node(f"check-{c.get('name')}", str(c.get("name", "")).replace("_", " ").lower(),
                  "pass" if c.get("passed") else "fail", summary=str(c.get("detail", "")))
            for c in (rc.checks or [])
        ]
        failed = [c for c in checks if c["status"] == "fail"]
        passed = [c for c in checks if c["status"] == "pass"]
        nodes.append(_node(
            "risk", "Risk engine " + ("approved" if rc.approved else "rejected"),
            "pass" if rc.approved else "fail", rc.created_at,
            f"{len(passed)}/{len(checks)} checks passed"
            + ("" if rc.approved else " · " + ", ".join(rc.rejection_reasons or [])),
            [("Units", rc.units), ("Risk", f"{rc.risk_pct}% ({_m(rc.risk_amount)})" if rc.risk_pct is not None else None),
             ("Entry", _p(rc.entry_price)), ("Stop-loss", _p(rc.stop_loss)), ("Take-profit", _p(rc.take_profit)),
             ("Risk:reward", f"1:{rc.risk_reward}" if rc.risk_reward else None)],
            failed + passed,  # failures first
        ))

    if o is not None:
        slip = None
        if o.fill_price is not None and o.requested_price is not None:
            adverse = (o.fill_price - o.requested_price) if o.direction == "BUY" else (o.requested_price - o.fill_price)
            slip = f"{adverse / PIP:+.1f} pips"
        status = {"FILLED": "pass", "REJECTED": "fail", "CANCELLED": "fail", "FAILED": "fail"}.get(o.status, "pending")
        nodes.append(_node(
            "order", f"Order {o.status.lower()}", status, o.submitted_at or o.created_at,
            f"{o.direction} {abs(o.units)} {o.instrument}",
            [("Client ID", o.client_order_id), ("Requested", _p(o.requested_price)), ("Price bound", _p(o.price_bound)),
             ("Fill", _p(o.fill_price)), ("Slippage", slip), ("Attempts", o.attempts), ("Reason", o.reject_reason)],
            raw={"request": o.request_payload, "response": o.response_payload},
        ))

    if t is not None:
        nodes.append(_node(
            "opened", "Trade opened" + (" (not created by Tradlysis)" if t.unexpected else ""),
            "warn" if t.unexpected else "pass", t.open_time,
            f"{t.direction} {abs(t.initial_units)} @ {_p(t.open_price)}",
            [("Broker trade", t.broker_trade_id), ("Stop-loss", _p(t.stop_loss)), ("Take-profit", _p(t.take_profit))],
        ))
        for tx in chain.transactions:
            nodes.append(_node(
                f"tx-{tx.transaction_id}", tx.type.replace("_", " ").lower(), "info", tx.time,
                ", ".join(x for x in [tx.reason, f"P/L {_m(tx.pl)}" if tx.pl is not None else None] if x),
                [("Transaction", tx.transaction_id)], raw=tx.raw,
            ))
        ex = chain.excursion or {}
        excursion_rows = [
            ("Best point reached", _r(ex.get("best_r"), signed=True) if ex else None),
            ("Worst point reached", _r(-ex["worst_r"], signed=True) if ex.get("worst_r") is not None else None),
            ("Profit given back", _r(ex.get("given_back_r")) if ex.get("given_back_r") else None),
            ("Take-profit was at", _r(ex.get("target_r"), signed=True) if ex.get("target_r") else None),
        ]
        if t.state == "OPEN":
            nodes.append(_node("open", "Trade still open", "pending", None,
                               f"Unrealized P/L {_m(t.unrealized_pl)}" if t.unrealized_pl is not None else "",
                               excursion_rows))
        else:
            dur = (t.close_time - t.open_time) if t.close_time else None
            win = outcome.code == "WON"
            nodes.append(_node(
                "closed", f"Trade closed: {CLOSE_TITLES.get(t.close_reason or '', (t.close_reason or 'closed').replace('_', ' ').lower())}",
                "win" if win else ("loss" if outcome.code == "LOST" else "info"), t.close_time,
                outcome.label + (f" · P/L {_m(t.realized_pl)}" if t.realized_pl is not None else ""),
                [("Close price", _p(t.close_price)), ("Financing", _m(t.financing)),
                 ("Duration", _fmt_duration(dur) if dur else None), *excursion_rows],
            ))
    elif hypothetical is not None:
        status = {"WOULD_WIN": "win", "WOULD_LOSE": "loss"}.get(hypothetical["result"], "info")
        nodes.append(_node(
            "hypothetical", "What would have happened (not traded)", status,
            datetime.fromisoformat(hypothetical["resolved_at"]) if hypothetical.get("resolved_at") else None,
            hypothetical["label"],
            [("Candles checked", hypothetical.get("bars")),
             ("Note", "Estimated from mid-price candles; ignores spread and slippage")],
        ))

    # Mark where the chain was stopped.
    if outcome.category == "STOPPED" and outcome.stopped_at:
        for n in reversed(nodes):
            if n["key"] == outcome.stopped_at:
                n["stopped_here"] = True
                break
    return nodes


def _fmt_duration(d: timedelta) -> str:
    mins = int(d.total_seconds() // 60)
    h, m = divmod(mins, 60)
    days, h = divmod(h, 24)
    return (f"{days}d " if days else "") + (f"{h}h " if h or days else "") + f"{m}m"


# --------------------------------------------------------------------------- loading


def _qualifying(experiment: str):
    return select(DecisionRequest).where(
        DecisionRequest.experiment == experiment,
        or_(DecisionRequest.llm_called.is_(True), DecisionRequest.strategy_result["candidate"].as_boolean().is_(True)),
    )


async def load_chains(session: AsyncSession, experiment: str, limit: int = 2000) -> list[Chain]:
    reqs = (await session.scalars(_qualifying(experiment).order_by(DecisionRequest.candle_time.desc()).limit(limit))).all()
    chains = await _attach(session, list(reqs))
    # Broker trades not linked to any decision (e.g. opened manually on the account).
    orphans = (await session.scalars(select(Trade).where(Trade.order_id.is_(None)).order_by(Trade.open_time.desc()))).all()
    chains += [Chain(trade=t) for t in orphans]
    return chains


async def load_chain(session: AsyncSession, item_id: str) -> Chain | None:
    kind, _, raw_id = item_id.partition("-")
    if not raw_id.isdigit():
        return None
    if kind == "r":
        req = await session.get(DecisionRequest, int(raw_id))
        if req is None:
            return None
        chain = (await _attach(session, [req]))[0]
    elif kind == "t":
        trade = await session.get(Trade, int(raw_id))
        if trade is None:
            return None
        chain = Chain(trade=trade)
    else:
        return None
    if chain.trade is not None:
        chain.transactions = list((await session.scalars(
            select(BrokerTransaction)
            .where(BrokerTransaction.trade_id == chain.trade.broker_trade_id)
            .order_by(BrokerTransaction.time)
        )).all())
    return chain


async def _attach(session: AsyncSession, reqs: list[DecisionRequest]) -> list[Chain]:
    ids = [r.id for r in reqs]
    if not ids:
        return []
    decs = {d.request_id: d for d in (await session.scalars(select(Decision).where(Decision.request_id.in_(ids)))).all()}
    rcs = {r.request_id: r for r in (await session.scalars(select(RiskCheck).where(RiskCheck.request_id.in_(ids)))).all()}
    rc_ids = [r.id for r in rcs.values()]
    orders = {}
    trades = {}
    if rc_ids:
        orders = {o.risk_check_id: o for o in (await session.scalars(select(Order).where(Order.risk_check_id.in_(rc_ids)))).all()}
        o_ids = [o.id for o in orders.values()]
        if o_ids:
            trades = {t.order_id: t for t in (await session.scalars(select(Trade).where(Trade.order_id.in_(o_ids)))).all()}
    out = []
    for r in reqs:
        rc = rcs.get(r.id)
        o = orders.get(rc.id) if rc else None
        out.append(Chain(request=r, decision=decs.get(r.id), risk=rc, order=o, trade=trades.get(o.id) if o else None))
    return out


async def load_bars(session: AsyncSession, instrument: str, start: datetime, end: datetime) -> list[Bar]:
    rows = (await session.execute(
        select(Candle.time, Candle.high, Candle.low, Candle.close)
        .where(Candle.instrument == instrument, Candle.granularity == "M15", Candle.complete.is_(True),
               Candle.time >= start, Candle.time <= end)
        .order_by(Candle.time)
    )).all()
    return [Bar(t, h, lo, c) for t, h, lo, c in rows]


def item_id(chain: Chain) -> str:
    return f"r-{chain.request.id}" if chain.request is not None else f"t-{chain.trade.id}"  # type: ignore[union-attr]


def item_summary(chain: Chain, hypothetical: dict[str, Any] | None) -> dict[str, Any]:
    oc = classify(chain)
    req, d, t = chain.request, chain.decision, chain.trade
    snap = (req.snapshot or {}) if req else {}
    plan = req.trade_plan if req else None
    direction = (plan or {}).get("direction") or (req.strategy_result or {}).get("direction") if req else None
    if direction is None and t is not None:
        direction = t.direction
    time = (req.candle_time + DECISION_BAR) if req else (t.open_time if t else None)
    return {
        "id": item_id(chain),
        "time": time.isoformat() if time else None,
        "direction": direction,
        "category": "TRADED" if (t is not None and oc.category == "TRADED") else oc.category,
        "outcome": oc.code if t is None or not t.unexpected else "EXTERNAL",
        "label": oc.label if t is None or not t.unexpected else f"External trade · {oc.label}",
        "stopped_at": oc.stopped_at,
        "reasons": oc.reasons[:4],
        "confidence": d.confidence if d else None,
        "decision": d.decision if d else None,
        "r_multiple": t.r_multiple if t else None,
        "pl": float(t.realized_pl) if t is not None and t.realized_pl is not None else None,
        "entry": (plan or {}).get("entry") if plan else (t.open_price if t else None),
        "risk_reward": (plan or {}).get("risk_reward") if plan else None,
        "regime": (snap.get("market_regime") or {}).get("label"),
        "news_risk": (snap.get("news") or {}).get("risk"),
        "hypothetical": hypothetical,
    }
