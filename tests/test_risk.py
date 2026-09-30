from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.broker.types import DEFAULT_INSTRUMENTS
from app.risk.conversion import conversion_rates
from app.risk.engine import OpenTradeRisk, RiskContext, evaluate
from app.strategy.trend_pullback import TradePlan
from tests.helpers import T0

PLAN = TradePlan("BUY", "TREND_PULLBACK", 1.1026, 1.1006, 1.1076, 20.0, 50.0, 2.5, "PULLBACK_EXTREME", "SWING_CLUSTER")


def ctx(**kw) -> RiskContext:
    base = RiskContext(
        now=T0,
        decision="BUY",
        decision_valid=True,
        confidence=0.8,
        trade_plan=PLAN,
        strategy_candidate=True,
        kill_switch_active=False,
        kill_switch_reason="",
        daily_breaker_tripped=False,
        drawdown_breaker_tripped=False,
        market_open=True,
        stream_connected=True,
        bid=1.1025,
        ask=1.1026,
        price_time=T0 - timedelta(seconds=1),
        tradeable=True,
        instrument=DEFAULT_INSTRUMENTS["EUR_USD"],
        account_currency="USD",
        nav=Decimal("100000"),
        balance=Decimal("100000"),
        margin_available=Decimal("100000"),
        account_state_time=T0 - timedelta(seconds=5),
        day_start_nav=Decimal("100000"),
        peak_nav=Decimal("100000"),
        quote_home_rate=1.0,
        base_home_rate=1.1025,
    )
    return replace(base, **kw)


def failed(result):
    return set(result.rejection_reasons)


def test_baseline_approved_and_sized(settings):
    r = evaluate(ctx(), settings)
    assert r.approved, r.rejection_reasons
    # 0.25% of 100,000 USD = 250 USD over a 20-pip (0.0020) stop -> 125,000 units
    assert r.units == 125000
    assert r.risk_pct == pytest.approx(0.25, abs=1e-6)
    assert r.entry == 1.1026 and r.stop_loss == 1.1006 and r.take_profit == 1.1076
    assert r.risk_reward == pytest.approx(2.5)


def test_sell_units_are_negative(settings):
    plan = TradePlan("SELL", "TREND_PULLBACK", 1.1025, 1.1045, 1.0975, 20, 50, 2.5, "x", "y")
    r = evaluate(ctx(decision="SELL", trade_plan=plan), settings)
    assert r.approved, r.rejection_reasons
    assert r.units == -125000


def test_eur_account_uses_conversion(settings):
    q, b = conversion_rates("EUR", "EUR_USD", 1.10255)
    assert b == 1.0 and q == pytest.approx(1 / 1.10255)
    r = evaluate(ctx(account_currency="EUR", quote_home_rate=q, base_home_rate=b), settings)
    assert r.approved
    # risk 250 EUR = 275.6 USD over 0.0020 -> ~137,818 units
    assert r.units == pytest.approx(137818, abs=2)


def test_third_currency_uses_cross_rates():
    q, b = conversion_rates("GBP", "EUR_USD", 1.10, {"USD": 0.75, "EUR": 0.855})
    assert q == 0.75 and b == 0.855
    # Missing cross rate -> sizing is impossible (the risk engine then rejects the trade).
    assert conversion_rates("NGN", "EUR_USD", 1.10, {}) == (None, None)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"kill_switch_active": True}, "KILL_SWITCH"),
        ({"daily_breaker_tripped": True}, "DAILY_LOSS_BREAKER"),
        ({"drawdown_breaker_tripped": True}, "DRAWDOWN_BREAKER"),
        ({"decision": "WAIT"}, "DECISION_VALID"),
        ({"decision_valid": False}, "DECISION_VALID"),
        ({"confidence": 0.4}, "MIN_CONFIDENCE"),
        ({"trade_plan": None}, "TRADE_PLAN_MATCHES"),
        ({"strategy_candidate": False}, "TRADE_PLAN_MATCHES"),
        ({"decision": "SELL"}, "TRADE_PLAN_MATCHES"),
        ({"market_open": False}, "MARKET_OPEN"),
        ({"tradeable": False}, "MARKET_OPEN"),
        ({"stream_connected": False}, "PRICE_FRESH"),
        ({"price_time": T0 - timedelta(seconds=60)}, "PRICE_FRESH"),
        ({"ask": 1.1045}, "SPREAD_LIMIT"),
        ({"news_blackout": True, "news_blackout_titles": ["Non-Farm Payrolls"]}, "NEWS_BLACKOUT"),
        ({"calendar_fresh": False}, "NEWS_CALENDAR_FRESH"),
        ({"open_trades": [OpenTradeRisk("EUR_USD", 1000, 1.1, 1.09)]}, "EXISTING_POSITION"),
        ({"unresolved_orders": 1}, "NO_UNRESOLVED_ORDERS"),
        ({"last_entry_same_direction_at": T0 - timedelta(minutes=10)}, "SIGNAL_COOLDOWN"),
        ({"account_state_time": T0 - timedelta(minutes=10)}, "ACCOUNT_HEALTH"),
        ({"nav": Decimal("98900")}, "MAX_DAILY_LOSS"),
        ({"peak_nav": Decimal("106000")}, "MAX_DRAWDOWN"),
        ({"day_start_nav": None}, "MAX_DAILY_LOSS"),
        ({"quote_home_rate": None}, "POSITION_SIZE"),
        ({"margin_available": Decimal("1000")}, "MARGIN"),
    ],
)
def test_each_check_can_reject(settings, overrides, reason):
    r = evaluate(ctx(**overrides), settings)
    assert not r.approved
    assert reason in failed(r), r.rejection_reasons


def test_breaker_flags_set(settings):
    assert evaluate(ctx(nav=Decimal("98900")), settings).trip_daily_breaker
    assert evaluate(ctx(peak_nav=Decimal("106000")), settings).trip_drawdown_breaker


def test_plan_revalidated_against_current_price(settings):
    # Price ran up after the decision: the stop is now too far and R:R too small.
    r = evaluate(ctx(bid=1.1060, ask=1.1061), settings)
    assert not r.approved
    assert {"MIN_RISK_REWARD"} <= failed(r)


def test_stop_on_wrong_side_rejected(settings):
    r = evaluate(ctx(bid=1.1000, ask=1.1001), settings)  # price below the stop
    assert "STOP_TARGET_SIDES" in failed(r)


def test_open_trade_without_stop_blocks_exposure(settings):
    s = settings.model_copy(update={"max_open_trades": 2})
    r = evaluate(ctx(open_trades=[OpenTradeRisk("GBP_USD", 1000, 1.3, None)]), s)
    assert "MAX_TOTAL_RISK" in failed(r)


def test_trading_disabled_setting(settings):
    s = settings.model_copy(update={"trading_enabled": False})
    assert "TRADING_ENABLED" in failed(evaluate(ctx(), s))


def test_every_check_recorded_even_when_early_fail(settings):
    r = evaluate(ctx(kill_switch_active=True), settings)
    names = {c.name for c in r.checks}
    assert {"KILL_SWITCH", "SPREAD_LIMIT", "POSITION_SIZE", "MARGIN", "MIN_RISK_REWARD"} <= names
