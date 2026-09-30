"""Account Reconciliation: keeps local state in line with Capital.com (the source of truth).

Every cycle:
  1. Open positions + account balances -> in-memory account view (+ periodic ``account_snapshots``).
  2. Open positions -> create/update local ``trades``; positions no order of ours explains are
     flagged as unexpected.
  3. Local OPEN trades missing at the broker -> close details from the activity history, mark
     CLOSED, compute R and P/L.
  4. New activity and transaction history -> ``broker_transactions`` (audit trail).
  5. Orders stuck in PENDING_SUBMIT/SUBMITTED/UNKNOWN -> resolved from broker state.
  6. Peak NAV, start-of-day NAV and the daily-loss / max-drawdown circuit breakers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.alerts.notifier import Notifier
from app.broker.capital import CapitalClient
from app.broker.types import AccountState, BrokerPosition
from app.config.settings import Settings
from app.db.control import get_control, set_control
from app.db.enums import ControlKey, Direction, TradeState
from app.db.models import AccountSnapshot, BrokerTransaction, Trade
from app.db.session import Database
from app.execution.executor import OrderExecutor
from app.market_data.timeutil import parse_time, trading_day, utcnow

log = logging.getLogger(__name__)
COMPONENT = "reconciliation"
# A local trade missing at the broker is closed without details after this long, if the
# activity history never shows the close (so it cannot block new trades forever).
CLOSE_DETAILS_GRACE_SECONDS = 60.0
# The activity/transaction audit trail re-reads this far back on every cycle (rows are de-duplicated).
HISTORY_OVERLAP = timedelta(minutes=10)


@dataclass
class AccountView:
    account_id: str
    currency: str
    balance: Decimal
    nav: Decimal
    unrealized_pl: Decimal
    margin_used: Decimal
    margin_available: Decimal
    open_trade_count: int
    open_position_count: int
    pending_order_count: int
    last_transaction_id: str | None
    fetched_at: datetime
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_state(cls, a: AccountState, positions: list[BrokerPosition], fetched_at: datetime) -> AccountView:
        return cls(
            account_id=a.account_id,
            currency=a.currency,
            balance=a.balance,
            nav=a.nav,
            unrealized_pl=a.unrealized_pl,
            margin_used=a.margin_used,
            margin_available=a.margin_available,
            open_trade_count=len(positions),
            open_position_count=len({p.instrument for p in positions}),
            pending_order_count=0,
            last_transaction_id=None,
            fetched_at=fetched_at,
            raw=a.raw,
        )


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        client: CapitalClient,
        db: Database,
        notifier: Notifier,
        executor: OrderExecutor,
    ) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.notifier = notifier
        self.executor = executor
        self.account: AccountView | None = None
        self.broker_open_trades: list[BrokerPosition] = []
        self.open_trades_fetched_at: datetime | None = None
        self._last_snapshot_at: datetime | None = None
        self._failures = 0
        self._missing_since: dict[str, datetime] = {}

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.reconcile_interval_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> bool:
        try:
            positions = await self.client.get_positions()
            await self._sync_account(positions)
            await self._sync_trades(positions)
            await self._sync_history()
            await self.executor.resolve_unresolved_orders(min_age_seconds=30)
            await self._update_breakers()
        except Exception as exc:
            self._failures += 1
            await self.notifier.error(
                COMPONENT,
                "RECONCILE_FAILED",
                f"Reconciliation failed ({self._failures} in a row): {exc!r}",
                dedup_key="reconcile_failed",
            )
            log.exception("reconciliation failed")
            return False
        if self._failures:
            await self.notifier.info(COMPONENT, "RECONCILE_RECOVERED", "Reconciliation recovered", alert=True)
        self._failures = 0
        return True

    # ------------------------------------------------------------------ account

    async def _sync_account(self, positions: list[BrokerPosition]) -> None:
        now = utcnow()
        self.account = AccountView.from_state(await self.client.get_account(), positions, now)
        interval = self.settings.account_snapshot_interval_seconds
        if self._last_snapshot_at is None or (now - self._last_snapshot_at).total_seconds() >= interval:
            a = self.account
            async with self.db.session() as s:
                s.add(
                    AccountSnapshot(
                        taken_at=now,
                        account_id=a.account_id,
                        currency=a.currency,
                        balance=a.balance,
                        nav=a.nav,
                        unrealized_pl=a.unrealized_pl,
                        margin_used=a.margin_used,
                        margin_available=a.margin_available,
                        open_trade_count=a.open_trade_count,
                        open_position_count=a.open_position_count,
                        pending_order_count=a.pending_order_count,
                        last_transaction_id=a.last_transaction_id,
                        raw=a.raw,
                    )
                )
            self._last_snapshot_at = now

    # ------------------------------------------------------------------ trades

    async def _sync_trades(self, positions: list[BrokerPosition]) -> None:
        self.broker_open_trades = positions
        self.open_trades_fetched_at = utcnow()
        broker_ids = {p.deal_id for p in positions}

        async with self.db.session() as s:
            local_open = (await s.scalars(select(Trade).where(Trade.state == str(TradeState.OPEN)))).all()
            local_by_id = {t.broker_trade_id: t for t in local_open}

        for p in positions:
            self._missing_since.pop(p.deal_id, None)
            await self._upsert_open_trade(p, local_by_id.get(p.deal_id))

        for trade in local_open:
            if trade.broker_trade_id not in broker_ids:
                await self._close_local_trade(trade)

    async def _upsert_open_trade(self, p: BrokerPosition, local: Trade | None) -> None:
        async with self.db.session() as s:
            if local is None:
                local = await s.scalar(select(Trade).where(Trade.broker_trade_id == p.deal_id))
            else:
                local = await s.get(Trade, local.id)
            if local is not None:
                local.current_units = p.units
                local.unrealized_pl = p.unrealized_pl
                local.stop_loss = p.stop_loss
                local.take_profit = p.take_profit
                local.state = str(TradeState.OPEN)
                return

        order = await self.executor.adopt_position(p)
        async with self.db.session() as s:
            if await s.scalar(select(Trade.id).where(Trade.broker_trade_id == p.deal_id)) is not None:
                return  # recorded meanwhile by the executor
            s.add(
                Trade(
                    broker_trade_id=p.deal_id,
                    order_id=order.id if order else None,
                    client_trade_id=order.client_order_id if order else None,
                    experiment=self.settings.experiment_name if order else None,
                    instrument=p.instrument,
                    direction=p.direction,
                    initial_units=p.units,
                    current_units=p.units,
                    open_price=p.open_price,
                    open_time=p.open_time,
                    stop_loss=p.stop_loss,
                    take_profit=p.take_profit,
                    initial_risk_price=abs(p.open_price - order.stop_loss) if order and order.stop_loss else None,
                    state=str(TradeState.OPEN),
                    unrealized_pl=p.unrealized_pl,
                    unexpected=order is None,
                    raw=p.raw,
                )
            )
        if order is None:
            await self.notifier.critical(
                COMPONENT,
                "UNEXPECTED_POSITION",
                f"Broker has an open position not created by this system: {p.instrument} "
                f"{p.units} units (deal {p.deal_id}). It counts toward exposure limits.",
                details=p.raw,
                dedup_key=f"unexpected_{p.deal_id}",
            )

    async def _close_local_trade(self, trade: Trade) -> None:
        if await self.client.get_position(trade.broker_trade_id) is not None:
            return  # raced with a fresh positions fetch; next cycle will see it
        now = utcnow()
        activity = await self.client.find_close_activity(trade.broker_trade_id, trade.open_time)
        if activity is None:
            first_missing = self._missing_since.setdefault(trade.broker_trade_id, now)
            if (now - first_missing).total_seconds() < CLOSE_DETAILS_GRACE_SECONDS:
                return  # the activity history can lag the positions list; retry next cycle
            await self.notifier.error(
                COMPONENT, "CLOSE_DETAILS_MISSING",
                f"Position {trade.broker_trade_id} is closed at the broker but its close is not in the "
                "activity history; recorded as closed without price or P/L",
                dedup_key=f"close_missing_{trade.broker_trade_id}",
            )
        self._missing_since.pop(trade.broker_trade_id, None)

        details = (activity or {}).get("details") or {}
        close_price = _float(details.get("level"))
        close_time = parse_time(str(activity["dateUTC"])) if activity and activity.get("dateUTC") else now
        reason = _close_reason(activity, trade, close_price)
        realized = self._realized_pl(trade, close_price)
        r_mult = r_multiple(trade.direction, trade.open_price, close_price, trade.initial_risk_price)
        async with self.db.session() as s:
            row = await s.get(Trade, trade.id)
            assert row is not None
            row.state = str(TradeState.CLOSED)
            row.close_price = close_price
            row.close_time = close_time
            row.close_reason = reason
            row.realized_pl = realized
            row.unrealized_pl = Decimal(0)
            row.current_units = 0
            row.r_multiple = r_mult
            row.raw = {**(row.raw or {}), "closed": activity, "realized_pl_source": "computed_from_prices"}
        await self.notifier.info(
            COMPONENT,
            "TRADE_CLOSED",
            f"{trade.direction} {trade.instrument} closed ({reason}) @ {close_price}: "
            f"P/L {realized} ({'n/a' if r_mult is None else f'{r_mult:+.2f}R'})",
            alert=True,
            dedup_key=f"closed_{trade.broker_trade_id}",
        )

    def _realized_pl(self, trade: Trade, close_price: float | None) -> Decimal | None:
        """P/L in account currency from open/close prices (the activity history carries no P/L).

        Exact when the account currency is the quote currency (USD for EUR/USD). The broker's own
        figures, including financing, are kept in ``broker_transactions``.
        """
        if close_price is None or self.account is None:
            return None
        base, _, quote = trade.instrument.partition("_")
        if self.account.currency == quote:
            rate = 1.0
        elif self.account.currency == base:
            rate = 1.0 / close_price
        else:
            return None
        move = close_price - trade.open_price
        pl = move * trade.initial_units * rate  # initial_units is signed
        return Decimal(str(round(pl, 6)))

    # ------------------------------------------------------------------ history

    async def _sync_history(self) -> None:
        now = utcnow()
        async with self.db.session() as s:
            ctl = await get_control(s, ControlKey.LAST_TRANSACTION_ID)
        cursor = (ctl or {}).get("value")
        if cursor is None:
            # First run: start the audit trail from now rather than replaying the account history.
            async with self.db.session() as s:
                await set_control(s, ControlKey.LAST_TRANSACTION_ID, {"value": now.isoformat()}, COMPONENT)
            return
        try:
            since = parse_time(str(cursor))
        except ValueError:
            since = now
        start = min(since, now - HISTORY_OVERLAP)
        activities = await self.client.get_activity(start, now)
        transactions = await self.client.get_transactions(start, now)
        rows = [_activity_row(a, self.client.account_id) for a in activities if a.get("dateUTC")]
        rows += [_transaction_row(t, self.client.account_id) for t in transactions if t.get("dateUtc")]
        if rows:
            async with self.db.session() as s:
                await s.execute(
                    insert(BrokerTransaction).values(rows).on_conflict_do_nothing(index_elements=["transaction_id"])
                )
        newest = max((r["time"] for r in rows), default=since)
        async with self.db.session() as s:
            await set_control(s, ControlKey.LAST_TRANSACTION_ID, {"value": max(newest, since).isoformat()}, COMPONENT)

    # ------------------------------------------------------------------ breakers

    async def _update_breakers(self) -> None:
        if self.account is None:
            return
        nav = self.account.nav
        today = trading_day(self.account.fetched_at).isoformat()
        async with self.db.session() as s:
            peak = await get_control(s, ControlKey.PEAK_NAV)
            if peak is None or nav > Decimal(str(peak["value"])):
                await set_control(s, ControlKey.PEAK_NAV, {"value": str(nav)}, COMPONENT)
                peak_nav = nav
            else:
                peak_nav = Decimal(str(peak["value"]))

            day = await get_control(s, ControlKey.DAY_START_NAV)
            if day is None or day.get("trading_day") != today:
                await set_control(s, ControlKey.DAY_START_NAV, {"trading_day": today, "value": str(nav)}, COMPONENT)
                day_nav = nav
                daily = await get_control(s, ControlKey.DAILY_LOSS_BREAKER)
                if daily and daily.get("tripped"):
                    await set_control(
                        s, ControlKey.DAILY_LOSS_BREAKER, {"tripped": False, "trading_day": today}, COMPONENT
                    )
            else:
                day_nav = Decimal(str(day["value"]))

            daily_pct = float((nav - day_nav) / day_nav * 100) if day_nav else 0.0
            dd_pct = float((peak_nav - nav) / peak_nav * 100) if peak_nav else 0.0

            daily = await get_control(s, ControlKey.DAILY_LOSS_BREAKER) or {}
            trip_daily = daily_pct <= -self.settings.max_daily_loss_pct and not daily.get("tripped")
            if trip_daily:
                await set_control(
                    s,
                    ControlKey.DAILY_LOSS_BREAKER,
                    {"tripped": True, "trading_day": today, "loss_pct": round(daily_pct, 3)},
                    COMPONENT,
                )
            dd = await get_control(s, ControlKey.DRAWDOWN_BREAKER) or {}
            trip_dd = dd_pct >= self.settings.max_drawdown_pct and not dd.get("tripped")
            if trip_dd:
                await set_control(
                    s,
                    ControlKey.DRAWDOWN_BREAKER,
                    {"tripped": True, "drawdown_pct": round(dd_pct, 3), "at": utcnow().isoformat()},
                    COMPONENT,
                )
        if trip_daily:
            await self.notifier.critical(
                COMPONENT, "DAILY_LOSS_BREAKER", f"Daily loss {daily_pct:.2f}% hit the limit; new trades halted until the next trading day"
            )
        if trip_dd:
            await self.notifier.critical(
                COMPONENT, "DRAWDOWN_BREAKER", f"Drawdown {dd_pct:.2f}% hit the limit; new trades halted until manually reset"
            )


# ---------------------------------------------------------------------- helpers


def r_multiple(direction: str, open_price: float, close_price: float | None, risk_dist: float | None) -> float | None:
    if close_price is None or not risk_dist:
        return None
    move = (close_price - open_price) if direction == Direction.BUY else (open_price - close_price)
    return round(move / risk_dist, 3)


SOURCE_REASONS = {"SL": "STOP_LOSS", "TP": "TAKE_PROFIT", "CLOSE_OUT": "MARGIN_CLOSEOUT"}


def _close_reason(activity: dict[str, Any] | None, trade: Trade, close_price: float | None) -> str:
    if activity is None:
        return "CLOSED_UNKNOWN"
    source = activity.get("source")
    if source in SOURCE_REASONS:
        return SOURCE_REASONS[source]
    if close_price is not None:
        tol = abs(trade.open_price) * 0.00002
        if trade.stop_loss is not None and abs(close_price - trade.stop_loss) <= tol:
            return "STOP_LOSS"
        if trade.take_profit is not None and abs(close_price - trade.take_profit) <= tol:
            return "TAKE_PROFIT"
    return "CLOSED"


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _activity_row(a: dict[str, Any], account_id: str) -> dict[str, Any]:
    details = a.get("details") or {}
    actions = details.get("actions") or []
    action = next((x.get("actionType") for x in actions if x.get("actionType")), None)
    trade_id = next((x.get("affectedDealId") for x in actions if x.get("affectedDealId")), None) or a.get("dealId")
    return {
        "transaction_id": f"activity:{a.get('dateUTC')}:{a.get('dealId')}:{a.get('type')}:{a.get('status')}:{action}",
        "account_id": account_id,
        "type": str(action or f"{a.get('type')}_{a.get('status')}"),
        "time": parse_time(str(a["dateUTC"])),
        "order_id": details.get("dealReference"),
        "trade_id": str(trade_id) if trade_id else None,
        "client_order_id": None,
        "reason": a.get("source"),
        "pl": None,
        "raw": a,
    }


def _transaction_row(t: dict[str, Any], account_id: str) -> dict[str, Any]:
    ttype = str(t.get("transactionType", "UNKNOWN"))
    amount = _float(t.get("size"))
    return {
        "transaction_id": f"transaction:{t.get('dateUtc')}:{ttype}:{t.get('reference')}:{t.get('size')}",
        "account_id": account_id,
        "type": ttype,
        "time": parse_time(str(t["dateUtc"])),
        "order_id": str(t["reference"]) if t.get("reference") else None,
        "trade_id": None,
        "client_order_id": None,
        "reason": t.get("note"),
        "pl": Decimal(str(amount)) if amount is not None and ttype in ("TRADE", "SWAP") else None,
        "raw": t,
    }
