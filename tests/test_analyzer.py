import pytest

from app.experiments.analyzer import (
    ClosedTrade,
    compute_metrics,
    confidence_bucket,
    max_drawdown,
    nav_drawdown_pct,
    r_stats,
)


def trade(r, conf=0.75, vol="NORMAL", news="low", h1="fully aligned"):
    return ClosedTrade(
        r=r, pl=r * 100, direction="BUY", close_reason="TAKE_PROFIT" if r > 0 else "STOP_LOSS", confidence=conf,
        snapshot={"volatility": {"regime": vol}, "news": {"risk": news}, "regimes": {"1h": "TRENDING_UP"}},
        strategy={"conditions": [{"name": "h1_alignment", "detail": f"H1 trend is bullish ({h1})"},
                                 {"name": "pullback_to_level", "detail": "tested SWING_CLUSTER 1.1-1.1"}]},
        requested_price=1.1000, fill_price=1.1001, unexpected=False,
    )


def test_r_stats_and_drawdown():
    s = r_stats([2.0, -1.0, -1.0, 2.5, -1.0])
    assert s["trades"] == 5 and s["wins"] == 2 and s["win_rate"] == 0.4
    assert s["avg_r"] == pytest.approx(0.3) and s["avg_winner_r"] == 2.25 and s["avg_loser_r"] == -1.0
    assert s["profit_factor"] == pytest.approx(1.5)
    assert max_drawdown([2.0, -1.0, -1.0, 2.5, -1.0]) == 2.0
    assert nav_drawdown_pct([100, 110, 99, 120]) == 10.0
    assert r_stats([]) == {"trades": 0}
    assert confidence_bucket(0.82) == "0.8-0.9" and confidence_bucket(1.0) == "0.9-1.0" and confidence_bucket(None) == "none"


def test_compute_metrics_groups():
    trades = [trade(2.0, 0.85), trade(-1.0, 0.65, vol="HIGH", news="medium"), trade(-1.0, 0.7, h1="x")]
    m = compute_metrics(
        requests=[{"snapshot": {"price": {"spread_pips": 0.6}}, "strategy_result": {"candidate": True}, "llm_called": True},
                  {"snapshot": {"price": {"spread_pips": 1.0}}, "strategy_result": {"candidate": False}, "llm_called": False}],
        decisions=[{"decision": "BUY", "source": "LLM", "valid": True, "reason_codes": ["HTF_BULLISH"], "latency_ms": 900},
                   {"decision": "WAIT", "source": "PREFILTER", "valid": True, "reason_codes": ["NO_HTF_TREND"], "latency_ms": None}],
        risk_checks=[{"approved": True, "rejection_reasons": []}, {"approved": False, "rejection_reasons": ["SPREAD_LIMIT"]}],
        orders=[{"status": "FILLED"}],
        trades=trades, open_trades=0, navs=[100000, 100200, 100100], pip_size=0.0001,
    )
    assert m["opportunities"] == 2 and m["setup_candidates"] == 1 and m["llm_calls"] == 1
    assert m["rejection_reasons"] == {"SPREAD_LIMIT": 1}
    assert m["wait_reasons"] == {"NO_HTF_TREND": 1}
    assert m["closed_trades"]["trades"] == 3
    assert m["by_market_regime"]["volatility"]["HIGH"]["trades"] == 1
    assert m["by_news_risk"]["low"]["trades"] == 2
    assert set(m["by_confidence"]) == {"0.6-0.7", "0.7-0.8", "0.8-0.9"}
    assert m["by_setup_condition"]["h1_alignment"]["h1_fully_aligned"]["trades"] == 2
    assert m["by_setup_condition"]["pullback_level"]["SWING_CLUSTER"]["trades"] == 3
    assert m["execution"]["avg_slippage_pips"] == pytest.approx(1.0)
    assert m["execution"]["avg_spread_pips_at_decision"] == pytest.approx(0.8)


async def test_analyzer_run_once_on_db(db):
    from app.experiments.analyzer import run_once
    from tests.conftest import make_settings

    m = await run_once(db, make_settings())
    assert m["opportunities"] == 0 and m["closed_trades"] == {"trades": 0}
