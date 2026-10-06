"""Executor + reconciliation against the fake Capital.com broker and a real (test) PostgreSQL database."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.alerts.notifier import Notifier
from app.broker.types import InstrumentInfo
from app.db.control import get_control, scoped, set_control
from app.db.enums import ControlKey
from app.db.models import BrokerTransaction, Decision, DecisionRequest, Order, RiskCheck, SystemEvent, Trade
from app.execution.executor import OrderExecutor
from app.reconciliation.reconciler import ExperimentBook, Reconciler, r_multiple
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


async def chain(db, candle_offset=0, experiment="test") -> tuple[int, int]:
    """Insert request -> decision -> risk_check rows; returns (request_id, risk_check_id)."""
    from datetime import timedelta

    async with db.session() as s:
        req = DecisionRequest(experiment=experiment, instrument="EUR_USD", candle_time=T0 + timedelta(minutes=15 * candle_offset),
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


SLUG = "v1-trend-pullback-eur-usd"  # make_settings()' experiment


async def closed_trade(db, pl: float, experiment: str = SLUG) -> None:
    """A closed trade of an experiment with the given realized P/L (moves its equity)."""
    async with db.session() as s:
        n = await s.scalar(select(func.count()).select_from(Trade))
        s.add(Trade(broker_trade_id=f"closed-{experiment}-{n}", experiment=experiment, instrument="EUR_USD",
                    direction="BUY", initial_units=1000, current_units=0, open_price=1.1, open_time=T0,
                    state="CLOSED", realized_pl=Decimal(str(pl)), raw={}))


def ctl(key: ControlKey, experiment: str = SLUG) -> str:
    return scoped(key, experiment)


async def test_experiment_equity_and_breakers(db, broker, parts):
    settings, _, reconciler = parts
    await reconciler.reconcile_once()
    assert reconciler.account.nav == Decimal("100000.0") and reconciler.account.currency == "USD"
    eq = reconciler.equity[SLUG]
    assert eq.capital == Decimal("100000.0") and eq.equity == Decimal("100000.0")
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.PEAK_NAV)))["value"] == "100000.0"
        assert (await get_control(s, ctl(ControlKey.DAY_START_NAV)))["value"] == "100000.0"
    # The account's NAV alone no longer matters: only the experiment's own P/L does.
    broker.nav = 90000.0
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert not (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)) or {}).get("tripped")
    await closed_trade(db, -1500)  # -1.5% of the experiment's equity on the day, limit is 1%
    await reconciler.reconcile_once()
    assert reconciler.equity[SLUG].equity == Decimal("98500.0")
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"] is True
        assert not (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)) or {}).get("tripped")
    await closed_trade(db, -4500)  # -6% from peak, limit is 5%
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)))["tripped"] is True


async def test_breakers_are_per_experiment(db, broker, parts):
    settings, executor, _ = parts
    other = settings.model_copy(update={"experiment_name": "other", "max_daily_loss_pct": 2.0})
    saved = {}

    async def on_capital(slug, capital):
        saved[slug] = capital

    books = {
        SLUG: ExperimentBook(SLUG, settings),
        "other": ExperimentBook("other", other, capital=Decimal("10000")),
    }
    reconciler = Reconciler(settings, executor.client, db, Notifier(db, None), executor, books, on_capital)
    await reconciler.reconcile_once()
    assert saved == {SLUG: Decimal("100000.0")}, "only an experiment without capital takes the account balance"
    await closed_trade(db, -150, "other")  # -1.5% of the other experiment's 10,000: below its 2% limit
    await closed_trade(db, -1200)  # -1.2% of the first: above its 1% limit
    await reconciler.reconcile_once()
    assert reconciler.equity["other"].equity == Decimal("9850")
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"] is True
        assert not (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER, "other")) or {}).get("tripped")
    [ev] = await events(db, "DAILY_LOSS_BREAKER")
    assert ev.details["experiment"] == SLUG


async def test_bad_account_readings_never_trip_breakers(db, broker, parts):
    _, _, reconciler = parts
    assert await reconciler.reconcile_once()
    good = reconciler.account
    assert good.nav == Decimal("100000.0")
    await closed_trade(db, -6000)

    broker.nav = 0.0  # Capital.com hiccup: equity reported as 0
    assert await reconciler.reconcile_once()
    broker.nav = 100000.0
    broker.account_without_balance = True  # ... or no balance at all
    assert await reconciler.reconcile_once()

    assert reconciler.account is good, "the last valid reading is kept"
    async with db.session() as s:
        assert not (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)) or {}).get("tripped")
        assert not (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)) or {}).get("tripped")
        assert (await get_control(s, ctl(ControlKey.PEAK_NAV)))["value"] == "100000.0"
        assert (await get_control(s, ctl(ControlKey.DAY_START_NAV)))["value"] == "100000.0"
    [ev] = await events(db, "ACCOUNT_READING_IGNORED")  # one warning per run of bad readings, not a critical
    assert ev.level == "WARNING"
    assert not await events(db, "DRAWDOWN_BREAKER") and not await events(db, "DAILY_LOSS_BREAKER")

    # Valid readings resume normal behaviour, including real breaker trips.
    broker.account_without_balance = False
    assert await reconciler.reconcile_once()
    assert len(await events(db, "ACCOUNT_READING_RECOVERED")) == 1
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"] is True


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
    await closed_trade(db, -6000)  # -6% on the day and from the peak: both breakers trip
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"] is True
        assert (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)))["tripped"] is True
        await reset_breaker(s, ControlKey.DAILY_LOSS_BREAKER, "admin", SLUG)
        await reset_breaker(s, ControlKey.DRAWDOWN_BREAKER, "admin", SLUG)

    await reconciler.reconcile_once()
    async with db.session() as s:
        assert not (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"]
        assert not (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)))["tripped"]
        assert Decimal((await get_control(s, ctl(ControlKey.DAY_START_NAV)))["value"]) == Decimal("94000")
        assert Decimal((await get_control(s, ctl(ControlKey.PEAK_NAV)))["value"]) == Decimal("94000")

    await closed_trade(db, -1000)  # a further -1.06% from the new base: the daily limit applies again
    await reconciler.reconcile_once()
    async with db.session() as s:
        assert (await get_control(s, ctl(ControlKey.DAILY_LOSS_BREAKER)))["tripped"] is True
        assert not (await get_control(s, ctl(ControlKey.DRAWDOWN_BREAKER)))["tripped"]


async def test_flatten_one_experiment_only(db, broker, parts):
    settings, executor, reconciler = parts
    broker.hedging_mode = True
    _, rc_a = await chain(db, 0)
    await executor.execute_trade(rc_a, 1, approved(), settings.model_copy(update={"experiment_name": "test"}))
    external = broker.add_external_position(units=1000, price=1.1, sl=1.09)
    [mine] = [d for d in broker.positions if d != external]
    await reconciler.reconcile_once()
    owners = await executor.position_owners([mine, external])
    assert owners == {mine: "test", external: None}
    closed = await executor.flatten_all("test only", "test")
    assert closed == 1 and list(broker.positions) == [external]


async def test_hedging_lets_experiments_share_an_instrument(db, broker, parts):
    settings, executor, reconciler = parts
    exp = settings.model_copy(update={"experiment_name": "test"})
    _, rc1 = await chain(db, 0)
    assert (await executor.execute_trade(rc1, 1, approved(), exp)).status == "FILLED"
    await reconciler.reconcile_once()
    other = settings.model_copy(update={"experiment_name": "other"})
    _, rc2 = await chain(db, 1, "other")
    blocked = await executor.execute_trade(rc2, 2, approved(), other)
    assert blocked.status == "FAILED" and blocked.reject_reason.startswith("OPEN_ONLY"), "netting account"
    broker.hedging_mode = True
    _, rc3 = await chain(db, 2, "other")
    assert (await executor.execute_trade(rc3, 3, approved(), other)).status == "FILLED"
    assert sorted((await executor.position_owners(list(broker.positions))).values()) == ["other", "test"]
    _, rc4 = await chain(db, 3)
    again = await executor.execute_trade(rc4, 4, approved(), exp)
    assert again.status == "FAILED", "still one position per experiment and instrument"
