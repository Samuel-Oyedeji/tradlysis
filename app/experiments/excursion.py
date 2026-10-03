"""Trade excursions: how far each trade went in our favour and against us, in R.

R is the trade's initial risk (entry to stop-loss). For every trade this measures:
  * best_r   - the furthest it went in our favour (maximum favourable excursion), >= 0
  * worst_r  - the furthest it went against us (maximum adverse excursion), >= 0
  * result_r - where it closed (or where it is now, for an open trade)
  * given_back_r - best_r minus result_r: profit seen but not kept
  * reached  - whether it got to +1R, +2R and +3R before closing

It is computed from stored candles rather than recorded live, so it works for every trade ever
taken and survives restarts. It uses 5-minute candles where they cover a 15-minute slot and
15-minute candles otherwise, with mid prices (spread ignored, about 0.1R on a 7-pip stop or less).
Candles that straddle the open or close can include prices just outside the trade, so the figures
are a close approximation, not tick-exact.

Close categories (``close_category``): take_profit, stop_loss, manual (closed by you, in the
Capital.com platform or with the dashboard's "close all"), bot_exit (closed by the bot's own
slippage guard), margin_closeout, unknown.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Candle, Order, Trade

LEVELS = (1.0, 2.0, 3.0)
M5 = timedelta(minutes=5)
M15 = timedelta(minutes=15)
OUTCOMES = ("take_profit", "stop_loss", "manual", "bot_exit", "margin_closeout", "unknown")


@dataclass(frozen=True)
class PathBar:
    time: datetime  # open time, UTC
    duration: timedelta
    high: float
    low: float
    close: float


@dataclass
class Excursion:
    best_r: float
    worst_r: float
    result_r: float | None
    given_back_r: float | None
    target_r: float | None
    reached: dict[str, bool] = field(default_factory=dict)
    best_at: datetime | None = None
    bars: int = 0
    open: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["best_at"] = self.best_at.isoformat() if self.best_at else None
        return d


def risk_distance(trade: Trade) -> float | None:
    if trade.initial_risk_price:
        return float(trade.initial_risk_price)
    if trade.stop_loss is not None:
        dist = abs(trade.open_price - trade.stop_loss)
        return dist or None
    return None


def compute_excursion(
    *,
    direction: str,
    entry: float,
    risk: float | None,
    start: datetime,
    end: datetime,
    path: list[PathBar],
    close_price: float | None = None,
    take_profit: float | None = None,
    is_open: bool = False,
    exit_level: str | None = None,
) -> Excursion | None:
    """Excursion of a trade from ``start`` to ``end`` over a candle path. None without risk or candles.

    ``exit_level`` is "stop" or "target" when the trade closed at its stop-loss / take-profit: the candle
    that hit the level may run past it after the trade was already closed, so that side is capped at the
    close.
    """
    if not risk or risk <= 0:
        return None
    window = [b for b in path if b.time < end and b.time + b.duration > start]
    if not window:
        return None
    long = direction == "BUY"
    best = worst = 0.0
    best_at: datetime | None = None
    for b in window:
        fav = (b.high - entry) if long else (entry - b.low)
        adv = (entry - b.low) if long else (b.high - entry)
        if fav / risk > best:
            best, best_at = fav / risk, b.time
        worst = max(worst, adv / risk)
    # Open trades: "result" is where the latest candle closed. Closed trades without a known close price: unknown.
    last = window[-1].close if is_open else close_price
    result = None if last is None else ((last - entry) if long else (entry - last)) / risk
    target = abs(take_profit - entry) / risk if take_profit is not None else None
    if result is not None:
        # The close itself is a price the trade reached, even if the candle path missed it.
        best = max(best, result)
        worst = max(worst, -result)
        if exit_level == "stop":
            worst = max(-result, 0.0)
        elif exit_level == "target":
            best = max(result, 0.0)
    return Excursion(
        best_r=round(best, 3),
        worst_r=round(worst, 3),
        result_r=None if result is None else round(result, 3),
        given_back_r=None if result is None else round(max(best - result, 0.0), 3),
        target_r=round(target, 3) if target is not None else None,
        reached={f"{lvl:g}R": best >= lvl for lvl in LEVELS},
        best_at=best_at,
        bars=len(window),
        open=is_open,
    )


def close_category(trade: Trade, close_reasons_by_deal: dict[str, list[str]]) -> str | None:
    """How a closed trade ended (None while it is open)."""
    if trade.state != "CLOSED":
        return None
    bot_closes = close_reasons_by_deal.get(trade.broker_trade_id, [])
    if any("slippage" in r.lower() for r in bot_closes):
        return "bot_exit"
    reason = (trade.close_reason or "").upper()
    if reason == "TAKE_PROFIT":
        return "take_profit"
    if reason in ("STOP_LOSS", "TRAILING_STOP"):
        return "stop_loss"
    if reason == "MARGIN_CLOSEOUT":
        return "margin_closeout"
    if bot_closes or reason == "CLOSED":
        return "manual"  # dashboard "close all", or closed in the Capital.com platform
    return "unknown"


def summarize(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-trade rows (each with ``outcome`` and ``excursion``) for the analysis page."""

    def avg(values: list[float]) -> float | None:
        return round(sum(values) / len(values), 3) if values else None

    counts = {o: 0 for o in OUTCOMES}
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
        groups[t["outcome"]].append(t)

    def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
        ex = [r["excursion"] for r in rows if r.get("excursion")]
        return {
            "trades": len(rows),
            "avg_result_r": avg([e["result_r"] for e in ex if e["result_r"] is not None]),
            "avg_best_r": avg([e["best_r"] for e in ex]),
            "avg_worst_r": avg([e["worst_r"] for e in ex]),
            "avg_given_back_r": avg([e["given_back_r"] for e in ex if e["given_back_r"] is not None]),
            "measured": len(ex),
        }

    missed = [t for t in trades if t["outcome"] != "take_profit"]
    missed_ex = [t["excursion"] for t in missed if t.get("excursion")]
    return {
        "counts": {k: v for k, v in counts.items() if v or k in ("take_profit", "stop_loss", "manual")},
        "by_outcome": {k: stats(v) for k, v in groups.items()},
        "all": stats(trades),
        "not_reaching_tp": {
            "trades": len(missed),
            "measured": len(missed_ex),
            "reached": {f"{lvl:g}R": sum(1 for e in missed_ex if e["reached"].get(f"{lvl:g}R")) for lvl in LEVELS},
            "avg_best_r": avg([e["best_r"] for e in missed_ex]),
        },
        "avg_target_r": avg([t["excursion"]["target_r"] for t in trades if t.get("excursion") and t["excursion"]["target_r"]]),
    }


# ---------------------------------------------------------------------- loading


async def load_path(session: AsyncSession, instrument: str, start: datetime, end: datetime) -> list[PathBar]:
    """Candle path covering [start, end]: 5-minute bars where a whole 15-minute slot is covered, else 15-minute."""

    async def bars(granularity: str, lo: datetime) -> list[tuple[datetime, float, float, float]]:
        rows = await session.execute(
            select(Candle.time, Candle.high, Candle.low, Candle.close)
            .where(
                Candle.instrument == instrument,
                Candle.granularity == granularity,
                Candle.complete.is_(True),
                Candle.time >= lo,
                Candle.time <= end,
            )
            .order_by(Candle.time)
        )
        return [tuple(r) for r in rows.all()]  # type: ignore[misc]

    m15 = await bars("M15", start - M15)
    m5 = await bars("M5", start - M15)
    slots: dict[datetime, list[PathBar]] = defaultdict(list)
    for t, h, lo, c in m5:
        slot = t - timedelta(minutes=t.minute % 15, seconds=t.second, microseconds=t.microsecond)
        slots[slot].append(PathBar(t, M5, h, lo, c))
    path: list[PathBar] = []
    for t, h, lo, c in m15:
        fine = slots.get(t, [])
        path.extend(fine if len(fine) == 3 else [PathBar(t, M15, h, lo, c)])
    # Still-forming 15-minute slot (open trades): complete 5-minute bars after the last 15-minute bar.
    last_end = (m15[-1][0] + M15) if m15 else start - M15
    path.extend(PathBar(t, M5, h, lo, c) for t, h, lo, c in m5 if t >= last_end)
    return path


async def trade_excursion(session: AsyncSession, trade: Trade, now: datetime) -> Excursion | None:
    end = trade.close_time or now
    path = await load_path(session, trade.instrument, trade.open_time, end)
    return compute_excursion(
        direction=trade.direction,
        entry=trade.open_price,
        risk=risk_distance(trade),
        start=trade.open_time,
        end=end,
        path=path,
        close_price=trade.close_price,
        take_profit=trade.take_profit,
        is_open=trade.state != "CLOSED",
        exit_level={"STOP_LOSS": "stop", "TRAILING_STOP": "stop", "TAKE_PROFIT": "target"}.get(
            (trade.close_reason or "").upper()
        ),
    )


async def bot_close_reasons(session: AsyncSession) -> dict[str, list[str]]:
    """Reasons of the close orders the bot sent, keyed by deal ID (from the order's request payload)."""
    rows = (await session.scalars(select(Order).where(Order.purpose == "CLOSE"))).all()
    out: dict[str, list[str]] = defaultdict(list)
    for o in rows:
        payload = o.request_payload or {}
        deal = payload.get("deal_id")
        if deal:
            out[str(deal)].append(str(payload.get("reason", "")))
    return out
