"""Order Executor: the only component allowed to submit orders to the broker.

Guarantees:
  * An order row (status PENDING_SUBMIT) is committed *before* the request is sent, with a
    unique ``client_order_id`` that is also sent to OANDA as ``clientExtensions.id``. The
    ``risk_check_id`` column is unique, so one approved risk check can create at most one order.
  * A timeout or connection error never counts as "not filled" or "filled": the order becomes
    UNKNOWN and is resolved by looking it up by client ID. Only if the broker confirms the
    order does not exist is it resubmitted (same client ID, bounded attempts).
  * ``positionFill=OPEN_ONLY`` so an entry order can never close or reduce another position.
  * The kill switch is re-read immediately before submission.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.alerts.notifier import Notifier
from app.broker.oanda import InstrumentInfo, OandaClient, OandaTransportError
from app.config.settings import Settings
from app.db.control import is_kill_switch_active
from app.db.enums import OPEN_ORDER_STATUSES, Direction, OrderPurpose, OrderStatus, TradeState
from app.db.models import Order, Trade
from app.db.session import Database
from app.market_data.timeutil import parse_time, utcnow
from app.risk.engine import RiskResult

log = logging.getLogger(__name__)
COMPONENT = "execution"
MAX_SUBMIT_ATTEMPTS = 2
LOOKUP_DELAY_SECONDS = 2.0
CLIENT_TAG = "tradlysis"


class OrderExecutor:
    def __init__(
        self,
        settings: Settings,
        client: OandaClient,
        db: Database,
        notifier: Notifier,
        instrument: InstrumentInfo,
    ) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.notifier = notifier
        self.instrument = instrument
        self._lock = asyncio.Lock()  # one submission at a time

    # ------------------------------------------------------------------ entry orders

    async def execute_trade(self, risk_check_id: int, request_id: int, risk: RiskResult) -> Order:
        """Submit an approved entry. Returns the final order row."""
        if not risk.approved or risk.units is None or risk.direction is None:
            raise ValueError("execute_trade requires an approved risk result")
        assert risk.entry is not None and risk.stop_loss is not None and risk.take_profit is not None

        async with self._lock:
            order = await self._create_order_row(risk_check_id, request_id, risk)
            async with self.db.session() as s:
                active, reason = await is_kill_switch_active(s)
            if active:
                await self._update(order.id, status=OrderStatus.FAILED, reject_reason=f"KILL_SWITCH: {reason}")
                await self.notifier.warning(COMPONENT, "ORDER_BLOCKED", f"Kill switch active: {reason}")
                return await self._get(order.id)
            return await self._submit(order.id)

    async def _create_order_row(self, risk_check_id: int, request_id: int, risk: RiskResult) -> Order:
        inst = self.instrument
        long = risk.direction == Direction.BUY
        slip = self.settings.max_slippage_pips * inst.pip_size
        price_bound = round(risk.entry + slip if long else risk.entry - slip, inst.display_precision)
        client_order_id = f"tlys-{request_id}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "type": "MARKET",
            "instrument": inst.name,
            "units": str(risk.units),
            "timeInForce": "FOK",
            "positionFill": "OPEN_ONLY",
            "priceBound": inst.format_price(price_bound),
            "stopLossOnFill": {"price": inst.format_price(risk.stop_loss), "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": inst.format_price(risk.take_profit), "timeInForce": "GTC"},
            "clientExtensions": {
                "id": client_order_id,
                "tag": CLIENT_TAG,
                "comment": self.settings.experiment_name[:100],
            },
            "tradeClientExtensions": {"id": client_order_id, "tag": CLIENT_TAG},
        }
        async with self.db.session() as s:
            order = Order(
                client_order_id=client_order_id,
                risk_check_id=risk_check_id,
                purpose=OrderPurpose.ENTRY,
                instrument=inst.name,
                direction=risk.direction,
                units=risk.units,
                order_type="MARKET",
                requested_price=risk.entry,
                price_bound=price_bound,
                stop_loss=risk.stop_loss,
                take_profit=risk.take_profit,
                status=OrderStatus.PENDING_SUBMIT,
                request_payload=payload,
                attempts=0,
            )
            s.add(order)
            await s.flush()
            await s.refresh(order)
        return order

    async def _submit(self, order_id: int) -> Order:
        order = await self._get(order_id)
        while order.attempts < MAX_SUBMIT_ATTEMPTS:
            await self._update(order.id, attempts=order.attempts + 1, submitted_at=utcnow())
            try:
                status, body = await self.client.create_order(order.request_payload)
            except OandaTransportError as exc:
                await self._update(order.id, status=OrderStatus.UNKNOWN, reject_reason=str(exc)[:500])
                await self.notifier.warning(
                    COMPONENT, "ORDER_OUTCOME_UNKNOWN", f"{order.client_order_id}: {exc}; resolving via lookup"
                )
                await asyncio.sleep(LOOKUP_DELAY_SECONDS)
                resolved = await self.resolve_order(order.id)
                if resolved is None:
                    # Broker confirms the order does not exist: safe to resubmit with the same client ID.
                    order = await self._get(order.id)
                    continue
                return resolved
            order = await self._apply_create_response(order.id, status, body)
            if order.status == OrderStatus.UNKNOWN:
                await asyncio.sleep(LOOKUP_DELAY_SECONDS)
                resolved = await self.resolve_order(order.id)
                if resolved is None:
                    order = await self._get(order.id)
                    continue
                return resolved
            return order

        order = await self._get(order.id)
        if order.status in (OrderStatus.UNKNOWN, OrderStatus.PENDING_SUBMIT):
            await self._update(order.id, status=OrderStatus.FAILED, reject_reason="max submit attempts reached")
            await self.notifier.error(
                COMPONENT, "ORDER_FAILED", f"{order.client_order_id}: gave up after {order.attempts} attempts"
            )
        return await self._get(order.id)

    async def _apply_create_response(self, order_id: int, status: int, body: dict[str, Any]) -> Order:
        fill = body.get("orderFillTransaction")
        cancel = body.get("orderCancelTransaction")
        reject = body.get("orderRejectTransaction")
        create = body.get("orderCreateTransaction")
        common: dict[str, Any] = {"response_payload": body, "http_status": status}
        if create:
            common["broker_order_id"] = create.get("id")

        if status == 201 and fill:
            opened = fill.get("tradeOpened") or {}
            await self._update(
                order_id,
                status=OrderStatus.FILLED,
                fill_transaction_id=fill.get("id"),
                broker_trade_id=opened.get("tradeID"),
                fill_price=_f(opened.get("price") or fill.get("price")),
                filled_units=_i(opened.get("units") or fill.get("units")),
                filled_at=parse_time(fill["time"]) if fill.get("time") else utcnow(),
                **common,
            )
            order = await self._get(order_id)
            await self._record_trade_from_fill(order, fill)
            await self.notifier.info(
                COMPONENT,
                "ORDER_FILLED",
                f"{order.direction} {abs(order.filled_units or order.units)} {order.instrument} @ "
                f"{order.fill_price} (SL {order.stop_loss}, TP {order.take_profit})",
                alert=True,
                dedup_key=f"fill_{order.client_order_id}",
            )
            return order
        if status == 201 and cancel:
            reason = cancel.get("reason", "CANCELLED")
            await self._update(order_id, status=OrderStatus.CANCELLED, reject_reason=reason, **common)
            await self.notifier.warning(COMPONENT, "ORDER_CANCELLED", f"Broker cancelled order: {reason}")
            return await self._get(order_id)
        if status == 201:
            await self._update(order_id, status=OrderStatus.SUBMITTED, **common)
            return await self._get(order_id)
        if reject or status in (400, 404):
            reason = (reject or {}).get("rejectReason") or body.get("errorCode") or body.get("errorMessage") or f"HTTP {status}"
            await self._update(order_id, status=OrderStatus.REJECTED, reject_reason=str(reason), **common)
            await self.notifier.error(COMPONENT, "ORDER_REJECTED", f"Broker rejected order: {reason}", details=body)
            return await self._get(order_id)
        # 401/403/5xx or anything unexpected: outcome unknown until looked up.
        await self._update(order_id, status=OrderStatus.UNKNOWN, reject_reason=f"HTTP {status}", **common)
        await self.notifier.error(COMPONENT, "ORDER_HTTP_ERROR", f"Order request returned HTTP {status}", details=body)
        return await self._get(order_id)

    async def resolve_order(self, order_id: int) -> Order | None:
        """Look an order up at the broker by client ID and update the row.

        Returns the updated order, or None if the broker confirms it does not exist.
        Raises nothing on transport errors: the order simply stays UNKNOWN.
        """
        order = await self._get(order_id)
        try:
            broker_order = await self.client.get_order(f"@{order.client_order_id}")
        except Exception as exc:
            log.warning("Order lookup failed for %s: %s", order.client_order_id, exc)
            return order
        if broker_order is None:
            return None
        state = broker_order.get("state")
        if state == "FILLED":
            trade_id = broker_order.get("tradeOpenedID")
            trade = await self.client.get_trade(trade_id) if trade_id else None
            await self._update(
                order.id,
                status=OrderStatus.FILLED,
                broker_order_id=broker_order.get("id"),
                fill_transaction_id=broker_order.get("fillingTransactionID"),
                broker_trade_id=trade_id,
                fill_price=_f(trade.get("price")) if trade else None,
                filled_units=_i(trade.get("initialUnits")) if trade else None,
                filled_at=parse_time(broker_order["filledTime"]) if broker_order.get("filledTime") else utcnow(),
                response_payload={"lookup": broker_order, "trade": trade},
            )
            order = await self._get(order.id)
            if trade:
                await self._record_trade(order, trade)
            await self.notifier.info(
                COMPONENT, "ORDER_FILLED", f"Resolved {order.client_order_id} as FILLED", alert=True
            )
            return order
        if state == "CANCELLED":
            await self._update(
                order.id,
                status=OrderStatus.CANCELLED,
                broker_order_id=broker_order.get("id"),
                reject_reason="CANCELLED",
                response_payload={"lookup": broker_order},
            )
            return await self._get(order.id)
        await self._update(
            order.id, status=OrderStatus.SUBMITTED, broker_order_id=broker_order.get("id"),
            response_payload={"lookup": broker_order},
        )
        return await self._get(order.id)

    async def resolve_unresolved_orders(self, min_age_seconds: float = 0.0) -> None:
        """Called at startup and by reconciliation for orders left in an in-between state.

        Holds the submission lock so it never races an in-flight submission.
        """
        async with self._lock:
            await self._resolve_unresolved(min_age_seconds)

    async def _resolve_unresolved(self, min_age_seconds: float) -> None:
        cutoff = utcnow() - timedelta(seconds=min_age_seconds)
        async with self.db.session() as s:
            rows = (
                await s.scalars(
                    select(Order).where(
                        Order.status.in_([str(x) for x in OPEN_ORDER_STATUSES]),
                        Order.purpose == str(OrderPurpose.ENTRY),
                        Order.created_at <= cutoff,
                    )
                )
            ).all()
        for order in rows:
            resolved = await self.resolve_order(order.id)
            if resolved is None:
                # The broker never received it. Do not resubmit stale entries; mark failed.
                await self._update(order.id, status=OrderStatus.FAILED, reject_reason="not found at broker")
                await self.notifier.warning(
                    COMPONENT, "ORDER_NOT_FOUND", f"{order.client_order_id} never reached the broker; marked FAILED"
                )

    # ------------------------------------------------------------------ flatten

    async def flatten_all(self, reason: str) -> int:
        """Close every open trade on the account (used by the dashboard 'flatten' control)."""
        trades = await self.client.get_open_trades()
        closed = 0
        for t in trades:
            client_order_id = f"tlys-close-{t['id']}-{uuid.uuid4().hex[:6]}"
            async with self.db.session() as s:
                row = Order(
                    client_order_id=client_order_id,
                    purpose=OrderPurpose.CLOSE,
                    instrument=t["instrument"],
                    direction=Direction.SELL if int(float(t["currentUnits"])) > 0 else Direction.BUY,
                    units=-int(float(t["currentUnits"])),
                    order_type="TRADE_CLOSE",
                    status=OrderStatus.PENDING_SUBMIT,
                    request_payload={"trade_id": t["id"], "units": "ALL", "reason": reason},
                    attempts=1,
                    submitted_at=utcnow(),
                )
                s.add(row)
                await s.flush()
                row_id = row.id
            try:
                status, body = await self.client.close_trade(t["id"])
            except OandaTransportError as exc:
                await self._update(row_id, status=OrderStatus.UNKNOWN, reject_reason=str(exc)[:500])
                continue
            fill = body.get("orderFillTransaction")
            if status == 200 and fill:
                await self._update(
                    row_id, status=OrderStatus.FILLED, fill_price=_f(fill.get("price")),
                    fill_transaction_id=fill.get("id"), response_payload=body, http_status=status,
                    filled_at=utcnow(),
                )
                closed += 1
            else:
                await self._update(
                    row_id, status=OrderStatus.REJECTED, response_payload=body, http_status=status,
                    reject_reason=str(body.get("errorMessage") or body.get("errorCode") or status),
                )
        await self.notifier.warning(COMPONENT, "FLATTENED", f"Closed {closed}/{len(trades)} trades: {reason}", alert=True)
        return closed

    # ------------------------------------------------------------------ helpers

    async def _record_trade_from_fill(self, order: Order, fill: dict[str, Any]) -> None:
        opened = fill.get("tradeOpened") or {}
        trade_id = opened.get("tradeID")
        if not trade_id:
            return
        units = _i(opened.get("units")) or order.units
        price = _f(opened.get("price") or fill.get("price")) or 0.0
        await self._upsert_trade(
            order,
            broker_trade_id=trade_id,
            units=units,
            price=price,
            open_time=parse_time(fill["time"]) if fill.get("time") else utcnow(),
            raw={"fill": fill},
        )

    async def _record_trade(self, order: Order, trade: dict[str, Any]) -> None:
        await self._upsert_trade(
            order,
            broker_trade_id=trade["id"],
            units=_i(trade.get("initialUnits")) or order.units,
            price=_f(trade.get("price")) or 0.0,
            open_time=parse_time(trade["openTime"]) if trade.get("openTime") else utcnow(),
            raw={"trade": trade},
        )

    async def _upsert_trade(
        self, order: Order, *, broker_trade_id: str, units: int, price: float, open_time: Any, raw: dict[str, Any]
    ) -> None:
        async with self.db.session() as s:
            existing = await s.scalar(select(Trade).where(Trade.broker_trade_id == broker_trade_id))
            if existing is not None:
                existing.order_id = order.id
                existing.unexpected = False
                return
            s.add(
                Trade(
                    broker_trade_id=broker_trade_id,
                    order_id=order.id,
                    client_trade_id=order.client_order_id,
                    experiment=self.settings.experiment_name,
                    instrument=order.instrument,
                    direction=order.direction,
                    initial_units=units,
                    current_units=units,
                    open_price=price,
                    open_time=open_time,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    initial_risk_price=abs(price - order.stop_loss) if order.stop_loss else None,
                    state=TradeState.OPEN,
                    unexpected=False,
                    raw=raw,
                )
            )

    async def _get(self, order_id: int) -> Order:
        async with self.db.session() as s:
            order = await s.get(Order, order_id)
            assert order is not None
            return order

    async def _update(self, order_id: int, **values: Any) -> None:
        async with self.db.session() as s:
            order = await s.get(Order, order_id)
            assert order is not None
            for k, v in values.items():
                setattr(order, k, str(v) if isinstance(v, OrderStatus) else v)


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    try:
        return None if v is None else int(Decimal(str(v)))
    except (TypeError, ValueError, ArithmeticError):
        return None
