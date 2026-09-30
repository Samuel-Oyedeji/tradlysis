import pytest

from app.market_data.candles import Bar
from app.technicals.indicators import atr, ema, percentile_rank, rsi, sma
from app.technicals.structure import Structure, Trend, classify_structure, find_swings, trend_direction
from tests.conftest import trend_bars


def test_sma_and_ema_seed():
    values = [1, 2, 3, 4, 5, 6]
    assert sma(values, 3)[2:] == [2, 3, 4, 5]
    e = ema(values, 3)
    assert e[:2] == [None, None]
    assert e[2] == pytest.approx(2.0)  # seeded with SMA
    assert e[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5


def test_rsi_extremes_and_known_value():
    assert rsi([float(i) for i in range(20)], 14)[-1] == 100.0
    assert rsi([float(20 - i) for i in range(20)], 14)[-1] == 0.0
    # StockCharts' Wilder example. Exact arithmetic: avg gain 3.34/14, avg loss 1.40/14 -> RSI 70.46
    # (their published 70.53 comes from rounding the averages).
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert rsi(closes, 14)[14] == pytest.approx(70.464, abs=0.001)


def test_atr_constant_range():
    bars = [Bar(None, 1.0, 1.001, 0.999, 1.0) for _ in range(30)]  # type: ignore[arg-type]
    a = atr(bars, 14)
    assert a[13] is None
    assert a[14] == pytest.approx(0.002)
    assert a[-1] == pytest.approx(0.002)


def test_percentile_rank():
    assert percentile_rank([1, 2, 3, 4], 4) == pytest.approx(87.5)
    assert percentile_rank([], 1) == 50.0


def test_swings_and_structure_uptrend():
    bars = trend_bars(120, 1.10, 0.0005)
    swings = find_swings(bars, 2, 2)
    assert any(s.kind == "HIGH" for s in swings) and any(s.kind == "LOW" for s in swings)
    assert classify_structure(swings) == Structure.BULLISH
    # No swing can be detected in the last `right` bars (no look-ahead).
    assert max(s.index for s in swings) <= len(bars) - 3


def test_structure_downtrend():
    bars = trend_bars(120, 1.20, -0.0005)
    assert classify_structure(find_swings(bars, 2, 2)) == Structure.BEARISH


def test_trend_direction_rules():
    assert trend_direction(1.2, 1.19, 1.18, 1.17, Structure.BULLISH) == Trend.BULLISH
    assert trend_direction(1.1, 1.11, 1.12, 1.13, Structure.BEARISH) == Trend.BEARISH
    # bullish EMAs but bearish structure -> neutral
    assert trend_direction(1.2, 1.19, 1.18, 1.17, Structure.BEARISH) == Trend.NEUTRAL
    assert trend_direction(1.2, None, 1.18, 1.17, Structure.BULLISH) == Trend.UNKNOWN
