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


def _req(t, rr, failed=("risk_reward",), hypo=None, candidate=False):
    from datetime import UTC, datetime, timedelta

    names = ["htf_trend", "h1_alignment", "pullback_to_level", "pullback_depth", "momentum", "stop_distance", "risk_reward"]
    return {
        "snapshot": {},
        "llm_called": False,
        "time": datetime(2026, 10, 5, tzinfo=UTC) + timedelta(minutes=15 * t),
        "strategy_result": {
            "candidate": candidate,
            "conditions": [{"name": n, "passed": n not in failed} for n in names],
            "trade_plan": {"risk_reward": rr},
        },
        "hypothetical": hypo,
    }


def test_near_misses_what_if_replay():
    from datetime import UTC, datetime

    from app.experiments.analyzer import near_misses

    def resolved(result, t):
        return {"result": result, "resolved_at": datetime(2026, 10, 5, t // 4, (t % 4) * 15, tzinfo=UTC).isoformat()}

    reqs = [
        _req(0, 1.6, hypo=resolved("WOULD_WIN", 4)),
        _req(1, 1.7, hypo=resolved("WOULD_WIN", 5)),   # still "in" the first trade: skipped
        _req(8, 1.1, hypo=resolved("WOULD_LOSE", 10)),  # only counted at thresholds <= 1.1
        _req(12, 1.3, hypo=resolved("WOULD_LOSE", 14)),
        _req(20, 1.9, hypo={"result": "UNRESOLVED", "resolved_at": None}),
        _req(21, 0.5, failed=("momentum",)),
        _req(22, 0.5, failed=("momentum", "risk_reward")),  # two rules failed: not a near miss
        _req(23, 2.5, failed=(), candidate=True),
    ]
    m = near_misses(reqs)
    assert m["blocked_by_one_rule"] == {"risk_reward": 5, "momentum": 1}
    assert m["rr_only"]["candles"] == 5
    assert m["rr_only"]["by_planned_rr"] == {"1.5-2.0R": 3, "1.0-1.2R": 1, "1.2-1.5R": 1}
    w = m["rr_only"]["what_if_min_rr"]
    assert w["1"] == {"setups": 4, "would_win": 1, "would_lose": 2, "not_resolved": 1, "total_r": -0.4, "avg_r": -0.133}
    assert w["1.2"]["setups"] == 3 and w["1.2"]["total_r"] == 0.6
    assert w["1.5"] == {"setups": 2, "would_win": 1, "would_lose": 0, "not_resolved": 1, "total_r": 1.6, "avg_r": 1.6}
    assert w["1.8"]["setups"] == 1 and w["1.8"]["total_r"] is None


def test_by_planned_rr_groups_closed_trades():
    from app.experiments.analyzer import rr_bucket

    t1, t2 = trade(1.4), trade(-1.0)
    t1.strategy = {**t1.strategy, "trade_plan": {"risk_reward": 1.4}}
    t2.strategy = {**t2.strategy, "trade_plan": {"risk_reward": 2.6}}
    m = compute_metrics(requests=[], decisions=[], risk_checks=[], orders=[], trades=[t1, t2, trade(2.0)],
                        open_trades=0, navs=[], pip_size=0.0001)
    assert set(m["by_planned_rr"]) == {"1.2-1.5R", "2.0-3.0R", "unknown"}
    assert rr_bucket(0.9) == "<1.0R" and rr_bucket(3.0) == "3.0R+" and rr_bucket(1.5) == "1.5-2.0R"


async def test_analyzer_replays_rr_only_setups_on_db(db):
    from datetime import UTC, datetime, timedelta

    from app.db.models import Candle, DecisionRequest
    from app.experiments.analyzer import run_once
    from tests.conftest import make_settings

    settings = make_settings()
    t0 = datetime(2026, 10, 5, 8, tzinfo=UTC)
    conditions = [{"name": "momentum", "passed": True}, {"name": "risk_reward", "passed": False}]
    plan = {"direction": "BUY", "entry": 1.1000, "stop_loss": 1.0990, "take_profit": 1.1015, "risk_reward": 1.5}
    async with db.session() as s:
        s.add(DecisionRequest(experiment=settings.experiment_name, instrument="EUR_USD", candle_time=t0, snapshot={},
                              strategy_result={"candidate": False, "conditions": conditions, "trade_plan": plan},
                              trade_plan=plan, llm_called=False))
        # Price rises to the take-profit two candles after the decision.
        for i, hi in enumerate((1.1005, 1.1008, 1.1020)):
            s.add(Candle(instrument="EUR_USD", granularity="M15", time=t0 + timedelta(minutes=15 * (i + 1)),
                         open=1.1, high=hi, low=1.0995, close=hi - 0.0002, complete=True))

    m = await run_once(db, settings)
    rr_only = m["near_misses"]["rr_only"]
    assert rr_only["candles"] == 1 and rr_only["by_planned_rr"] == {"1.5-2.0R": 1}
    assert rr_only["what_if_min_rr"]["1.5"] == {"setups": 1, "would_win": 1, "would_lose": 0, "not_resolved": 0,
                                                "total_r": 1.5, "avg_r": 1.5}
    assert rr_only["what_if_min_rr"]["1.8"]["setups"] == 0
