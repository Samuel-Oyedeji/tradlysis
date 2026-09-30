"""Account Reconciliation: keeps local state in line with OANDA (the source of truth).

Every cycle:
  1. Account summary -> in-memory account view (+ periodic ``account_snapshots``).
  2. Open trades -> create/update local ``trades``; flag trades we did not open as unexpected.
  3. Local OPEN trades missing at the broker -> fetch final state, mark CLOSED, compute R.
  4. New broker transactions -> ``broker_transactions`` (audit trail of broker IDs).
  5. Orders stuck in PENDING_SUBMIT/SUBMITTED/UNKNOWN -> resolved by client-ID lookup.
  6. Peak NAV, start-of-day NAV and the daily-loss / max-drawdown circuit breakers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.alerts.notifier import Notifier
from app.broker.oanda import OandaClient
from app.config.settings import Settings
from app.db.control import get_control, set_control
from app.db.enums import ControlKey, Direction, TradeState
from app.db.models import AccountSnapshot, BrokerTransaction, Order, Trade
from app.db.session import Database
from app.execution.executor import OrderExecutor
from app.market_data.timeutil import parse_time, trading_day, utcnow

log = logging.getLogger(__name__)
COMPONENT = "reconciliation"


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
    def from_api(cls, a: dict[str, Any], fetched_at: datetime) -> AccountView:
        return cls(
            account_id=str(a.get("id", "")),
            currency=str(a.get("currency", "")),
            balance=Decimal(str(a.get("balance", "0"))),
            nav=Decimal(str(a.get("NAV", a.get("balance", "0")))),
            unrealized_pl=Decimal(str(a.get("unrealizedPL", "0"))),
            margin_used=Decimal(str(a.get("marginUsed", "0"))),
            margin_available=Decimal(str(a.get("marginAvailable", "0"))),
            open_trade_count=int(a.get("openTradeCount", 0)),
            open_position_count=int(a.get("openPositionCount", 0)),
            pending_order_count=int(a.get("pendingOrderCount", 0)),
            last_transaction_id=a.get("lastTransactionID"),
            fetched_at=fetched_at,
            raw=a,
        )


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        client: OandaClient,
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
        self.broker_open_trades: list[dict[str, Any]] = []
        self.open_trades_fetched_at: datetime | None = None
        self._last_snapshot_at: datetime | None = None
        self._failures = 0

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self.reconcile_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.settings.reconcile_interval_seconds)
            except TimeoutError:
                pass

    async def reconcile_once(self) -> bool:
        try:
            await self._sync_account()
            await self._sync_trades()
            await self._sync_transactions()
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

    async def _sync_account(self) -> None:
        now = utcnow()
        self.account = AccountView.from_api(await self.client.get_account_summary(), now)
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

    async def _sync_trades(self) -> None:
        broker_trades = await self.client.get_open_trades()
        self.broker_open_trades = broker_trades
        self.open_trades_fetched_at = utcnow()
        broker_ids = {t["id"] for t in broker_trades}

        async with self.db.session() as s:
            local_open = (await s.scalars(select(Trade).where(Trade.state == str(TradeState.OPEN)))).all()
            local_by_id = {t.broker_trade_id: t for t in local_open}

        for bt in broker_trades:
            await self._upsert_open_trade(bt, local_by_id.get(bt["id"]))

        for trade in local_open:
            if trade.broker_trade_id not in broker_ids:
                await self._close_local_trade(trade)

    async def _upsert_open_trade(self, bt: dict[str, Any], local: Trade | None) -> None:
        units = int(Decimal(str(bt.get("currentUnits", "0"))))
        sl = _price(bt.get("stopLossOrder"))
        tp = _price(bt.get("takeProfitOrder"))
        async with self.db.session() as s:
            if local is None:
                local = await s.scalar(select(Trade).where(Trade.broker_trade_id == bt["id"]))
            else:
                local = await s.get(Trade, local.id)
            if local is not None:
                local.current_units = units
                local.unrealized_pl = Decimal(str(bt.get("unrealizedPL", "0")))
                local.stop_loss = sl
                local.take_profit = tp
                local.state = str(TradeState.OPEN)
                return

            client_id = (bt.get("clientExtensions") or {}).get("id")
            order = None
            if client_id:
                order = await s.scalar(select(Order).where(Order.client_order_id == client_id))
            initial_units = int(Decimal(str(bt.get("initialUnits", units))))
            open_price = float(bt["price"])
            s.add(
                Trade(
                    broker_trade_id=bt["id"],
                    order_id=order.id if order else None,
                    client_trade_id=client_id,
                    experiment=self.settings.experiment_name if order else None,
                    instrument=bt["instrument"],
                    direction=str(Direction.BUY if initial_units > 0 else Direction.SELL),
                    initial_units=initial_units,
                    current_units=units,
                    open_price=open_price,
                    open_time=parse_time(bt["openTime"]),
                    stop_loss=sl,
                    take_profit=tp,
                    initial_risk_price=abs(open_price - order.stop_loss) if order and order.stop_loss else None,
                    state=str(TradeState.OPEN),
                    unrealized_pl=Decimal(str(bt.get("unrealizedPL", "0"))),
                    unexpected=order is None,
                    raw=bt,
                )
            )
        if order is None:
            await self.notifier.critical(
                COMPONENT,
                "UNEXPECTED_POSITION",
                f"Broker has an open trade not created by this system: {bt['instrument']} "
                f"{bt.get('currentUnits')} units (trade {bt['id']}). It counts toward exposure limits.",
                details=bt,
                dedup_key=f"unexpected_{bt['id']}",
            )

    async def _close_local_trade(self, trade: Trade) -> None:
        bt = await self.client.get_trade(trade.broker_trade_id)
        if bt is None:
            await self.notifier.error(
                COMPONENT, "TRADE_MISSING", f"Trade {trade.broker_trade_id} not found at broker",
                dedup_key=f"missing_{trade.broker_trade_id}",
            )
            return
        if bt.get("state") == "OPEN":
            return  # raced with a fresh open-trades fetch; next cycle will see it
        close_price = _float(bt.get("averageClosePrice"))
        realized = Decimal(str(bt.get("realizedPL", "0")))
        reason = _close_reason(bt, trade, close_price)
        r_mult = r_multiple(trade.direction, trade.open_price, close_price, trade.initial_risk_price)
        async with self.db.session() as s:
            row = await s.get(Trade, trade.id)
            assert row is not None
            row.state = str(TradeState.CLOSED)
            row.close_price = close_price
            row.close_time = parse_time(bt["closeTime"]) if bt.get("closeTime") else utcnow()
            row.close_reason = reason
            row.realized_pl = realized
            row.unrealized_pl = Decimal(0)
            row.financing = Decimal(str(bt.get("financing", "0")))
            row.current_units = 0
            row.r_multiple = r_mult
            row.raw = {**(row.raw or {}), "closed": bt}
        await self.notifier.info(
            COMPONENT,
            "TRADE_CLOSED",
            f"{trade.direction} {trade.instrument} closed ({reason}) @ {close_price}: "
            f"P/L {realized} ({'n/a' if r_mult is None else f'{r_mult:+.2f}R'})",
            alert=True,
            dedup_key=f"closed_{trade.broker_trade_id}",
        )

    # ------------------------------------------------------------------ transactions

    async def _sync_transactions(self) -> None:
        async with self.db.session() as s:
            ctl = await get_control(s, ControlKey.LAST_TRANSACTION_ID)
        last_id = (ctl or {}).get("value")
        if last_id is None:
            # First run: start the audit trail from now rather than replaying the account history.
            if self.account and self.account.last_transaction_id:
                async with self.db.session() as s:
                    await set_control(
                        s, ControlKey.LAST_TRANSACTION_ID, {"value": self.account.last_transaction_id}, COMPONENT
                    )
            return
        if self.account and self.account.last_transaction_id == last_id:
            return
        data = await self.client.get_transactions_since(str(last_id))
        txs = data.get("transactions", [])
        if txs:
            rows = [
                {
                    "transaction_id": str(t["id"]),
                    "account_id": str(t.get("accountID", self.client.account_id)),
                    "type": t.get("type", "UNKNOWN"),
                    "time": parse_time(t["time"]),
                    "order_id": t.get("orderID"),
                    "trade_id": _tx_trade_id(t),
                    "client_order_id": (t.get("clientExtensions") or {}).get("id") or t.get("clientOrderID"),
                    "reason": t.get("reason"),
                    "pl": Decimal(str(t["pl"])) if t.get("pl") is not None else None,
                    "raw": t,
                }
                for t in txs
            ]
            async with self.db.session() as s:
                await s.execute(insert(BrokerTransaction).values(rows).on_conflict_do_nothing())
        new_last = data.get("lastTransactionID") or (txs[-1]["id"] if txs else last_id)
        async with self.db.session() as s:
            await set_control(s, ControlKey.LAST_TRANSACTION_ID, {"value": str(new_last)}, COMPONENT)

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


def _close_reason(bt: dict[str, Any], trade: Trade, close_price: float | None) -> str:
    for key, label in (("stopLossOrder", "STOP_LOSS"), ("takeProfitOrder", "TAKE_PROFIT"), ("trailingStopLossOrder", "TRAILING_STOP")):
        order = bt.get(key)
        if isinstance(order, dict) and order.get("state") == "FILLED":
            return label
    if close_price is not None:
        tol = abs(trade.open_price) * 0.00002
        if trade.stop_loss is not None and abs(close_price - trade.stop_loss) <= tol:
            return "STOP_LOSS"
        if trade.take_profit is not None and abs(close_price - trade.take_profit) <= tol:
            return "TAKE_PROFIT"
    return "CLOSED"


def _price(order: Any) -> float | None:
    if isinstance(order, dict) and order.get("price") is not None:
        return float(order["price"])
    return None


def _float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _tx_trade_id(t: dict[str, Any]) -> str | None:
    if t.get("tradeID"):
        return str(t["tradeID"])
    opened = t.get("tradeOpened")
    if isinstance(opened, dict) and opened.get("tradeID"):
        return str(opened["tradeID"])
    closed = t.get("tradesClosed")
    if isinstance(closed, list) and closed:
        return str(closed[0].get("tradeID"))
    return None
