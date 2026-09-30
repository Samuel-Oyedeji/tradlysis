import json

import httpx
import pytest

from app.broker.oanda import OandaClient, OandaError
from app.market_data.candles import parse_oanda_candle
from app.market_data.state import MarketState, PriceTick
from app.market_data.timeutil import floor_time, is_fx_market_open, parse_time, trading_day


def make(handler, stream_handler=None) -> OandaClient:
    return OandaClient(
        "https://api.test", "https://stream.test", "tok", "101-001-1-001",
        transport=httpx.MockTransport(handler),
        stream_transport=httpx.MockTransport(stream_handler or handler),
        max_get_retries=2,
    )


async def test_get_retries_on_503(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = []

    def handler(req):
        calls.append(req.url.path)
        assert req.headers["Authorization"] == "Bearer tok"
        if len(calls) < 2:
            return httpx.Response(503, json={"errorMessage": "busy"})
        return httpx.Response(200, json={"account": {"id": "101-001-1-001", "NAV": "100000"}})

    assert (await make(handler).get_account_summary())["NAV"] == "100000"
    assert len(calls) == 2


async def test_client_errors_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(401, json={"errorMessage": "Insufficient authorization"})

    with pytest.raises(OandaError) as e:
        await make(handler).get_account_summary()
    assert e.value.status_code == 401 and len(calls) == 1


async def test_get_order_404_is_none():
    c = make(lambda r: httpx.Response(404, json={"errorCode": "ORDER_DOESNT_EXIST"}))
    assert await c.get_order("@abc") is None


async def test_create_order_returns_status_and_body():
    def handler(req):
        body = json.loads(req.content)
        assert body["order"]["type"] == "MARKET"
        return httpx.Response(400, json={"orderRejectTransaction": {"rejectReason": "INSUFFICIENT_MARGIN"}})

    status, body = await make(handler).create_order({"type": "MARKET"})
    assert status == 400 and body["orderRejectTransaction"]["rejectReason"] == "INSUFFICIENT_MARGIN"


async def test_price_stream_parsing():
    lines = [
        {"type": "PRICE", "instrument": "EUR_USD", "time": "2026-09-29T10:00:00.123456789Z",
         "bids": [{"price": "1.10250", "liquidity": 1000000}], "asks": [{"price": "1.10256", "liquidity": 1000000}],
         "closeoutBid": "1.10245", "closeoutAsk": "1.10261", "tradeable": True},
        {"type": "HEARTBEAT", "time": "2026-09-29T10:00:05.000000000Z"},
    ]
    content = ("\n".join(json.dumps(x) for x in lines) + "\n").encode()

    def stream_handler(req):
        assert req.url.path.endswith("/pricing/stream")
        return httpx.Response(200, content=content)

    msgs = [m async for m in make(stream_handler).stream_prices(["EUR_USD"])]
    assert [m["type"] for m in msgs] == ["PRICE", "HEARTBEAT"]
    tick = PriceTick.from_stream(msgs[0])
    assert tick.bid == 1.1025 and tick.ask == 1.10256
    assert tick.spread_pips(0.0001) == 0.6
    state = MarketState("EUR_USD", 0.0001)
    state.stream_connected = True
    state.on_tick(tick, tick.time)
    assert not state.is_price_stale(tick.time, 15)
    assert state.is_price_stale(parse_time("2026-09-29T10:01:00Z"), 15)


def test_parse_candle_and_time_helpers():
    bar = parse_oanda_candle({"time": "2026-09-29T10:00:00.000000000Z", "volume": 42, "complete": True,
                              "mid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.15"},
                              "bid": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.1499"},
                              "ask": {"o": "1.1", "h": "1.2", "l": "1.0", "c": "1.1501"}})
    assert bar.close == 1.15 and bar.bid_close == 1.1499 and bar.complete and bar.volume == 42
    t = parse_time("2026-09-29T10:07:31Z")
    assert floor_time(t, "M15").isoformat() == "2026-09-29T10:00:00+00:00"
    # Saturday closed, Sunday 22:00 UTC (18:00 NY) open, Friday 21:30 UTC (17:30 NY) closed
    assert not is_fx_market_open(parse_time("2026-10-03T12:00:00Z"))
    assert is_fx_market_open(parse_time("2026-10-04T22:00:00Z"))
    assert not is_fx_market_open(parse_time("2026-10-02T21:30:00Z"))
    # Trading day rolls at 17:00 New York (21:00 UTC in EDT)
    assert trading_day(parse_time("2026-09-29T20:59:00Z")).isoformat() == "2026-09-29"
    assert trading_day(parse_time("2026-09-29T21:00:00Z")).isoformat() == "2026-09-30"


async def _no_sleep(*_a, **_k):
    return None
