"""Trend following (4h channel breakout) and London breakout strategies."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from app.decision.prompts import LONDON_BREAKOUT, TREND_FOLLOWING
from app.decision.schema import DecisionOut
from app.decision.service import parse_decision_answers
from app.engine import evaluate_strategy
from app.market_data.candles import Bar
from app.strategy.registry import STRATEGIES
from app.strategy.session_breakout import evaluate_session_breakout
from app.strategy.trend_following import evaluate_trend_following
from app.technicals.structure import Trend
from tests.conftest import make_settings
from tests.helpers import mirror, state, tf

S = make_settings(min_risk_reward=2.0)


def bars(start: datetime, minutes: int, pts: list[tuple[float, float, float, float]]) -> list[Bar]:
    return [Bar(start + timedelta(minutes=minutes * i), o, h, lo, c) for i, (o, h, lo, c) in enumerate(pts)]


# ---------------------------------------------------------------------- trend following

H4_START = datetime(2026, 9, 1, tzinfo=UTC)


def h4_bars(last_close: float = 1.1060, prev_close: float = 1.1030) -> list[Bar]:
    pts = [(1.1020, 1.1040, 1.1000, 1.1020)] * 23 + [(1.1020, 1.1035, 1.1015, prev_close)]
    pts.append((prev_close, max(prev_close, last_close) + 0.0005, min(prev_close, last_close) - 0.0005, last_close))
    return bars(H4_START, 240, pts)


def trend_state(b: list[Bar], *, ema50=1.1050, ema200=1.1000, atr=0.0020, minutes_after=15):
    h4 = dataclasses.replace(tf("H4", Trend.BULLISH, ema50=ema50, atr=atr, bars=b), ema200=ema200)
    st = state(h4, tf("H1", Trend.BULLISH), tf("M15", Trend.BULLISH), [], b[-1].close)
    return dataclasses.replace(st, as_of=b[-1].time + timedelta(hours=4, minutes=minutes_after))


def test_trend_following_breakout_with_the_trend():
    r = evaluate_trend_following(trend_state(h4_bars()), 1.1060, 1.1061, S)
    assert r.candidate, [(c.name, c.detail) for c in r.conditions if not c.passed]
    assert r.setup == "TREND_FOLLOWING" and r.direction == "BUY"
    p = r.trade_plan
    assert p.entry == 1.1061 and p.risk_pips == 30.0 and p.stop_loss == pytest.approx(1.1031)
    assert p.take_profit == pytest.approx(1.1061 + 0.0075) and p.risk_reward == 2.5
    assert r.context["channel_high"] == 1.104


def test_trend_following_short_mirrors():
    b = mirror(h4_bars(), 2.2)
    r = evaluate_trend_following(trend_state(b, ema50=2.2 - 1.1050, ema200=2.2 - 1.1000), 1.0939, 1.0940, S)
    assert r.candidate and r.direction == "SELL" and r.trade_plan.stop_loss > r.trade_plan.entry


@pytest.mark.parametrize(
    ("kwargs", "failure"),
    [
        ({"b": h4_bars(last_close=1.1030)}, "NO_CHANNEL_BREAKOUT"),
        ({"b": h4_bars(prev_close=1.1045)}, "STALE_SIGNAL"),  # the previous 4h candle already broke out
        ({"minutes_after": 90}, "STALE_SIGNAL"),  # too long after the 4h close
        ({"ema50": 1.0990}, "HTF_OPPOSES"),
        ({"atr": 0.0040}, "STOP_TOO_WIDE"),  # 60 pips > max 40
    ],
)
def test_trend_following_rules_block(kwargs, failure):
    b = kwargs.pop("b", h4_bars())
    r = evaluate_trend_following(trend_state(b, **kwargs), 1.1060, 1.1061, S)
    assert not r.candidate and failure in r.failure_codes, r.failure_codes


def test_trend_following_wider_stop_allowed_by_settings():
    r = evaluate_trend_following(trend_state(h4_bars(), atr=0.0040), 1.1060, 1.1061, make_settings(max_stop_pips=80))
    assert r.candidate and r.trade_plan.risk_pips == 60.0


# ---------------------------------------------------------------------- London breakout

WINTER = datetime(2026, 1, 13, tzinfo=UTC)  # a Tuesday; London = UTC
SUMMER = datetime(2026, 7, 14, tzinfo=UTC)  # a Tuesday; London = UTC+1


def session_state(day: datetime, m15_pts, *, utc_offset_h=0, atr_h1=0.0012, atr_m15=0.0008):
    midnight = day - timedelta(hours=utc_offset_h)  # 00:00 London
    h1 = bars(midnight - timedelta(hours=4), 60, [(1.1020, 1.1030, 1.1010, 1.1020)] * 4  # yesterday evening
              + [(1.1020, 1.1040 if i == 3 else 1.1030, 1.1000 if i == 5 else 1.1010, 1.1020) for i in range(8)])
    m15 = bars(midnight + timedelta(hours=8), 15, m15_pts)
    st = state(tf("H4", Trend.NEUTRAL), tf("H1", Trend.NEUTRAL, atr=atr_h1, bars=h1),
               tf("M15", Trend.NEUTRAL, atr=atr_m15, bars=m15), [], m15[-1].close)
    return dataclasses.replace(st, as_of=m15[-1].time + timedelta(minutes=15))


QUIET = (1.1025, 1.1032, 1.1022, 1.1030)
BREAK_UP = (1.1032, 1.1052, 1.1031, 1.1050)


def test_london_breakout_first_break_after_the_range():
    r = evaluate_session_breakout(session_state(WINTER, [QUIET, QUIET, BREAK_UP]), 1.1050, 1.1051, S)
    assert r.candidate, [(c.name, c.detail) for c in r.conditions if not c.passed]
    assert r.setup == "LONDON_BREAKOUT" and r.direction == "BUY"
    assert (r.context["range_high"], r.context["range_low"]) == (1.104, 1.1)
    p = r.trade_plan
    assert p.stop_loss == pytest.approx(1.1020) and p.risk_pips == 31.0  # middle of the 40-pip range
    assert p.take_profit == pytest.approx(1.1051 + 2 * 0.0031)


def test_london_hours_follow_british_summer_time():
    r = evaluate_session_breakout(session_state(SUMMER, [QUIET, QUIET, BREAK_UP], utc_offset_h=1), 1.1050, 1.1051, S)
    assert r.candidate and r.context["range_high"] == 1.104


@pytest.mark.parametrize(
    ("pts", "kwargs", "failure"),
    [
        ([QUIET, QUIET, QUIET], {}, "NO_BREAKOUT"),
        ([QUIET, (1.1030, 1.1048, 1.1029, 1.1046), BREAK_UP], {}, "LATE_BREAKOUT"),
        ([QUIET] * 17 + [BREAK_UP], {}, "OUTSIDE_SESSION"),  # closes 12:30, after the entry window
        ([QUIET, QUIET, BREAK_UP], {"atr_h1": 0.0040}, "RANGE_TOO_NARROW"),
        ([QUIET, QUIET, BREAK_UP], {"atr_h1": 0.0005}, "RANGE_TOO_WIDE"),
    ],
)
def test_london_breakout_rules_block(pts, kwargs, failure):
    r = evaluate_session_breakout(session_state(WINTER, pts, **kwargs), 1.1050, 1.1051, S)
    assert not r.candidate and failure in r.failure_codes, r.failure_codes


def test_no_london_breakout_at_the_weekend():
    saturday = WINTER + timedelta(days=4)
    r = evaluate_session_breakout(session_state(saturday, [QUIET, QUIET, BREAK_UP]), 1.1050, 1.1051, S)
    assert r.failure_codes == ["OUTSIDE_SESSION"]


# ---------------------------------------------------------------------- wiring


def test_strategies_are_registered_and_dispatched():
    assert {"trend_following", "london_breakout"} <= set(STRATEGIES)
    r = evaluate_strategy("trend_following", trend_state(h4_bars()), 1.1060, 1.1061, S)
    assert r.setup == "TREND_FOLLOWING"
    r = evaluate_strategy("london_breakout", session_state(WINTER, [QUIET, QUIET, BREAK_UP]), 1.1050, 1.1051, S)
    assert r.setup == "LONDON_BREAKOUT"


@pytest.mark.parametrize("prompt", [TREND_FOLLOWING, LONDON_BREAKOUT])
def test_prompts_name_no_pair_and_parse(prompt):
    assert "EUR/USD" not in prompt.system_prompt + prompt.instructions and prompt.setup in prompt.instructions
    answers = {"decision": {"type": "choice", "choice": "SELL", "probabilities": {"SELL": 0.7, "WAIT": 0.3}},
               "breakout_decisive": {"type": "noul", "noul": 0.9}}
    out = DecisionOut.model_validate(parse_decision_answers(answers, {}, prompt))
    assert out.setup == prompt.setup and "BREAKOUT_CONFIRMED" in out.reason_codes


def test_new_settings_are_validated():
    with pytest.raises(ValueError, match="TREND_CHANNEL_BARS"):
        make_settings(trend_channel_bars=40)
    with pytest.raises(ValueError, match="SESSION_RANGE_START_HOUR"):
        make_settings(session_range_end_hour=13, session_entry_end_hour=12)
    with pytest.raises(ValueError, match="at most 7 hours"):
        make_settings(session_entry_end_hour=16)
