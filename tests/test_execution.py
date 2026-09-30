"""Executor + reconciliation against the fake broker and a real (test) PostgreSQL database."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.alerts.notifier import Notifier
from app.broker.oanda import InstrumentInfo, OandaClient
from app.db.control import get_control, set_control
from app.db.enums import ControlKey
from app.db.models import BrokerTransaction, Decision, DecisionRequest, Order, RiskCheck, SystemEvent, Trade
from app.execution.executor import OrderExecutor
from app.reconciliation.reconciler import Reconciler, r_multiple
from app.risk.engine import CheckResult, RiskResult
from tests.conftest import make_settings
from tests.fake_broker import ACCOUNT_ID, FakeBroker
from tests.helpers import T0

INST = InstrumentInfo("EUR_USD", -4, 5, 0, 1.0, 0.0333)


@pytest.fixture(autouse=True)
def fast_lookup(monkeypatch):
    monkeypatch.setattr("app.execution.executor.LOOKUP_DELAY_SECONDS", 0)


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def parts(db, broker):
    settings = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    client = OandaClient("https://api.test", "https://stream.test", "tok", ACCOUNT_ID,
                         transport=broker.transport(), max_get_retries=0)
    notifier = Notifier(db, None)
    executor = OrderExecutor(settings, client, db, notifier, INST)
    reconciler = Reconciler(settings, client, db, notifier, executor)
    return settings, executor, reconciler


def approved(direction="BUY") -> RiskResult:
    long = direction == "BUY"
    return RiskResult(
        approved=True, checks=[CheckResult("ALL", True)], direction=direction,
        units=10000 if long else -10000, risk_amount=Decimal("250"), risk_pct=0.25,
        entry=1.1026 if long else 1.1025, stop_loss=1.1006 if long else 1.1045,
        take_profit=1.1076 if long else 1.0975, risk_reward=2.5, nav=Decimal("100000"),
    )


async def chain(db, candle_offset=0) -> tuple[int, int]:
    """Insert request -> decision -> risk_check rows; returns (request_id, risk_check_id)."""
    from datetime import timedelta

    async with db.session() as s:
        req = DecisionRequest(experiment="test", instrument="EUR_USD", candle_time=T0 + timedelta(minutes=15 * candle_offset),
                              snapshot={}, strategy_result={})
        s.add(req)
        await s.flush()
        dec = Decision(request_id=req.id, source="LLM", decision="BUY", reason_codes=[])
        s.add(dec)
        await s.flush()
        rc = RiskCheck(request_id=req.id, decision_id=dec.id, approved=True, checks=[], rejection_reasons=[])
        s.add(rc)
        await s.flush()
        return req.id, rc.id


async def orders(db) -> list[Order]:
    async with db.session() as s:
        return list((await s.scalars(select(Order).order_by(Order.id))).all())


async def trades(db) -> list[Trade]:
    async with db.session() as s:
        return list((await s.scalars(select(Trade).order_by(Trade.id))).all())


async def test_fill_records_order_and_trade(db, broker, parts):
    _, executor, _ = parts
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FILLED"
    assert order.fill_price == pytest.approx(1.1026)
    posted = broker.order_posts[0]
    assert posted["positionFill"] == "OPEN_ONLY" and posted["timeInForce"] == "FOK"
    assert posted["stopLossOnFill"]["price"] == "1.10060" and posted["takeProfitOnFill"]["price"] == "1.10760"
    assert posted["priceBound"] == "1.10270"  # entry + 1 pip max slippage
    assert posted["clientExtensions"]["id"] == order.client_order_id
    [t] = await trades(db)
    assert t.state == "OPEN" and t.order_id == order.id and not t.unexpected
    assert t.initial_risk_price == pytest.approx(0.0020)


async def test_one_order_per_risk_check(db, broker, parts):
    _, executor, _ = parts
    req_id, rc_id = await chain(db)
    await executor.execute_trade(rc_id, req_id, approved())
    with pytest.raises(IntegrityError):  # unique(risk_check_id)
        await executor.execute_trade(rc_id, req_id, approved())
    assert len(broker.order_posts) == 1


async def test_timeout_after_fill_is_resolved_not_resubmitted(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "timeout_after"
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FILLED"
    assert len(broker.order_posts) == 1, "must not resubmit an order the broker already filled"
    assert len(broker.open_trades) == 1
    assert len(await trades(db)) == 1


async def test_timeout_before_receipt_is_resubmitted_with_same_client_id(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "timeout_before"
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FILLED" and order.attempts == 2
    assert [p["clientExtensions"]["id"] for p in broker.order_posts] == [order.client_order_id] * 2
    assert len(broker.open_trades) == 1


async def test_reject_and_cancel(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "reject"
    req_id, rc_id = await chain(db, 0)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "REJECTED" and o.reject_reason == "INSUFFICIENT_MARGIN"
    broker.order_mode = "cancel"
    req_id, rc_id = await chain(db, 1)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "CANCELLED" and o.reject_reason == "BOUNDS_VIOLATION"
    assert not broker.open_trades and not await trades(db)


async def test_http_500_then_not_found_is_retried_once_then_fails(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "http500"
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    # 500 -> lookup says it doesn't exist -> resubmit -> 500 again -> give up
    assert o.status == "FAILED" and o.attempts == 2


async def test_kill_switch_rechecked_before_submission(db, broker, parts):
    _, executor, _ = parts
    async with db.session() as s:
        await set_control(s, ControlKey.KILL_SWITCH, {"active": True, "reason": "test"}, "test")
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "FAILED" and "KILL_SWITCH" in o.reject_reason
    assert not broker.order_posts


async def test_reconciler_tracks_close_and_r(db, broker, parts):
    _, executor, reconciler = parts
    assert await reconciler.reconcile_once()  # first run: sets transaction cursor, snapshots
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    [bt_id] = broker.open_trades
    broker.close_trade_at(bt_id, 1.1006, reason="STOP_LOSS_ORDER")
    assert await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.state == "CLOSED" and t.close_reason == "STOP_LOSS"
    assert t.r_multiple == pytest.approx(-1.0)
    assert t.realized_pl == Decimal("-20.000000")
    async with db.session() as s:
        n = await s.scalar(select(func.count()).select_from(BrokerTransaction))
    assert n >= 1
    assert o.status == "FILLED"


async def test_unexpected_position_flagged(db, broker, parts):
    _, _, reconciler = parts
    broker.add_external_trade(units=5000, price=1.1)
    await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.unexpected and t.order_id is None
    async with db.session() as s:
        ev = await s.scalar(select(SystemEvent).where(SystemEvent.event_type == "UNEXPECTED_POSITION"))
    assert ev is not None and ev.level == "CRITICAL"


async def test_breakers_and_day_start(db, broker, parts):
    settings, _, reconciler = parts
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ControlKey.PEAK_NAV))["value"] == str(reconciler.account.nav)
        assert (await get_control(s, ControlKey.DAY_START_NAV))["value"] == str(reconciler.account.nav)
    broker.nav = 98500.0  # -1.5% on the day, limit is 1%
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"] is True
        assert not (await get_control(s, ControlKey.DRAWDOWN_BREAKER) or {}).get("tripped")
    broker.nav = 94000.0  # -6% from peak, limit is 5%
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ControlKey.DRAWDOWN_BREAKER))["tripped"] is True


async def test_flatten_all(db, broker, parts):
    _, executor, _ = parts
    broker.add_external_trade(units=1000, price=1.1, sl=1.09)
    closed = await executor.flatten_all("test")
    assert closed == 1 and not broker.open_trades
    [o] = await orders(db)
    assert o.purpose == "CLOSE" and o.status == "FILLED"


def test_r_multiple():
    assert r_multiple("BUY", 1.1, 1.104, 0.002) == pytest.approx(2.0)
    assert r_multiple("SELL", 1.1, 1.102, 0.002) == pytest.approx(-1.0)
    assert r_multiple("BUY", 1.1, None, 0.002) is None
