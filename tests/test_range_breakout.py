"""Range breakout strategy (app/strategy/range_breakout.py) and its decision prompt."""

from __future__ import annotations

import pytest

from app.decision.prompts import RANGE_BREAKOUT, TREND_PULLBACK
from app.decision.schema import DecisionOut
from app.decision.service import parse_decision_answers
from app.market_data.candles import Bar
from app.strategy.range_breakout import edge_visits, evaluate_range_breakout
from app.technicals.structure import Trend
from tests.conftest import make_settings
from tests.helpers import bars_from, mirror, state, tf

HI, LO = 1.1040, 1.1000  # the range: 40 pips = 3.3 x ATR(H1) 0.0012


def range_h1() -> list[Bar]:
    """22 H1 bars swinging between the range edges (3 visits to each), plus a latest bar inside."""
    pts = []
    for i in range(22):
        top = i % 7 in (2, 3)
        bottom = i % 7 == 6
        hi = HI if top else 1.1030
        lo = LO if bottom else 1.1010
        pts.append((1.1020, hi, lo, 1.1020))
    return bars_from(pts)


def m15_bars(last: tuple[float, float, float, float], before: float = 1.1032) -> list[Bar]:
    """Five quiet M15 candles closing at ``before``, then the candle under test."""
    return bars_from([(1.1030, 1.1036, 1.1028, before)] * 5 + [last])


BREAK_UP = (1.1036, 1.1047, 1.1035, 1.1046)  # body 10 pips, closes 6 pips above the range high


def tech(*, h4=Trend.NEUTRAL, h1_bars=None, m15=None, atr_h1=0.0012, atr_m15=0.0008):
    m15b = m15 or m15_bars(BREAK_UP)
    return state(
        tf("H4", h4, atr=0.003), tf("H1", Trend.NEUTRAL, atr=atr_h1, bars=h1_bars or range_h1()),
        tf("M15", Trend.NEUTRAL, atr=atr_m15, bars=m15b), [], m15b[-1].close,
    )


SETTINGS = make_settings(min_risk_reward=1.5)


def test_edge_visits_count_separate_tests_of_an_edge():
    bars = bars_from([(1, 1.10, 1.0, 1), (1, 1.10, 1.0, 1), (1, 1.05, 1.0, 1), (1, 1.099, 1.0, 1)])
    assert edge_visits(bars, 1.10, 0.002, True) == 2


def test_upside_breakout_is_a_candidate_with_a_measured_move_plan():
    r = evaluate_range_breakout(tech(), 1.1046, 1.1047, SETTINGS)
    assert r.candidate, [(c.name, c.detail) for c in r.conditions if not c.passed]
    assert r.setup == "RANGE_BREAKOUT" and r.direction == "BUY" and r.failure_codes == []
    assert r.context["range_high"] == HI and r.context["range_low"] == LO
    assert r.context["visits_high"] >= 2 and r.context["visits_low"] >= 2
    p = r.trade_plan
    assert p.entry == 1.1047
    assert p.stop_loss == pytest.approx(HI - 0.3 * (HI - LO))  # 30% back inside the range
    assert p.take_profit == pytest.approx(HI + (HI - LO))  # one range height beyond the edge
    assert p.risk_reward == pytest.approx(1.74, abs=0.01)
    assert r.to_dict()["context"]["range_height_pips"] == 40.0


def test_downside_breakout_mirrors():
    pivot = 2.2
    h1 = [Bar(b.time, pivot - b.open, pivot - b.low, pivot - b.high, pivot - b.close) for b in range_h1()]
    m15 = mirror(m15_bars(BREAK_UP), pivot)
    r = evaluate_range_breakout(tech(h1_bars=h1, m15=m15), pivot - 1.1047, pivot - 1.1046, SETTINGS)
    assert r.candidate and r.direction == "SELL"
    assert r.trade_plan.entry == pytest.approx(pivot - 1.1047)
    assert r.trade_plan.take_profit < r.trade_plan.entry < r.trade_plan.stop_loss


@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"m15": m15_bars((1.1030, 1.1039, 1.1029, 1.1038))}, "NO_BREAKOUT"),
        ({"m15": m15_bars(BREAK_UP, before=1.1042)}, "LATE_BREAKOUT"),
        ({"m15": m15_bars((1.1043, 1.1050, 1.1040, 1.1045))}, "WEAK_BREAKOUT_CANDLE"),
        ({"h4": Trend.BEARISH}, "HTF_OPPOSES"),
        ({"atr_h1": 0.004}, "RANGE_TOO_NARROW"),
        ({"atr_h1": 0.0005}, "RANGE_TOO_WIDE"),
        ({"h1_bars": bars_from([(1.1020, 1.1040, 1.1000, 1.1020)] + [(1.1020, 1.1030, 1.1010, 1.1020)] * 21)},
         "RANGE_NOT_TESTED"),
    ],
)
def test_each_rule_can_block(kwargs, failure):
    r = evaluate_range_breakout(tech(**kwargs), 1.1046, 1.1047, SETTINGS)
    assert not r.candidate and failure in r.failure_codes, r.failure_codes


def test_risk_reward_uses_the_experiment_minimum():
    r = evaluate_range_breakout(tech(), 1.1046, 1.1047, make_settings())  # default min R:R 2.0
    assert not r.candidate and r.failure_codes == ["POOR_RISK_REWARD"]


def test_insufficient_data():
    r = evaluate_range_breakout(tech(h1_bars=range_h1()[:10]), 1.1046, 1.1047, SETTINGS)
    assert not r.candidate and r.failure_codes == ["INSUFFICIENT_DATA"]


def test_breakout_settings_are_validated():
    with pytest.raises(ValueError, match="BREAKOUT_RANGE_BARS"):
        make_settings(breakout_range_bars=40)
    with pytest.raises(ValueError, match="BREAKOUT_MIN_RANGE_ATR"):
        make_settings(breakout_min_range_atr=5, breakout_max_range_atr=4)


def test_breakout_prompt_questions_and_answers():
    q = RANGE_BREAKOUT.questions()
    assert set(q) == {"decision", "range_clear", "breakout_decisive", "htf_supports", "room_to_target", "news_risk_high"}
    assert "RANGE_BREAKOUT" in q["decision"]["instructions"] and "EUR/USD" not in q["decision"]["instructions"]
    assert "EUR/USD" not in TREND_PULLBACK.system_prompt + TREND_PULLBACK.instructions
    answers = {
        "decision": {"type": "choice", "choice": "BUY", "probabilities": {"BUY": 0.7, "WAIT": 0.3}},
        "range_clear": {"type": "noul", "noul": 0.8},
        "breakout_decisive": {"type": "noul", "noul": 0.3},
        "htf_supports": {"type": "noul", "noul": 0.9},
    }
    snapshot = {"setup_check": {"trade_plan": {"direction": "BUY"}}}
    out = DecisionOut.model_validate(parse_decision_answers(answers, snapshot, RANGE_BREAKOUT))
    assert out.setup == "RANGE_BREAKOUT" and out.confidence == 0.7
    assert [str(c) for c in out.reason_codes] == ["RANGE_DEFINED", "BREAKOUT_WEAK", "HTF_BULLISH"]


async def test_chat_model_answering_another_setup_is_rejected():
    import json

    import httpx

    from app.decision.openrouter import OpenRouterClient
    from app.decision.service import DecisionService

    body = {"decision": "BUY", "setup": "TREND_PULLBACK", "confidence": 0.8, "reason_codes": ["OTHER"], "rationale": ""}

    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(body)}}],
                                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    client = OpenRouterClient("k", "https://or.test/api/v1", transport=httpx.MockTransport(handler))
    out = await DecisionService(client, "openai/gpt-4.1-mini", 600, 0.0, RANGE_BREAKOUT).decide({"pair": "GBP_USD"})
    assert out.decision == "WAIT" and not out.valid and "RANGE_BREAKOUT" in out.validation_error
