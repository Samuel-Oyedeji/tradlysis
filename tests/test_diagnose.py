"""The no-trades diagnosis (app/diagnose.py) against recorded cycles."""

from __future__ import annotations

from datetime import timedelta

from app import diagnose
from app.config.store import single_experiment
from app.db.models import Decision, DecisionRequest, RiskCheck
from app.market_data.timeutil import utcnow
from tests.conftest import make_settings

SLUG = "v1-trend-pullback-eur-usd"


def strat(candidate: bool, failed: list[str] = (), codes: list[str] = ()) -> dict:
    conds = [{"name": n, "passed": False, "detail": ""} for n in failed] + [{"name": "ok", "passed": True}]
    return {"setup": "TREND_PULLBACK", "candidate": candidate, "conditions": conds, "failure_codes": list(codes)}


async def seed(db, rows):
    t0 = utcnow() - timedelta(days=1)
    async with db.session() as s:
        for i, (strategy, decision, risk) in enumerate(rows):
            r = DecisionRequest(experiment=SLUG, instrument="EUR_USD", candle_time=t0 + timedelta(minutes=15 * i),
                                snapshot={}, strategy_result=strategy)
            s.add(r)
            await s.flush()
            if decision:
                d = Decision(request_id=r.id, reason_codes=decision.pop("codes", []), **decision)
                s.add(d)
                await s.flush()
                if risk is not None:
                    s.add(RiskCheck(request_id=r.id, decision_id=d.id, approved=not risk, rejection_reasons=risk,
                                    checks=[{"name": n, "passed": False, "detail": f"{n} detail"} for n in risk]))


async def test_funnel_names_where_cycles_stop(db, capsys):
    prefilter = {"source": "PREFILTER", "decision": "WAIT"}
    await seed(db, [
        (strat(False, ["pullback_to_level", "momentum"], ["NO_PULLBACK_TO_LEVEL", "NO_MOMENTUM_CONFIRMATION"]),
         {**prefilter, "codes": ["NO_PULLBACK_TO_LEVEL"]}, None),
        (strat(False, ["risk_reward"], ["POOR_RISK_REWARD"]), {**prefilter, "codes": ["POOR_RISK_REWARD"]}, None),
        (strat(True), {**prefilter, "codes": ["NEWS_BLACKOUT"]}, None),
        (strat(True), {"source": "LLM", "decision": "WAIT", "confidence": 0.7, "codes": ["MOMENTUM_WEAK"]}, None),
        (strat(True), {"source": "LLM", "decision": "BUY", "confidence": 0.65, "codes": ["HTF_BULLISH"]},
         ["SPREAD_LIMIT"]),
    ])
    f = await diagnose.build_funnel(db, SLUG, utcnow() - timedelta(days=7))
    assert (f.cycles, f.setups, f.model_calls, f.risk_checks, f.approved) == (5, 3, 2, 1, 0)
    assert f.near_misses == {"risk_reward": 1}
    assert f.prefilter_on_setup == {"NEWS_BLACKOUT": 1}
    assert f.decisions == {"WAIT": 1, "BUY": 1} and f.trade_confidences == [0.65]
    assert f.risk_rejections == {"SPREAD_LIMIT": 1} and f.risk_details["SPREAD_LIMIT"] == "SPREAD_LIMIT detail"
    exp = single_experiment(make_settings()).experiments[0]
    assert "risk engine rejected all 1" in diagnose.verdict(f, exp)

    settings = make_settings(database_url=db.engine.url.render_as_string(hide_password=False))
    assert await diagnose.run(settings, 7) == 0
    out = capsys.readouterr().out
    assert f"■ {SLUG}" in out and "near misses (only one rule failed): risk_reward 1" in out
    assert "SPREAD_LIMIT: SPREAD_LIMIT detail" in out and "Engine: no heartbeat" in out


def test_verdicts():
    exp = single_experiment(make_settings()).experiments[0]
    assert "no cycles recorded" in diagnose.verdict(diagnose.Funnel(), exp)
    f = diagnose.Funnel(cycles=40)
    f.rule_failures.update(["H1_CONFLICT", "H1_CONFLICT", "NO_HTF_TREND"])
    assert "found no setup in 40 cycles (most common blocker: H1_CONFLICT)" in diagnose.verdict(f, exp)
    f = diagnose.Funnel(cycles=40, setups=3, model_calls=3)
    f.decisions.update({"WAIT": 3})
    assert "model answered WAIT to all 3" in diagnose.verdict(f, exp)
