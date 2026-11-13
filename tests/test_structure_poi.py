"""Structure, liquidity & POI strategy (sweep -> break of structure -> first return to the POI)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from app.decision.prompts import STRUCTURE_POI
from app.decision.schema import DecisionOut
from app.decision.service import parse_decision_answers
from app.engine import evaluate_strategy
from app.market_data.candles import Bar
from app.strategy.registry import STRATEGIES
from app.strategy.structure_poi import evaluate_structure_poi
from app.technicals.structure import Trend
from tests.conftest import make_settings
from tests.helpers import mirror, state, tf

S = make_settings(min_risk_reward=2.0)
START = datetime(2026, 9, 1, tzinfo=UTC)
STEP = 0.0005
WICK = 0.0001


def path_bars(start: datetime, minutes: int, points: list[float]) -> list[Bar]:
    """Candles walking from each point to the next in 5-pip steps (1-pip wicks)."""
    out: list[Bar] = []
    for a, b in zip(points, points[1:], strict=False):
        n = round(abs(b - a) / STEP)
        d = STEP if b > a else -STEP
        for k in range(n):
            o, c = round(a + d * k, 5), round(a + d * (k + 1), 5)
            out.append(Bar(start + timedelta(minutes=minutes * len(out)), o, max(o, c) + WICK, min(o, c) - WICK, c))
    return out


# Down to 1.1040 (previous low), lower high 1.1070, sweep to 1.1020 (protected low), up to 1.1060, structural
# liquidity 1.1045, break above 1.1070 to the swing high 1.1110, retrace to 1.1050 (not yet at the POI).
LONG_PATH = [1.1080, 1.1100, 1.1040, 1.1070, 1.1020, 1.1060, 1.1045, 1.1110, 1.1050]
QUIET = (1.1052, 1.1056, 1.1048, 1.1050)  # 15m candles above the POI (1.1034-1.1041)
TAP = (1.1050, 1.1051, 1.1038, 1.1044)  # trades into the POI, closes back above its low


def setup_state(m15_pts=(QUIET, QUIET, QUIET, TAP), *, path=LONG_PATH, h4=Trend.BULLISH, flipped=False,
                structure_tf="H1"):
    minutes = 60 if structure_tf == "H1" else 240
    st_bars = path_bars(START, minutes, path)
    m15_start = st_bars[-1].time + timedelta(minutes=minutes)
    m15 = [Bar(m15_start + timedelta(minutes=15 * i), *p) for i, p in enumerate(m15_pts)]
    if flipped:
        st_bars, m15 = mirror(st_bars), mirror(m15)
    other = tf("H4" if structure_tf == "H1" else "H1", Trend.NEUTRAL)
    structure = tf(structure_tf, h4 if structure_tf == "H4" else Trend.NEUTRAL, atr=0.0010, bars=st_bars)
    h4_state, h1_state = (tf("H4", h4), structure) if structure_tf == "H1" else (structure, other)
    st = state(h4_state, h1_state, tf("M15", Trend.NEUTRAL, atr=0.0005, bars=m15), [], m15[-1].close)
    return dataclasses.replace(st, as_of=m15[-1].time + timedelta(minutes=15))


def failed(r):
    return [(c.name, c.detail) for c in r.conditions if not c.passed]


def test_long_setup_at_the_poi():
    r = evaluate_structure_poi(setup_state(), 1.1044, 1.1045, S)
    assert r.candidate, failed(r)
    assert r.setup == "STRUCTURE_POI" and r.direction == "BUY"
    c = r.context
    assert (c["protected_level"], c["swept_level"], c["broken_level"]) == (1.1019, 1.1039, 1.1071)
    assert (c["swing_extreme"], c["liquidity_level"]) == (1.1111, 1.1044)
    assert (c["poi_low"], c["poi_high"]) == (1.1034, 1.1041)  # the bearish 1.1040 -> 1.1035 candle
    p = r.trade_plan
    assert p.entry == 1.1045 and p.stop_loss == pytest.approx(1.1018)  # 1 pip (0.1 ATR) below the low
    assert p.take_profit == pytest.approx(1.1111) and p.target_source == "SWING_HIGH"
    assert p.risk_pips == 27.0 and p.risk_reward == pytest.approx(66 / 27, abs=0.01)


def test_short_mirrors():
    pivot = 2.2
    r = evaluate_structure_poi(setup_state(flipped=True, h4=Trend.BEARISH), pivot - 1.1045, pivot - 1.1044, S)
    assert r.candidate, failed(r)
    assert r.direction == "SELL"
    p = r.trade_plan
    assert p.stop_loss == pytest.approx(pivot - 1.1018) and p.take_profit == pytest.approx(pivot - 1.1111)
    assert (r.context["poi_low"], r.context["poi_high"]) == (round(pivot - 1.1041, 6), round(pivot - 1.1034, 6))


def test_fixed_target():
    r = evaluate_structure_poi(setup_state(), 1.1044, 1.1045, make_settings(smc_target_r=2.0))
    assert r.candidate and r.trade_plan.take_profit == pytest.approx(1.1045 + 2 * 0.0027)
    assert r.trade_plan.target_source == "FIXED_2R"


@pytest.mark.parametrize(
    ("kwargs", "settings", "failure"),
    [
        ({"m15_pts": (QUIET, QUIET, QUIET)}, {}, "NOT_AT_POI"),
        ({"m15_pts": (QUIET, TAP, QUIET, TAP)}, {}, "POI_ALREADY_TAPPED"),
        ({"m15_pts": (QUIET, (1.1050, 1.1051, 1.1028, 1.1030))}, {}, "POI_BROKEN"),
        ({"m15_pts": (QUIET, (1.1050, 1.1051, 1.1015, 1.1040))}, {}, "STRUCTURE_BROKEN"),
        ({"h4": Trend.BEARISH}, {}, "HTF_OPPOSES"),
        ({"path": [1.1080, 1.1100, 1.1040, 1.1070, 1.1020, 1.1060, 1.1045, 1.1065, 1.1050]}, {}, "NO_STRUCTURE"),
        ({}, {"smc_liquidity_max_frac": 0.2}, "LIQUIDITY_TOO_HIGH"),
        ({}, {"smc_poi_max_gap_atr": 0.2}, "NO_POI"),
        ({}, {"smc_min_swing_atr": 10}, "SWING_TOO_SMALL"),
        ({}, {"min_risk_reward": 3.0}, "POOR_RISK_REWARD"),
        ({}, {"max_stop_pips": 20}, "STOP_TOO_WIDE"),
    ],
)
def test_rules_block(kwargs, settings, failure):
    r = evaluate_structure_poi(setup_state(**kwargs), 1.1044, 1.1045, make_settings(**settings))
    assert not r.candidate and failure in r.failure_codes, r.failure_codes


DEEP_TAP = (1.1050, 1.1051, 1.1022, 1.1030)  # through the 1.1034 POI, into the base of the move


@pytest.mark.parametrize(
    ("mode", "m15_last", "zone", "ok"),
    [
        ("LIQUIDITY", TAP, (1.1034, 1.1041), True),
        ("LIQUIDITY", DEEP_TAP, (1.1034, 1.1041), False),
        ("DISCOUNT", TAP, (1.1034, 1.1041), True),  # the liquidity candle itself (1.1044) is not a POI
        ("ORIGIN", TAP, (1.1019, 1.1026), False),  # not reached yet
        ("ORIGIN", DEEP_TAP, (1.1019, 1.1026), True),  # the last down candle into the protected low
    ],
)
def test_poi_modes(mode, m15_last, zone, ok):
    r = evaluate_structure_poi(setup_state((QUIET, QUIET, QUIET, m15_last)), 1.1044, 1.1045,
                               make_settings(smc_poi_mode=mode))
    assert (r.context["poi_low"], r.context["poi_high"]) == zone
    assert r.candidate == ok, failed(r)


def test_htf_filter_can_be_switched_off():
    r = evaluate_structure_poi(setup_state(h4=Trend.BEARISH), 1.1044, 1.1045, make_settings(smc_htf_filter=False))
    assert r.candidate, failed(r)


def test_structure_on_the_4h_chart():
    r = evaluate_structure_poi(setup_state(structure_tf="H4"), 1.1044, 1.1045, make_settings(smc_structure_tf="H4"))
    assert r.candidate, failed(r)
    assert r.context["structure_timeframe"] == "H4"


def test_insufficient_data():
    r = evaluate_structure_poi(setup_state(path=[1.1000, 1.1010]), 1.1044, 1.1045, S)
    assert r.failure_codes == ["INSUFFICIENT_DATA"]


def test_registered_and_dispatched():
    assert STRATEGIES["structure_poi"].setup == "STRUCTURE_POI"
    assert evaluate_strategy("structure_poi", setup_state(), 1.1044, 1.1045, S).candidate


def test_prompt_names_no_pair_and_parses():
    p = STRUCTURE_POI
    assert "EUR/USD" not in p.system_prompt + p.instructions and p.setup in p.instructions
    answers = {"decision": {"type": "choice", "choice": "BUY", "probabilities": {"BUY": 0.8, "WAIT": 0.2}},
               "structure_intact": {"type": "noul", "noul": 0.9}, "poi_reaction": {"type": "noul", "noul": 0.9}}
    out = DecisionOut.model_validate(parse_decision_answers(answers, {}, p))
    assert out.setup == "STRUCTURE_POI" and {"STRUCTURE_INTACT", "PULLBACK_TO_SUPPORT"} <= set(out.reason_codes)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"smc_structure_tf": "M15"}, "SMC_STRUCTURE_TF"),
        ({"smc_poi_mode": "ANY"}, "SMC_POI_MODE"),
        ({"smc_swing_bars": 0}, "SMC_SWING_BARS"),
        ({"smc_lookback_bars": 400}, "SMC_LOOKBACK_BARS"),
        ({"smc_liquidity_max_frac": 1.5}, "SMC_LIQUIDITY_MAX_FRAC"),
        ({"smc_target_r": -1}, "SMC_TARGET_R"),
    ],
)
def test_settings_are_validated(kwargs, match):
    with pytest.raises(ValueError, match=match):
        make_settings(**kwargs)
