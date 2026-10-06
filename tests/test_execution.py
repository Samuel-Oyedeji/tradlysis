"""Executor + reconciliation against the fake Capital.com broker and a real (test) PostgreSQL database."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.alerts.notifier import Notifier
from app.broker.types import InstrumentInfo
from app.db.control import get_control, set_control
from app.db.enums import ControlKey
from app.db.models import BrokerTransaction, Decision, DecisionRequest, Order, RiskCheck, SystemEvent, Trade
from app.execution.executor import OrderExecutor
from app.reconciliation.reconciler import Reconciler, r_multiple
from app.risk.engine import CheckResult, RiskResult
from tests.conftest import make_settings
from tests.fake_broker import FakeBroker, make_client
from tests.helpers import T0

INST = InstrumentInfo("EUR_USD", -4, 5, 0, 100.0, 0.0333, "EURUSD")


@pytest.fixture(autouse=True)
def fast_lookup(monkeypatch):
    monkeypatch.setattr("app.execution.executor.LOOKUP_DELAY_SECONDS", 0)
    monkeypatch.setattr("app.execution.executor.CONFIRM_DELAY_SECONDS", 0)


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def parts(db, broker):
    settings = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    client = make_client(broker)
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


async def events(db, event_type) -> list[SystemEvent]:
    async with db.session() as s:
        return list((await s.scalars(select(SystemEvent).where(SystemEvent.event_type == event_type))).all())


async def test_fill_records_order_and_trade(db, broker, parts):
    _, executor, _ = parts
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FILLED", order.reject_reason
    assert order.fill_price == pytest.approx(1.1026)
    posted = broker.order_posts[0]
    assert posted == {"epic": "EURUSD", "direction": "BUY", "size": 10000, "guaranteedStop": False,
                      "trailingStop": False, "stopLevel": 1.1006, "profitLevel": 1.1076}
    assert order.price_bound == pytest.approx(1.1027)  # entry + 1 pip max slippage
    assert order.broker_order_id.startswith("o_")
    [deal_id] = broker.positions
    assert order.broker_trade_id == deal_id
    [t] = await trades(db)
    assert t.state == "OPEN" and t.order_id == order.id and not t.unexpected
    assert t.broker_trade_id == deal_id and t.initial_units == 10000
    assert t.initial_risk_price == pytest.approx(0.0020)


async def test_sell_fill(db, broker, parts):
    _, executor, _ = parts
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved("SELL"))
    assert order.status == "FILLED" and order.filled_units == -10000
    assert broker.order_posts[0]["direction"] == "SELL" and broker.order_posts[0]["size"] == 10000
    [t] = await trades(db)
    assert t.direction == "SELL" and t.initial_units == -10000


async def test_one_order_per_risk_check(db, broker, parts):
    _, executor, _ = parts
    req_id, rc_id = await chain(db)
    await executor.execute_trade(rc_id, req_id, approved())
    with pytest.raises(IntegrityError):  # unique(risk_check_id)
        await executor.execute_trade(rc_id, req_id, approved())
    assert len(broker.order_posts) == 1


async def test_open_only_guard_blocks_second_position(db, broker, parts):
    _, executor, _ = parts
    broker.add_external_position(units=-5000, price=1.1030, sl=1.1060)
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "FAILED" and o.reject_reason.startswith("OPEN_ONLY")
    assert not broker.order_posts, "an opposite order could net against the open position"


async def test_timeout_after_fill_is_resolved_not_resubmitted(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "timeout_after"
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FILLED"
    assert len(broker.order_posts) == 1, "must not resubmit an order the broker already filled"
    assert len(broker.positions) == 1
    [t] = await trades(db)
    assert t.order_id == order.id and not t.unexpected


async def test_timeout_without_fill_fails_and_is_never_resubmitted(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "timeout_before"
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FAILED" and order.attempts == 1
    assert "not resubmitted" in order.reject_reason
    assert len(broker.order_posts) == 1 and not broker.positions


async def test_failed_unknown_order_is_adopted_if_the_position_appears_later(db, broker, parts):
    _, executor, reconciler = parts
    broker.order_mode = "timeout_before"
    req_id, rc_id = await chain(db)
    order = await executor.execute_trade(rc_id, req_id, approved())
    assert order.status == "FAILED"
    # The broker executed it after all (its history lagged): reconciliation links it instead of alerting.
    broker._fill(broker.order_posts[0])
    assert await reconciler.reconcile_once()
    [o] = await orders(db)
    assert o.status == "FILLED" and o.broker_trade_id in broker.positions
    [t] = await trades(db)
    assert t.order_id == o.id and not t.unexpected
    assert not await events(db, "UNEXPECTED_POSITION")


async def test_rejections(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "reject"
    req_id, rc_id = await chain(db, 0)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "REJECTED" and o.reject_reason.startswith("error.invalid.size")
    broker.order_mode = "reject_confirm"
    req_id, rc_id = await chain(db, 1)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "REJECTED" and o.reject_reason == "INSUFFICIENT_FUNDS"
    assert not broker.positions and not await trades(db)


async def test_http_500_without_trace_fails_without_retry(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "http500"
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "FAILED" and o.attempts == 1 and len(broker.order_posts) == 1


async def test_unconfirmed_order_resolved_later_by_deal_reference(db, broker, parts):
    _, executor, _ = parts
    broker.order_mode = "no_confirm"
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "SUBMITTED" and o.broker_order_id
    broker.release_confirms()
    await executor.resolve_unresolved_orders()
    [o] = await orders(db)
    assert o.status == "FILLED" and o.broker_trade_id in broker.positions
    assert len(await trades(db)) == 1


async def test_slippage_beyond_bound_is_closed(db, broker, parts):
    _, executor, _ = parts
    broker.fill_offset = 0.0003  # 3 pips worse than requested; bound is 1 pip
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "FILLED" and o.fill_price == pytest.approx(1.1029)
    assert not broker.positions, "position must be closed when the fill breaks the slippage bound"
    [entry, close] = await orders(db)
    assert close.purpose == "CLOSE" and close.status == "FILLED"
    assert len(await events(db, "SLIPPAGE_EXCEEDED")) == 1


async def test_slippage_within_bound_is_kept(db, broker, parts):
    _, executor, _ = parts
    broker.fill_offset = 0.00005  # half a pip
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    assert o.status == "FILLED" and len(broker.positions) == 1


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
    assert await reconciler.reconcile_once()  # first run: sets history cursor, snapshots
    req_id, rc_id = await chain(db)
    o = await executor.execute_trade(rc_id, req_id, approved())
    [deal_id] = broker.positions
    broker.close_position_at(deal_id, 1.1006, source="SL")
    assert await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.state == "CLOSED" and t.close_reason == "STOP_LOSS"
    assert t.close_price == pytest.approx(1.1006)
    assert t.r_multiple == pytest.approx(-1.0)
    assert t.realized_pl == Decimal("-20.000000")
    async with db.session() as s:
        txs = (await s.scalars(select(BrokerTransaction).order_by(BrokerTransaction.time))).all()
    assert {tx.type for tx in txs} >= {"POSITION_OPENED", "POSITION_CLOSED", "TRADE"}
    assert any(tx.trade_id == deal_id and tx.reason == "SL" for tx in txs)
    assert o.status == "FILLED"
    # A second pass re-reads the overlap window without duplicating the audit trail.
    assert await reconciler.reconcile_once()
    async with db.session() as s:
        assert await s.scalar(select(func.count()).select_from(BrokerTransaction)) == len(txs)


async def test_take_profit_close(db, broker, parts):
    _, executor, reconciler = parts
    req_id, rc_id = await chain(db)
    await executor.execute_trade(rc_id, req_id, approved())
    [deal_id] = broker.positions
    broker.close_position_at(deal_id, 1.1076, source="TP")
    assert await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.close_reason == "TAKE_PROFIT" and t.r_multiple == pytest.approx(2.5)
    assert t.realized_pl == Decimal("50.000000")


async def test_close_without_history_waits_then_closes(db, broker, parts, monkeypatch):
    _, executor, reconciler = parts
    req_id, rc_id = await chain(db)
    await executor.execute_trade(rc_id, req_id, approved())
    [deal_id] = broker.positions
    broker.positions.pop(deal_id)  # gone, but no close activity
    assert await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.state == "OPEN", "activity history may lag; wait before closing blind"
    monkeypatch.setattr("app.reconciliation.reconciler.CLOSE_DETAILS_GRACE_SECONDS", 0)
    assert await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.state == "CLOSED" and t.close_reason == "CLOSED_UNKNOWN" and t.close_price is None


async def test_unexpected_position_flagged(db, broker, parts):
    _, _, reconciler = parts
    broker.add_external_position(units=5000, price=1.1)
    await reconciler.reconcile_once()
    [t] = await trades(db)
    assert t.unexpected and t.order_id is None and t.initial_units == 5000
    [ev] = await events(db, "UNEXPECTED_POSITION")
    assert ev.level == "CRITICAL"


async def test_account_view_and_breakers(db, broker, parts):
    settings, _, reconciler = parts
    await reconciler.reconcile_once()
    assert reconciler.account.nav == Decimal("100000.0") and reconciler.account.currency == "USD"
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


async def test_bad_account_readings_never_trip_breakers(db, broker, parts):
    _, _, reconciler = parts
    assert await reconciler.reconcile_once()
    good = reconciler.account
    assert good.nav == Decimal("100000.0")

    broker.nav = 0.0  # Capital.com hiccup: equity reported as 0
    assert await reconciler.reconcile_once()
    broker.nav = 100000.0
    broker.account_without_balance = True  # ... or no balance at all
    assert await reconciler.reconcile_once()

    assert reconciler.account is good, "the last valid reading is kept"
    async with db.session() as s:
        assert not (await get_control(s, ControlKey.DAILY_LOSS_BREAKER) or {}).get("tripped")
        assert not (await get_control(s, ControlKey.DRAWDOWN_BREAKER) or {}).get("tripped")
        assert (await get_control(s, ControlKey.PEAK_NAV))["value"] == "100000.0"
        assert (await get_control(s, ControlKey.DAY_START_NAV))["value"] == "100000.0"
    [ev] = await events(db, "ACCOUNT_READING_IGNORED")  # one warning per run of bad readings, not a critical
    assert ev.level == "WARNING"
    assert not await events(db, "DRAWDOWN_BREAKER") and not await events(db, "DAILY_LOSS_BREAKER")

    # Valid readings resume normal behaviour, including real breaker trips.
    broker.account_without_balance = False
    broker.nav = 98500.0
    assert await reconciler.reconcile_once()
    assert reconciler.account.nav == Decimal("98500.0")
    assert len(await events(db, "ACCOUNT_READING_RECOVERED")) == 1
    async with db.session() as s:
        assert (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"] is True


async def test_session_expiry_relogs_in(db, broker, parts):
    _, _, reconciler = parts
    assert await reconciler.reconcile_once()
    assert broker.logins == 1
    broker.expire_session()
    assert await reconciler.reconcile_once()
    assert broker.logins == 2


async def test_flatten_all(db, broker, parts):
    _, executor, _ = parts
    broker.add_external_position(units=1000, price=1.1, sl=1.09)
    closed = await executor.flatten_all("test")
    assert closed == 1 and not broker.positions
    [o] = await orders(db)
    assert o.purpose == "CLOSE" and o.status == "FILLED" and o.units == -1000
    assert o.fill_price == pytest.approx(broker.bid)


def test_r_multiple():
    assert r_multiple("BUY", 1.1, 1.104, 0.002) == pytest.approx(2.0)
    assert r_multiple("SELL", 1.1, 1.102, 0.002) == pytest.approx(-1.0)
    assert r_multiple("BUY", 1.1, None, 0.002) is None


async def test_breaker_resets_do_not_re_trip(db, broker, parts):
    from app.db.control import reset_breaker

    _, _, reconciler = parts
    await reconciler.reconcile_once()
    broker.nav = 94000.0  # -6% on the day and from the peak: both breakers trip
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"] is True
        assert (await get_control(s, ControlKey.DRAWDOWN_BREAKER))["tripped"] is True
        await reset_breaker(s, ControlKey.DAILY_LOSS_BREAKER, "admin")
        await reset_breaker(s, ControlKey.DRAWDOWN_BREAKER, "admin")

    await reconciler.reconcile_once()
    async with db.session() as s:
        assert not (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"]
        assert not (await get_control(s, ControlKey.DRAWDOWN_BREAKER))["tripped"]
        assert (await get_control(s, ControlKey.DAY_START_NAV))["value"] == "94000.0"
        assert (await get_control(s, ControlKey.PEAK_NAV))["value"] == "94000.0"

    broker.nav = 93000.0  # a further -1.06% from the new base: the daily limit applies again
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ControlKey.DAILY_LOSS_BREAKER))["tripped"] is True
        assert not (await get_control(s, ControlKey.DRAWDOWN_BREAKER))["tripped"]
