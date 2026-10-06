"""Order Executor: the only component allowed to submit orders to the broker.

Guarantees:
  * An order row (status PENDING_SUBMIT) is committed *before* the request is sent, with a
    unique ``client_order_id``. The ``risk_check_id`` column is unique, so one approved risk
    check can create at most one order.
  * Open-only: right before submitting, the broker's open positions are re-read and the entry is
    refused if one already exists on the instrument (on a non-hedging account an opposite order
    would otherwise close or reduce it). All experiments share one account: with hedging mode
    off, any position on the instrument blocks every experiment trading it; with hedging mode on
    (positions are kept apart), only the experiment's own positions and positions no experiment
    owns block it. The risk engine checks this too; this closes the race.
  * Slippage bound: Capital.com market orders carry no price bound, so a fill worse than
    ``price_bound`` (entry +/- MAX_SLIPPAGE_PIPS) is closed immediately, which has the same
    effect as a bounded order being rejected (plus the spread).
  * A timeout or connection error never counts as "not filled" or "filled": the order becomes
    UNKNOWN and is resolved from the broker (the deal confirmation when the deal reference is
    known, otherwise open positions and activity history). Capital.com has no client order IDs,
    so the broker not knowing an order is no proof it never arrived: entries are never resubmitted.
  * The kill switch is re-read immediately before submission.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.alerts.notifier import Notifier
from app.broker.capital import CapitalClient, CapitalTransportError
from app.broker.types import BrokerPosition, InstrumentInfo
from app.config.settings import Settings
from app.db.control import is_kill_switch_active
from app.db.enums import OPEN_ORDER_STATUSES, Direction, OrderPurpose, OrderStatus, TradeState
from app.db.models import DecisionRequest, Order, RiskCheck, Trade
from app.db.session import Database
from app.market_data.timeutil import parse_time, utcnow
from app.risk.engine import RiskResult

log = logging.getLogger(__name__)
COMPONENT = "execution"
LOOKUP_DELAY_SECONDS = 2.0
CONFIRM_ATTEMPTS = 6
CONFIRM_DELAY_SECONDS = 0.5
# How far around the submission time a broker deal may be matched to an order without a deal reference.
MATCH_BEFORE = timedelta(seconds=60)
MATCH_AFTER = timedelta(minutes=10)


class OrderExecutor:
    def __init__(
        self,
        settings: Settings,
        client: CapitalClient,
        db: Database,
        notifier: Notifier,
        instruments: InstrumentInfo | dict[str, InstrumentInfo],
    ) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.notifier = notifier
        if isinstance(instruments, InstrumentInfo):
            instruments = {instruments.name: instruments}
        self.instruments = instruments
        # Per-experiment notifiers (set by the engine) so events name the experiment they belong to.
        self.notifiers: dict[str, Notifier] = {}
        self._order_experiments: dict[int, str | None] = {}
        self._lock = asyncio.Lock()  # one submission at a time, across all experiments

    # ------------------------------------------------------------------ entry orders

    def _inst(self, name: str) -> InstrumentInfo:
        inst = self.instruments.get(name)
        if inst is None:
            raise KeyError(f"no instrument details for {name}")
        return inst

    def _for(self, experiment: str | None) -> Notifier:
        return self.notifiers.get(experiment or "", self.notifier)

    async def _notifier(self, order_id: int) -> Notifier:
        return self._for(await self.order_experiment(order_id))

    async def order_experiment(self, order_id: int) -> str | None:
        """The experiment an entry order belongs to (through its risk check and decision request)."""
        if order_id not in self._order_experiments:
            async with self.db.session() as s:
                self._order_experiments[order_id] = await s.scalar(
                    select(DecisionRequest.experiment)
                    .join(RiskCheck, RiskCheck.request_id == DecisionRequest.id)
                    .join(Order, Order.risk_check_id == RiskCheck.id)
                    .where(Order.id == order_id)
                )
        return self._order_experiments[order_id]

    async def execute_trade(
        self, risk_check_id: int, request_id: int, risk: RiskResult, experiment: Settings | None = None
    ) -> Order:
        """Submit an approved entry for an experiment (its settings). Returns the final order row."""
        if not risk.approved or risk.units is None or risk.direction is None:
            raise ValueError("execute_trade requires an approved risk result")
        assert risk.entry is not None and risk.stop_loss is not None and risk.take_profit is not None
        exp = experiment or self.settings
        slug = exp.experiment_name
        notifier = self._for(slug)

        async with self._lock:
            order = await self._create_order_row(risk_check_id, request_id, risk, exp)
            self._order_experiments[order.id] = slug
            async with self.db.session() as s:
                active, reason = await is_kill_switch_active(s, slug)
            if active:
                await self._update(order.id, status=OrderStatus.FAILED, reject_reason=f"KILL_SWITCH: {reason}")
                await notifier.warning(COMPONENT, "ORDER_BLOCKED", f"Kill switch active: {reason}", alert=True)
                return await self._get(order.id)
            blocked = await self._open_only_violation(exp.instrument, slug)
            if blocked:
                await self._update(order.id, status=OrderStatus.FAILED, reject_reason=blocked)
                await notifier.warning(COMPONENT, "ORDER_BLOCKED", blocked, alert=True)
                return await self._get(order.id)
            return await self._submit(order.id)

    async def _open_only_violation(self, instrument: str, experiment: str) -> str | None:
        try:
            positions = await self.client.get_positions()
            on_instrument = [p for p in positions if p.instrument == instrument]
            if not on_instrument:
                return None
            hedging = bool((await self.client.get_preferences()).get("hedgingMode"))
        except Exception as exc:
            return f"OPEN_ONLY: could not verify open positions: {exc}"[:500]
        if hedging:
            owners = await self.position_owners([p.deal_id for p in on_instrument])
            # Positions no experiment owns (unknown or not yet recorded) block too.
            on_instrument = [p for p in on_instrument if owners.get(p.deal_id) in (None, experiment)]
            if not on_instrument:
                return None
        ids = ", ".join(p.deal_id for p in on_instrument)
        return f"OPEN_ONLY: a position is already open on {instrument} ({ids})"

    async def position_owners(self, deal_ids: list[str]) -> dict[str, str | None]:
        """Experiment of each broker position we have a trade row for."""
        if not deal_ids:
            return {}
        async with self.db.session() as s:
            rows = (
                await s.execute(select(Trade.broker_trade_id, Trade.experiment).where(Trade.broker_trade_id.in_(deal_ids)))
            ).all()
        return {deal_id: experiment for deal_id, experiment in rows}

    async def _create_order_row(self, risk_check_id: int, request_id: int, risk: RiskResult, exp: Settings) -> Order:
        inst = self._inst(exp.instrument)
        long = risk.direction == Direction.BUY
        slip = exp.max_slippage_pips * inst.pip_size
        price_bound = round(risk.entry + slip if long else risk.entry - slip, inst.display_precision)
        client_order_id = f"tlys-{request_id}-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {
            "epic": inst.epic or self.client.epic_for(inst.name),
            "direction": str(risk.direction),
            "size": abs(risk.units),
            "guaranteedStop": False,
            "trailingStop": False,
            "stopLevel": round(risk.stop_loss, inst.display_precision),
            "profitLevel": round(risk.take_profit, inst.display_precision),
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
        await self._update(order.id, attempts=order.attempts + 1, submitted_at=utcnow())
        try:
            status, body = await self.client.open_position(order.request_payload)
        except CapitalTransportError as exc:
            await self._update(order.id, status=OrderStatus.UNKNOWN, reject_reason=str(exc)[:500])
            await (await self._notifier(order.id)).warning(
                COMPONENT, "ORDER_OUTCOME_UNKNOWN", f"{order.client_order_id}: {exc}; resolving from broker state"
            )
            return await self._resolve_after_unknown(order.id)
        return await self._apply_open_response(order.id, status, body)

    async def _resolve_after_unknown(self, order_id: int) -> Order:
        await asyncio.sleep(LOOKUP_DELAY_SECONDS)
        resolved = await self.resolve_order(order_id)
        if resolved is not None:
            return resolved
        order = await self._get(order_id)
        await self._update(
            order_id,
            status=OrderStatus.FAILED,
            reject_reason=f"outcome unknown, no matching deal at broker; not resubmitted ({order.reject_reason})"[:500],
        )
        await (await self._notifier(order_id)).error(
            COMPONENT, "ORDER_FAILED", f"{order.client_order_id}: no matching deal found at the broker; not resubmitted"
        )
        return await self._get(order_id)

    async def _apply_open_response(self, order_id: int, status: int, body: dict[str, Any]) -> Order:
        common: dict[str, Any] = {"response_payload": {"open": body}, "http_status": status}
        reference = body.get("dealReference")
        if status == 200 and reference:
            await self._update(order_id, status=OrderStatus.SUBMITTED, broker_order_id=str(reference), **common)
            return await self._confirm(order_id)
        if 400 <= status < 500:
            reason = body.get("errorCode") or body.get("message") or f"HTTP {status}"
            await self._update(order_id, status=OrderStatus.REJECTED, reject_reason=str(reason)[:500], **common)
            await (await self._notifier(order_id)).error(
                COMPONENT, "ORDER_REJECTED", f"Broker rejected order: {reason}", details=body
            )
            return await self._get(order_id)
        # 5xx or anything unexpected: outcome unknown until resolved from broker state.
        await self._update(order_id, status=OrderStatus.UNKNOWN, reject_reason=f"HTTP {status}", **common)
        await (await self._notifier(order_id)).error(
            COMPONENT, "ORDER_HTTP_ERROR", f"Order request returned HTTP {status}", details=body
        )
        return await self._resolve_after_unknown(order_id)

    async def _confirm(self, order_id: int) -> Order:
        """Poll the deal confirmation for a submitted order."""
        order = await self._get(order_id)
        assert order.broker_order_id
        for attempt in range(CONFIRM_ATTEMPTS):
            try:
                conf = await self.client.get_confirmation(order.broker_order_id)
            except Exception as exc:
                log.warning("confirmation lookup failed for %s: %s", order.broker_order_id, exc)
                conf = None
            if conf and conf.get("dealStatus") in ("ACCEPTED", "REJECTED"):
                return await self._apply_confirmation(order_id, conf)
            if attempt < CONFIRM_ATTEMPTS - 1:
                await asyncio.sleep(CONFIRM_DELAY_SECONDS)
        # Accepted for processing but not confirmed yet; reconciliation resolves it by deal reference.
        await (await self._notifier(order_id)).warning(
            COMPONENT, "ORDER_UNCONFIRMED", f"{order.client_order_id}: no deal confirmation yet ({order.broker_order_id})"
        )
        return await self._get(order_id)

    async def _apply_confirmation(self, order_id: int, conf: dict[str, Any]) -> Order:
        order = await self._get(order_id)
        payload = {**(order.response_payload or {}), "confirmation": conf}
        if conf.get("dealStatus") == "REJECTED":
            reason = str(conf.get("reason") or conf.get("status") or "REJECTED")
            await self._update(order_id, status=OrderStatus.REJECTED, reject_reason=reason[:500], response_payload=payload)
            await (await self._notifier(order_id)).error(
                COMPONENT, "ORDER_REJECTED", f"Broker rejected order: {reason}", details=conf
            )
            return await self._get(order_id)
        deal_id = _opened_deal_id(conf)
        price = _f(conf.get("level"))
        position = None
        if price is None and deal_id:
            position = await self._safe_get_position(deal_id)
            price = position.open_price if position else None
        size = _i(conf.get("size")) or abs(order.units)
        return await self._mark_filled(
            order,
            deal_id=deal_id,
            price=price,
            units=size if order.direction == Direction.BUY else -size,
            filled_at=position.open_time if position else utcnow(),
            fill_ref=str(conf.get("dealId")) if conf.get("dealId") else None,
            payload=payload,
            raw={"confirmation": conf},
        )

    async def _mark_filled(
        self,
        order: Order,
        *,
        deal_id: str | None,
        price: float | None,
        units: int,
        filled_at: datetime,
        fill_ref: str | None,
        payload: dict[str, Any],
        raw: dict[str, Any],
    ) -> Order:
        await self._update(
            order.id,
            status=OrderStatus.FILLED,
            broker_trade_id=deal_id,
            fill_transaction_id=fill_ref,
            fill_price=price,
            filled_units=units,
            filled_at=filled_at,
            response_payload=payload,
        )
        order = await self._get(order.id)
        if deal_id:
            await self._upsert_trade(
                order, broker_trade_id=deal_id, units=units, price=price or 0.0, open_time=filled_at, raw=raw
            )
        await (await self._notifier(order.id)).info(
            COMPONENT,
            "ORDER_FILLED",
            f"{order.direction} {abs(order.filled_units or order.units)} {order.instrument} @ "
            f"{order.fill_price} (SL {order.stop_loss}, TP {order.take_profit})",
            alert=True,
            dedup_key=f"fill_{order.client_order_id}",
        )
        await self._enforce_slippage_bound(order)
        return await self._get(order.id)

    async def _enforce_slippage_bound(self, order: Order) -> None:
        if order.fill_price is None or order.price_bound is None or not order.broker_trade_id:
            return
        pip = self._inst(order.instrument).pip_size
        eps = pip * 1e-3
        long = order.direction == Direction.BUY
        worse = order.fill_price > order.price_bound + eps if long else order.fill_price < order.price_bound - eps
        if not worse:
            return
        slip = abs(order.fill_price - (order.requested_price or order.fill_price)) / pip
        allowed = abs(order.price_bound - (order.requested_price or order.price_bound)) / pip
        reason = (
            f"slippage {slip:.1f} pips exceeds MAX_SLIPPAGE_PIPS={allowed:g} "
            f"(fill {order.fill_price}, bound {order.price_bound})"
        )
        await (await self._notifier(order.id)).error(COMPONENT, "SLIPPAGE_EXCEEDED", f"{order.client_order_id}: {reason}; closing", alert=True)
        await self._close_position(order.broker_trade_id, order.instrument, order.filled_units or order.units, reason)

    # ------------------------------------------------------------------ resolution

    async def resolve_order(self, order_id: int) -> Order | None:
        """Resolve an entry order from broker state and update the row.

        Returns the updated order, or None if the broker has no trace of it.
        Raises nothing on lookup errors: the order simply stays as it is.
        """
        order = await self._get(order_id)
        try:
            if order.broker_order_id:
                conf = await self.client.get_confirmation(order.broker_order_id)
                if conf and conf.get("dealStatus") in ("ACCEPTED", "REJECTED"):
                    return await self._apply_confirmation(order.id, conf)
            match = await self._find_broker_deal(order)
        except Exception as exc:
            log.warning("Order lookup failed for %s: %s", order.client_order_id, exc)
            return order
        if match is None:
            return None
        kind, data = match
        if kind == "position":
            p: BrokerPosition = data
            await (await self._notifier(order.id)).info(
                COMPONENT, "ORDER_RESOLVED", f"Resolved {order.client_order_id} as FILLED ({p.deal_id})"
            )
            return await self._mark_filled(
                order,
                deal_id=p.deal_id,
                price=p.open_price,
                units=p.units,
                filled_at=p.open_time,
                fill_ref=None,
                payload={**(order.response_payload or {}), "lookup": {"position": p.raw}},
                raw={"position": p.raw},
            )
        activity: dict[str, Any] = data
        payload = {**(order.response_payload or {}), "lookup": {"activity": activity}}
        if kind == "rejected":
            await self._update(order.id, status=OrderStatus.REJECTED, reject_reason="REJECTED", response_payload=payload)
            return await self._get(order.id)
        details = activity.get("details") or {}
        opened_at = parse_time(str(activity["dateUTC"])) if activity.get("dateUTC") else utcnow()
        size = _i(details.get("size")) or abs(order.units)
        await (await self._notifier(order.id)).info(
            COMPONENT, "ORDER_RESOLVED", f"Resolved {order.client_order_id} as FILLED from activity"
        )
        return await self._mark_filled(
            order,
            deal_id=_activity_opened_deal(activity),
            price=_f(details.get("level")),
            units=size if order.direction == Direction.BUY else -size,
            filled_at=opened_at,
            fill_ref=None,
            payload=payload,
            raw={"activity": activity},
        )

    async def _find_broker_deal(self, order: Order) -> tuple[str, Any] | None:
        """Find the deal an order without a confirmation produced: an open position, else activity history."""
        submitted = order.submitted_at or order.created_at
        known = await self._known_trade_ids()
        for p in sorted(await self.client.get_positions(), key=lambda x: x.open_time):
            if p.deal_id not in known and self._position_matches(order, p, submitted):
                return "position", p
        activities = await self.client.get_activity(submitted - MATCH_BEFORE, instrument=order.instrument)
        for a in sorted(activities, key=lambda x: str(x.get("dateUTC", ""))):
            if a.get("type") != "POSITION":
                continue
            details = a.get("details") or {}
            if details.get("direction") != order.direction or _i(details.get("size")) != abs(order.units):
                continue
            if a.get("status") == "REJECTED":
                return "rejected", a
            actions = details.get("actions") or []
            deal_id = _activity_opened_deal(a)
            if any(x.get("actionType") == "POSITION_OPENED" for x in actions) and deal_id not in known:
                return "opened", a
        return None

    def _position_matches(self, order: Order, p: BrokerPosition, submitted: datetime) -> bool:
        inst = self.instruments.get(order.instrument)
        if inst is None:
            return False
        tol = inst.pip_size / 2
        return (
            p.instrument == order.instrument
            and p.direction == order.direction
            and abs(p.units) == abs(order.units)
            and submitted - MATCH_BEFORE <= p.open_time <= submitted + MATCH_AFTER
            and _near(p.stop_loss, order.stop_loss, tol)
            and _near(p.take_profit, order.take_profit, tol)
        )

    async def adopt_position(self, p: BrokerPosition) -> Order | None:
        """Link a broker position with no local trade to the entry order that created it, if any.

        Used by reconciliation: an order whose outcome was unknown (or that was marked FAILED
        because the broker showed no trace of it yet) is matched on instrument, direction, size,
        stop-loss, take-profit and time.
        """
        async with self.db.session() as s:
            order = await s.scalar(select(Order).where(Order.broker_trade_id == p.deal_id))
            if order is None:
                rows = (
                    await s.scalars(
                        select(Order)
                        .where(
                            Order.purpose == str(OrderPurpose.ENTRY),
                            Order.status.in_([*[str(x) for x in OPEN_ORDER_STATUSES], str(OrderStatus.FAILED)]),
                            Order.broker_trade_id.is_(None),
                            Order.submitted_at.is_not(None),
                        )
                        .order_by(Order.created_at)
                    )
                ).all()
                order = next((o for o in rows if self._position_matches(o, p, o.submitted_at)), None)
        if order is None:
            return None
        if order.status != OrderStatus.FILLED:
            await self._update(
                order.id,
                status=OrderStatus.FILLED,
                broker_trade_id=p.deal_id,
                fill_price=p.open_price,
                filled_units=p.units,
                filled_at=p.open_time,
                response_payload={**(order.response_payload or {}), "adopted": {"position": p.raw}},
            )
            await (await self._notifier(order.id)).warning(
                COMPONENT, "ORDER_ADOPTED", f"{order.client_order_id} matched to broker position {p.deal_id}; marked FILLED",
                alert=True,
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
                        Order.created_at <= cutoff,
                    )
                )
            ).all()
        for order in rows:
            if order.purpose == OrderPurpose.CLOSE:
                await self._resolve_close(order)
                continue
            resolved = await self.resolve_order(order.id)
            if resolved is None:
                await self._update(order.id, status=OrderStatus.FAILED, reject_reason="not found at broker")
                await (await self._notifier(order.id)).warning(
                    COMPONENT, "ORDER_NOT_FOUND", f"{order.client_order_id} not found at the broker; marked FAILED",
                    alert=True,
                )

    async def _resolve_close(self, order: Order) -> None:
        deal_id = (order.request_payload or {}).get("deal_id")
        if not deal_id:
            return
        try:
            position = await self.client.get_position(str(deal_id))
        except Exception as exc:
            log.warning("close lookup failed for %s: %s", order.client_order_id, exc)
            return
        if position is None:
            await self._update(order.id, status=OrderStatus.FILLED, filled_at=utcnow())
        else:
            await self._update(order.id, status=OrderStatus.FAILED, reject_reason="position still open")

    # ------------------------------------------------------------------ closing

    async def flatten_all(self, reason: str, experiment: str | None = None) -> int:
        """Close open positions (the dashboard's 'close all trades'): one experiment's, or all on the account."""
        positions = await self.client.get_positions()
        if experiment is not None:
            owners = await self.position_owners([p.deal_id for p in positions])
            positions = [p for p in positions if owners.get(p.deal_id) == experiment]
        closed = 0
        for p in positions:
            if await self._close_position(p.deal_id, p.instrument, p.units, reason):
                closed += 1
        await self._for(experiment).warning(
            COMPONENT, "FLATTENED", f"Closed {closed}/{len(positions)} trades: {reason}", alert=True
        )
        return closed

    async def _close_position(self, deal_id: str, instrument: str, units: int, reason: str) -> bool:
        client_order_id = f"tlys-close-{deal_id[-12:]}-{uuid.uuid4().hex[:6]}"
        async with self.db.session() as s:
            row = Order(
                client_order_id=client_order_id,
                purpose=OrderPurpose.CLOSE,
                instrument=instrument,
                direction=Direction.SELL if units > 0 else Direction.BUY,
                units=-units,
                order_type="POSITION_CLOSE",
                status=OrderStatus.PENDING_SUBMIT,
                request_payload={"deal_id": deal_id, "reason": reason[:300]},
                attempts=1,
                submitted_at=utcnow(),
            )
            s.add(row)
            await s.flush()
            row_id = row.id
        try:
            status, body = await self.client.close_position(deal_id)
        except CapitalTransportError as exc:
            await self._update(row_id, status=OrderStatus.UNKNOWN, reject_reason=str(exc)[:500])
            return False
        reference = body.get("dealReference")
        if status != 200 or not reference:
            await self._update(
                row_id, status=OrderStatus.REJECTED, response_payload=body, http_status=status,
                reject_reason=str(body.get("errorCode") or body.get("message") or f"HTTP {status}")[:500],
            )
            return False
        conf = None
        for attempt in range(CONFIRM_ATTEMPTS):
            try:
                conf = await self.client.get_confirmation(str(reference))
            except Exception as exc:
                log.warning("close confirmation lookup failed for %s: %s", reference, exc)
            if conf and conf.get("dealStatus") in ("ACCEPTED", "REJECTED"):
                break
            if attempt < CONFIRM_ATTEMPTS - 1:
                await asyncio.sleep(CONFIRM_DELAY_SECONDS)
        payload = {"close": body, "confirmation": conf}
        if conf and conf.get("dealStatus") == "REJECTED":
            await self._update(
                row_id, status=OrderStatus.REJECTED, response_payload=payload, http_status=status,
                broker_order_id=str(reference), reject_reason=str(conf.get("reason") or "REJECTED")[:500],
            )
            return False
        # Accepted (or confirmation not available yet: the broker accepted the close request).
        await self._update(
            row_id, status=OrderStatus.FILLED, response_payload=payload, http_status=status,
            broker_order_id=str(reference), fill_price=_f((conf or {}).get("level")), filled_at=utcnow(),
        )
        return True

    # ------------------------------------------------------------------ helpers

    async def _safe_get_position(self, deal_id: str) -> BrokerPosition | None:
        try:
            return await self.client.get_position(deal_id)
        except Exception as exc:
            log.warning("position lookup failed for %s: %s", deal_id, exc)
            return None

    async def _known_trade_ids(self) -> set[str]:
        async with self.db.session() as s:
            return set((await s.scalars(select(Trade.broker_trade_id))).all())

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
                    experiment=await self.order_experiment(order.id),
                    instrument=order.instrument,
                    direction=order.direction,
                    initial_units=units,
                    current_units=units,
                    open_price=price,
                    open_time=open_time,
                    stop_loss=order.stop_loss,
                    take_profit=order.take_profit,
                    initial_risk_price=abs(price - order.stop_loss) if order.stop_loss and price else None,
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


def _opened_deal_id(conf: dict[str, Any]) -> str | None:
    """The position deal ID from a confirmation (``affectedDeals`` entry with status OPENED)."""
    for deal in conf.get("affectedDeals") or []:
        if deal.get("status") == "OPENED" and deal.get("dealId"):
            return str(deal["dealId"])
    return str(conf["dealId"]) if conf.get("dealId") else None


def _activity_opened_deal(activity: dict[str, Any]) -> str | None:
    for action in (activity.get("details") or {}).get("actions") or []:
        if action.get("actionType") == "POSITION_OPENED" and action.get("affectedDealId"):
            return str(action["affectedDealId"])
    return str(activity["dealId"]) if activity.get("dealId") else None


def _near(a: float | None, b: float | None, tol: float) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= tol


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
