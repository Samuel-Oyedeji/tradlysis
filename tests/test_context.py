"""Deterministic context added from the design conversation: news impact facts, reference levels, regimes."""

import pytest

from app.market_data.candles import Bar
from app.news.state import EventView, build_news_state
from app.news.values import currency_impact, pair_impact
from app.technicals.levels import psychological_levels, reference_levels
from app.technicals.regime import MarketRegimeLabel, VolatilityRegime, classify_market_regime
from app.technicals.structure import Trend
from tests.helpers import T0


@pytest.mark.parametrize(
    "title,direction,expected",
    [
        ("CPI m/m", "BELOW", "BEARISH"),  # CPI 2.8 vs 3.1 expected -> USD bearish
        ("Non-Farm Employment Change", "ABOVE", "BULLISH"),
        ("Unemployment Rate", "ABOVE", "BEARISH"),  # higher unemployment is bad for the currency
        ("Unemployment Claims", "BELOW", "BULLISH"),
        ("GDP q/q", "INLINE", "NEUTRAL"),
        ("GDP q/q", None, None),
    ],
)
def test_currency_impact(title, direction, expected):
    assert currency_impact(title, direction) == expected


def test_pair_impact():
    assert pair_impact("USD", "BEARISH", "EUR", "USD") == "UP"
    assert pair_impact("USD", "BULLISH", "EUR", "USD") == "DOWN"
    assert pair_impact("EUR", "BULLISH", "EUR", "USD") == "UP"
    assert pair_impact("JPY", "BULLISH", "EUR", "USD") is None


def test_news_state_carries_impact():
    from datetime import timedelta

    ev = EventView("CPI m/m", "USD", "HIGH", T0 - timedelta(hours=2), "0.3%", "0.2%", "0.1%", -0.2, "BELOW")
    recent = build_news_state([ev], [], T0, ["EUR", "USD"]).recent[0]
    assert recent["currency_impact"] == "BEARISH" and recent["pair_impact"] == "UP"


def test_psychological_levels():
    levels = psychological_levels(1.18421, 0.0001)
    assert [lv.price for lv in levels] == [pytest.approx(1.17), pytest.approx(1.18), pytest.approx(1.19)]
    assert all(lv.source == "ROUND_NUMBER" for lv in levels)


def test_monthly_reference_levels():
    bar = Bar(T0, 1.1, 1.2, 1.0, 1.15)
    sources = {lv.source for lv in reference_levels([bar], [bar], [bar])}
    assert {"PREV_DAY_HIGH", "PREV_WEEK_LOW", "PREV_MONTH_HIGH", "PREV_MONTH_LOW"} <= sources


def regime(**kw):
    base = dict(
        h4_trend=Trend.NEUTRAL, h1_trend=Trend.NEUTRAL, h4_structure_bullish=False, h4_structure_bearish=False,
        h1_close=1.10, h1_prior_high=1.11, h1_prior_low=1.09, volatility=VolatilityRegime.NORMAL, news_blackout=False,
    )
    base.update(kw)
    return classify_market_regime(**base)


def test_market_regimes():
    assert regime(h4_trend=Trend.BULLISH, h1_trend=Trend.BULLISH, h4_structure_bullish=True) == MarketRegimeLabel.STRONG_UPTREND
    assert regime(h4_trend=Trend.BEARISH, h1_trend=Trend.BEARISH) == MarketRegimeLabel.STRONG_DOWNTREND
    assert regime(h4_trend=Trend.BULLISH, h1_trend=Trend.BULLISH, news_blackout=True) == MarketRegimeLabel.EVENT_RISK
    assert regime(h1_close=1.12) == MarketRegimeLabel.BREAKOUT_UP
    assert regime(h1_close=1.08) == MarketRegimeLabel.BREAKOUT_DOWN
    assert regime(volatility=VolatilityRegime.LOW) == MarketRegimeLabel.COMPRESSION
    assert regime() == MarketRegimeLabel.RANGE
    assert regime(h4_trend=Trend.BULLISH, h1_trend=Trend.BEARISH) == MarketRegimeLabel.UNCLEAR
