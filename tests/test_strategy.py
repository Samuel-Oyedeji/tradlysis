import pytest

from app.strategy.trend_pullback import evaluate_trend_pullback
from app.technicals.engine import compute_technical_state
from app.technicals.structure import Trend
from tests.conftest import trend_bars
from tests.helpers import long_state, short_state


def test_long_candidate_with_structural_plan(settings):
    res = evaluate_trend_pullback(long_state(), bid=1.1025, ask=1.1026, settings=settings)
    assert res.candidate, res.to_dict()
    assert res.direction == "BUY"
    plan = res.trade_plan
    assert plan.entry == pytest.approx(1.1026)
    assert plan.stop_loss == pytest.approx(1.1005 - 0.00025)  # below the support zone + 0.25 ATR buffer
    assert plan.take_profit == pytest.approx(1.1080 - 0.0001)  # just under the next resistance
    assert plan.risk_reward >= settings.min_risk_reward
    assert plan.target_source == "SWING_CLUSTER"


def test_short_candidate_is_mirror(settings):
    res = evaluate_trend_pullback(short_state(), bid=2.2 - 1.1026, ask=2.2 - 1.1025, settings=settings)
    assert res.candidate, res.to_dict()
    assert res.direction == "SELL"
    assert res.trade_plan.stop_loss > res.trade_plan.entry > res.trade_plan.take_profit


def test_no_htf_trend_means_no_setup(settings):
    res = evaluate_trend_pullback(long_state(h4=Trend.NEUTRAL), 1.1025, 1.1026, settings)
    assert not res.candidate and res.direction is None
    assert res.failure_codes == ["NO_HTF_TREND"]


def test_h1_conflict_blocks(settings):
    res = evaluate_trend_pullback(long_state(h1=Trend.BEARISH), 1.1025, 1.1026, settings)
    assert not res.candidate
    assert "H1_CONFLICT" in res.failure_codes


def test_h1_neutral_is_allowed(settings):
    assert evaluate_trend_pullback(long_state(h1=Trend.NEUTRAL), 1.1025, 1.1026, settings).candidate


def test_momentum_required(settings):
    res = evaluate_trend_pullback(long_state(rsi=45.0, rsi_prev=50.0), 1.1025, 1.1026, settings)
    assert not res.candidate
    assert "NO_MOMENTUM_CONFIRMATION" in res.failure_codes


def test_close_resistance_gives_poor_rr(settings):
    res = evaluate_trend_pullback(long_state(resistance=(1.1036, 1.1040)), 1.1025, 1.1026, settings)
    assert not res.candidate
    assert "POOR_RISK_REWARD" in res.failure_codes


def test_no_resistance_uses_fixed_r_target(settings):
    res = evaluate_trend_pullback(long_state(resistance=None), 1.1025, 1.1026, settings)
    plan = res.trade_plan
    assert plan.target_source == "FIXED_2R"
    assert plan.risk_reward == pytest.approx(2.0, abs=0.01)


def test_full_pipeline_on_synthetic_uptrend():
    h4 = trend_bars(300, 1.00, 0.0010, minutes=240)
    h1 = trend_bars(300, 1.10, 0.0004, minutes=60)
    m15 = trend_bars(300, 1.20, 0.0002, minutes=15)
    tech = compute_technical_state("EUR_USD", {"H4": h4, "H1": h1, "M15": m15}, m15[-1].close, 0.0001, m15[-1].time)
    assert tech.timeframes["H4"].trend == Trend.BULLISH
    assert tech.timeframes["H4"].ema200 is not None
    assert tech.levels, "swing clusters expected"
    assert all(lv.kind in ("SUPPORT", "RESISTANCE") for lv in tech.levels)
