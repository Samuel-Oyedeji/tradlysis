from datetime import timedelta

import pytest

from app.news.providers.forexfactory import parse_item
from app.news.providers.rss import parse_feed
from app.news.state import EventView, InterpretationView, build_news_state
from app.news.values import parse_value, surprise
from tests.helpers import T0


@pytest.mark.parametrize(
    "text,value",
    [("3.1%", 3.1), ("250K", 250_000), ("-0.2B", -2e8), ("1,234.5", 1234.5), ("<0.10%", 0.1), ("", None), ("n/a", None)],
)
def test_parse_value(text, value):
    assert parse_value(text) == (pytest.approx(value) if value is not None else None)


def test_surprise():
    assert surprise(3.2, 3.0) == (pytest.approx(0.2), "ABOVE")
    assert surprise(2.9, 3.0)[1] == "BELOW"
    assert surprise(3.0, 3.0) == (0.0, "INLINE")
    assert surprise(None, 3.0) == (None, None)


def test_forexfactory_item():
    e = parse_item(
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00",
         "impact": "High", "forecast": "150K", "previous": "142K"}
    )
    assert e.currency == "USD" and e.impact == "HIGH"
    assert e.event_time.isoformat() == "2026-10-02T12:30:00+00:00"
    assert e.forecast == "150K" and e.actual is None
    # stable id -> upserts instead of duplicates
    assert e.external_id == parse_item(
        {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High"}
    ).external_id
    assert parse_item({"title": "x"}) is None


def test_rss_parse():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Fed</title>
      <item><title>FOMC statement</title><link>https://example.org/a</link><guid>a1</guid>
      <pubDate>Wed, 17 Sep 2026 18:00:00 GMT</pubDate><description>The Committee decided...</description></item>
    </channel></rss>"""
    items = parse_feed(xml, "rss:usd", "USD")
    assert len(items) == 1
    assert items[0].title == "FOMC statement" and items[0].currency == "USD"
    assert items[0].published_at.isoformat() == "2026-09-17T18:00:00+00:00"


def ev(minutes: int, impact="HIGH", currency="USD", title="CPI"):
    return EventView(title, currency, impact, T0 + timedelta(minutes=minutes))


def test_blackout_window():
    s = build_news_state([ev(20)], [], T0, ["EUR", "USD"])
    assert s.blackout and s.risk == "high"
    assert s.blackout_events[0]["minutes_until"] == 20
    # just released: still inside the after-window
    assert build_news_state([ev(-25)], [], T0, ["EUR", "USD"]).blackout
    # far away / other currency / medium impact -> no blackout
    assert not build_news_state([ev(90)], [], T0, ["EUR", "USD"]).blackout
    assert not build_news_state([ev(10, currency="JPY")], [], T0, ["EUR", "USD"]).blackout
    assert not build_news_state([ev(10, impact="MEDIUM")], [], T0, ["EUR", "USD"]).blackout


def test_risk_levels():
    assert build_news_state([ev(120)], [], T0, ["EUR", "USD"]).risk == "medium"  # high impact within 4h
    assert build_news_state([ev(600)], [], T0, ["EUR", "USD"]).risk == "low"
    assert build_news_state([], [], T0, ["EUR", "USD"], calendar_fresh=False).risk == "unknown"
    nxt = build_news_state([ev(600)], [], T0, ["EUR", "USD"]).next_high_impact
    assert nxt["title"] == "CPI"


def test_bias_aggregation():
    interps = [
        InterpretationView("USD", "BULLISH", "HAWKISH", 0.9, T0 - timedelta(hours=1)),
        InterpretationView("USD", "BULLISH", "HAWKISH", 0.6, T0 - timedelta(hours=10)),
        InterpretationView("EUR", "BEARISH", "DOVISH", 0.8, T0 - timedelta(hours=2)),
        InterpretationView("EUR", "BULLISH", "HAWKISH", 0.9, T0 - timedelta(hours=100)),  # outside lookback
    ]
    b = build_news_state([], interps, T0, ["EUR", "USD"]).currency_bias
    assert b["USD"]["bias"] == "BULLISH" and b["USD"]["items"] == 2
    assert b["EUR"]["bias"] == "BEARISH" and b["EUR"]["items"] == 1
