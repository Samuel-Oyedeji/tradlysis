import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from app.broker import capital
from app.broker.capital import (
    CapitalClient,
    CapitalDataError,
    CapitalError,
    CapitalTransportError,
    account_state_from_api,
    api_time,
    instrument_from_market,
    is_close_activity,
    monthly_bars,
    parse_price_bars,
)
from app.market_data.candles import Bar
from app.market_data.state import MarketState, PriceTick
from app.market_data.timeutil import floor_time, is_fx_market_open, parse_time, trading_day

SESSION_OK = httpx.Response(
    200,
    headers={"CST": "cst-1", "X-SECURITY-TOKEN": "xst-1"},
    json={"currentAccountId": "A1", "accounts": [{"accountId": "A1"}, {"accountId": "A2"}],
          "streamingHost": "wss://stream.example/"},
)


def make(handler, **kw) -> CapitalClient:
    return CapitalClient("https://demo.test", "key", "me@example.com", "pw", transport=httpx.MockTransport(handler),
                         max_get_retries=2, **kw)


def session_then(handler):
    def wrapped(req):
        if req.url.path == "/api/v1/session" and req.method == "POST":
            body = json.loads(req.content)
            assert req.headers["X-CAP-API-KEY"] == "key"
            assert body == {"identifier": "me@example.com", "password": "pw", "encryptedPassword": False}
            return SESSION_OK
        assert req.headers["CST"] == "cst-1" and req.headers["X-SECURITY-TOKEN"] == "xst-1"
        return handler(req)

    return wrapped


async def _no_sleep(*_a, **_k):
    return None


async def test_login_resolves_account_and_stream_url():
    c = make(session_then(lambda r: httpx.Response(200, json={"accounts": [
        {"accountId": "A1", "currency": "USD",
         "balance": {"balance": 124.95, "deposit": 125.18, "profitLoss": -0.23, "available": 116.93}}]})))
    acct = await c.get_account()
    assert c.account_id == "A1" and c.stream_url == "wss://stream.example/connect"
    assert acct.nav == Decimal("124.95") and acct.balance == Decimal("125.18")
    assert acct.unrealized_pl == Decimal("-0.23") and acct.margin_available == Decimal("116.93")
    assert acct.margin_used == Decimal("8.02")


async def test_login_switches_to_configured_account():
    switched = []

    def handler(req):
        if req.method == "PUT" and req.url.path == "/api/v1/session":
            switched.append(json.loads(req.content)["accountId"])
            return httpx.Response(200, json={"dealingEnabled": True})
        return httpx.Response(200, json={"accounts": [{"accountId": "A2", "currency": "USD",
                                                       "balance": {"balance": 500, "available": 500}}]})

    c = make(session_then(handler), account_id="A2")
    assert (await c.get_account()).account_id == "A2"
    assert switched == ["A2"]


async def test_login_rejects_unknown_account():
    c = make(session_then(lambda r: httpx.Response(200, json={})), account_id="NOPE")
    with pytest.raises(CapitalError, match="not found"):
        await c.get_accounts()


async def test_login_failure_is_reported():
    c = make(lambda r: httpx.Response(401, json={"errorCode": "error.invalid.details"}))
    with pytest.raises(CapitalError, match="error.invalid.details"):
        await c.get_accounts()


async def test_expired_session_logs_in_again_once():
    logins = []
    calls = []

    def handler(req):
        if req.url.path == "/api/v1/session":
            logins.append(1)
            n = len(logins)
            return httpx.Response(200, headers={"CST": f"c{n}", "X-SECURITY-TOKEN": f"x{n}"},
                                  json={"currentAccountId": "A1"})
        calls.append(req.headers["CST"])
        if req.headers["CST"] == "c1":
            return httpx.Response(401, json={"errorCode": "error.invalid.session.token"})
        return httpx.Response(200, json={"positions": []})

    c = make(handler)
    assert await c.get_positions() == []
    assert len(logins) == 2 and calls == ["c1", "c2"]


async def test_get_retries_on_503(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 2:
            return httpx.Response(503, json={"errorCode": "busy"})
        return httpx.Response(200, json={"positions": []})

    assert await make(session_then(handler)).get_positions() == []
    assert len(calls) == 2


async def test_client_errors_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, json={"errorCode": "error.invalid.filter"})

    with pytest.raises(CapitalError) as e:
        await make(session_then(handler)).get_positions()
    assert e.value.status_code == 400 and e.value.error_code == "error.invalid.filter" and len(calls) == 1


async def test_orders_are_never_retried_and_timeouts_surface():
    calls = []

    def handler(req):
        calls.append(req.method)
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(CapitalTransportError):
        await make(session_then(handler)).open_position({"epic": "EURUSD"})
    assert calls == ["POST"]


async def test_open_position_returns_status_and_body():
    def handler(req):
        assert json.loads(req.content)["direction"] == "BUY"
        return httpx.Response(400, json={"errorCode": "error.invalid.stoploss.minvalue: 1.1"})

    status, body = await make(session_then(handler)).open_position({"direction": "BUY"})
    assert status == 400 and body["errorCode"].startswith("error.invalid.stoploss")


async def test_confirmation_and_position_404_are_none():
    c = make(session_then(lambda r: httpx.Response(404, json={"errorCode": "error.not-found.dealReference"})))
    assert await c.get_confirmation("o_x") is None
    assert await c.get_position("d1") is None


async def test_positions_are_mapped_to_instrument_names():
    item = {"position": {"dealId": "d1", "size": 10000.0, "direction": "SELL", "level": 1.1025, "upl": -1.5,
                         "createdDateUTC": "2026-09-29T09:46:01.872", "stopLevel": 1.1045, "profitLevel": 1.0975},
            "market": {"epic": "EURUSD"}}
    [p] = await make(session_then(lambda r: httpx.Response(200, json={"positions": [item]}))).get_positions()
    assert p.instrument == "EUR_USD" and p.units == -10000 and p.direction == "SELL"
    assert p.open_time == datetime(2026, 9, 29, 9, 46, 1, 872000, tzinfo=UTC)
    assert p.stop_loss == 1.1045 and p.take_profit == 1.0975 and p.unrealized_pl == Decimal("-1.5")


async def test_history_requests_stay_within_one_day():
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"activities": []})

    c = make(session_then(handler))
    end = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    await c.get_activity(end - timedelta(days=3), end)
    p = seen[0]
    assert p["to"] == "2026-09-30T12:00:00" and p["from"] == "2026-09-29T12:00:01"
    assert p["detailed"] == "true" and p["filter"] == "epic==EURUSD"


async def test_conversion_rate_uses_direct_or_inverse_market():
    def handler(req):
        epic = req.url.path.rsplit("/", 1)[-1]
        if epic == "USDNGN":
            return httpx.Response(200, json={"snapshot": {"bid": 1500.0, "offer": 1502.0}})
        return httpx.Response(404, json={"errorCode": "error.not-found.epic"})

    c = make(session_then(handler))
    assert await c.get_conversion_rate("USD", "NGN") == pytest.approx(1501.0)
    assert await c.get_conversion_rate("NGN", "USD") == pytest.approx(1 / 1501.0)
    assert await c.get_conversion_rate("EUR", "EUR") == 1.0


def test_instrument_from_market():
    inst = instrument_from_market("EUR_USD", {
        "instrument": {"epic": "EURUSD", "type": "CURRENCIES", "lotSize": 1, "marginFactor": 3.33,
                       "marginFactorUnit": "PERCENTAGE"},
        "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 100}},
        "snapshot": {"decimalPlacesFactor": 5},
    })
    assert inst.pip_size == pytest.approx(0.0001) and inst.display_precision == 5
    assert inst.minimum_trade_size == 100 and inst.trade_units_precision == 0
    assert inst.margin_rate == pytest.approx(0.0333) and inst.epic == "EURUSD" and inst.lot_size == 1
    jpy = instrument_from_market("USD_JPY", {"instrument": {"type": "CURRENCIES"}, "snapshot": {"decimalPlacesFactor": 3},
                                             "dealingRules": {"minDealSize": {"value": 0.5}}})
    assert jpy.pip_size == pytest.approx(0.01) and jpy.trade_units_precision == 1


def test_account_state_mapping():
    a = account_state_from_api({"accountId": "A", "currency": "USD",
                                "balance": {"balance": 1000, "deposit": 990, "profitLoss": 10, "available": 900}})
    assert (a.nav, a.balance, a.unrealized_pl, a.margin_used) == (Decimal("1000"), Decimal("990"), Decimal("10"), Decimal("100"))


def test_account_without_usable_balance_is_a_data_error():
    for entry in ({"accountId": "A"}, {"accountId": "A", "balance": {}}, {"accountId": "A", "balance": None},
                  {"accountId": "A", "balance": {"balance": "n/a"}}):
        with pytest.raises(CapitalDataError):
            account_state_from_api(entry)
    zero = account_state_from_api({"accountId": "A", "balance": {"balance": 0, "available": 0}})
    assert zero.nav == 0  # parsed; whether it is plausible is the reconciler's call


def test_price_bars_are_mid_and_completed_by_time():
    now = datetime(2026, 9, 29, 10, 20, tzinfo=UTC)
    raw = [
        {"snapshotTime": "2026-09-29T13:00:00", "snapshotTimeUTC": "2026-09-29T10:00:00",
         "openPrice": {"bid": 1.1000, "ask": 1.1002}, "highPrice": {"bid": 1.1010, "ask": 1.1012},
         "lowPrice": {"bid": 1.0990, "ask": 1.0992}, "closePrice": {"bid": 1.1005, "ask": 1.1007},
         "lastTradedVolume": 42},
        {"snapshotTime": "2026-09-29T13:15:00", "snapshotTimeUTC": "2026-09-29T10:15:00",
         "openPrice": {"bid": 1.1005, "ask": 1.1007}, "highPrice": {"bid": 1.1006, "ask": 1.1008},
         "lowPrice": {"bid": 1.1004, "ask": 1.1006}, "closePrice": {"bid": 1.1005, "ask": 1.1007},
         "lastTradedVolume": 3},
    ]
    first, forming = parse_price_bars(raw, "M15", now)
    assert first.time == datetime(2026, 9, 29, 10, 0, tzinfo=UTC)  # UTC, not the broker's local time
    assert first.open == pytest.approx(1.1001) and first.high == pytest.approx(1.1011)
    assert first.close == pytest.approx(1.1006) and first.bid_close == 1.1005 and first.ask_close == 1.1007
    assert first.volume == 42 and first.complete
    assert not forming.complete


def test_monthly_bars_drop_partial_first_month():
    days = [Bar(datetime(2026, m, d, tzinfo=UTC), 1.0 + d / 1000, 1.1 + d / 1000, 0.9, 1.0, 1)
            for m, d in [(7, 30), (7, 31), (8, 3), (8, 20), (9, 1), (9, 28)]]
    aug, sep = monthly_bars(days, datetime(2026, 9, 29, tzinfo=UTC))
    assert aug.time == datetime(2026, 8, 1, tzinfo=UTC) and aug.complete
    assert aug.open == pytest.approx(1.003) and aug.high == pytest.approx(1.12) and aug.volume == 2
    assert sep.time.month == 9 and not sep.complete


def test_close_activity_detection():
    opened = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    base = {"type": "POSITION", "status": "ACCEPTED", "dateUTC": "2026-09-29T11:00:00.000"}
    closed = {**base, "dealId": "x", "source": "USER",
              "details": {"actions": [{"actionType": "POSITION_CLOSED", "affectedDealId": "d1"}]}}
    opened_act = {**base, "dealId": "d1", "source": "USER",
                  "details": {"actions": [{"actionType": "POSITION_OPENED", "affectedDealId": "d1"}]}}
    assert is_close_activity(closed, "d1", opened)
    assert not is_close_activity(closed, "d2", opened)
    assert not is_close_activity(opened_act, "d1", opened)
    assert is_close_activity({**base, "dealId": "d1", "source": "TP"}, "d1", opened)
    assert not is_close_activity({**closed, "status": "REJECTED"}, "d1", opened)
    assert api_time(datetime(2026, 9, 29, 12, 0, 5, 123, tzinfo=UTC)) == "2026-09-29T12:00:05"


class FakeWs:
    def __init__(self, incoming):
        self.incoming = list(incoming)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    async def recv(self):
        if not self.incoming:
            await asyncio.sleep(3600)
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return json.dumps(item)


async def test_quote_stream(monkeypatch):
    ws = FakeWs([
        {"status": "OK", "destination": "marketData.subscribe", "correlationId": "subscribe",
         "payload": {"subscriptions": {"EURUSD": "PROCESSED"}}},
        {"status": "OK", "destination": "quote", "payload": {"epic": "EURUSD", "product": "CFD", "bid": 1.1025,
                                                             "bidQty": 1e6, "ofr": 1.10256, "ofrQty": 1e6,
                                                             "timestamp": 1790676000123}},
        {"status": "OK", "destination": "quote", "payload": {"epic": "GBPUSD", "bid": 1.3, "ofr": 1.3001}},
        {"status": "OK", "destination": "ping", "correlationId": "ping-1", "payload": {}},
        ConnectionResetError("gone"),
    ])
    urls = []

    def connect(url):
        urls.append(url)
        return ws

    c = make(session_then(lambda r: httpx.Response(200, json={})), ws_connect=connect)
    msgs = []
    with pytest.raises(CapitalTransportError):
        async for m in c.stream_quotes():
            msgs.append(m)
    assert urls == ["wss://stream.example/connect"]
    assert ws.sent[0] == {"destination": "marketData.subscribe", "correlationId": "subscribe", "cst": "cst-1",
                          "securityToken": "xst-1", "payload": {"epics": ["EURUSD"]}}
    assert msgs[0] is None and msgs[2] is None and len(msgs) == 3
    quote = msgs[1]
    assert quote.instrument == "EUR_USD" and quote.bid == 1.1025 and quote.ask == 1.10256
    assert quote.time == datetime.fromtimestamp(1790676000.123, tz=UTC)

    tick = PriceTick.from_quote(quote)
    assert tick.spread_pips(0.0001) == 0.6
    state = MarketState("EUR_USD", 0.0001)
    state.stream_connected = True
    state.on_tick(tick, tick.time)
    assert not state.is_price_stale(tick.time, 15)
    assert state.is_price_stale(tick.time + timedelta(minutes=1), 15)


async def test_quote_stream_pings_and_detects_silence(monkeypatch):
    monkeypatch.setattr(capital, "WS_PING_SECONDS", 0.01)
    monkeypatch.setattr(capital, "WS_IDLE_TIMEOUT_SECONDS", 0.3)
    ws = FakeWs([{"status": "OK", "destination": "marketData.subscribe",
                  "payload": {"subscriptions": {"EURUSD": "PROCESSED"}}}])
    c = make(session_then(lambda r: httpx.Response(200, json={})), ws_connect=lambda url: ws)
    with pytest.raises(CapitalTransportError, match="silent"):
        async for _ in c.stream_quotes():
            pass
    pings = [m for m in ws.sent if m["destination"] == "ping"]
    assert pings and pings[0]["cst"] == "cst-1"


async def test_failed_subscription_raises():
    ws = FakeWs([{"status": "OK", "destination": "marketData.subscribe",
                  "payload": {"subscriptions": {"EURUSD": "ERROR"}}}])
    c = make(session_then(lambda r: httpx.Response(200, json={})), ws_connect=lambda url: ws)
    with pytest.raises(CapitalError, match="subscription"):
        async for _ in c.stream_quotes():
            pass


def test_time_helpers():
    assert parse_time("2026-09-29T10:07:31.123") == datetime(2026, 9, 29, 10, 7, 31, 123000, tzinfo=UTC)
    t = parse_time("2026-09-29T10:07:31Z")
    assert floor_time(t, "M15").isoformat() == "2026-09-29T10:00:00+00:00"
    # Saturday closed, Sunday 22:00 UTC (18:00 NY) open, Friday 21:30 UTC (17:30 NY) closed
    assert not is_fx_market_open(parse_time("2026-10-03T12:00:00Z"))
    assert is_fx_market_open(parse_time("2026-10-04T22:00:00Z"))
    assert not is_fx_market_open(parse_time("2026-10-02T21:30:00Z"))
    # Trading day rolls at 17:00 New York (21:00 UTC in EDT)
    assert trading_day(parse_time("2026-09-29T20:59:00Z")).isoformat() == "2026-09-29"
    assert trading_day(parse_time("2026-09-29T21:00:00Z")).isoformat() == "2026-09-30"


async def test_one_client_serves_several_markets():
    seen = []

    def handler(req):
        seen.append((req.url.path, dict(req.url.params)))
        return httpx.Response(200, json={"activities": [{"epic": "EURUSD"}, {"epic": "GBPUSD"}, {"epic": "US500"}],
                                         "prices": []})

    c = make(session_then(handler))
    assert c.register_market("GBP_USD") == "GBPUSD"
    with pytest.raises(ValueError):
        c.register_market("GBP_USD", "GBPUSD_X")
    assert c.instrument_for_epic("GBPUSD") == "GBP_USD" and c.instrument_for_epic("US500") == "US500"
    end = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    both = await c.get_activity(end - timedelta(hours=1), end)
    assert "filter" not in seen[-1][1], "several markets are filtered here, not by the API"
    assert [a["epic"] for a in both] == ["EURUSD", "GBPUSD"]
    await c.get_activity(end - timedelta(hours=1), end, instrument="GBP_USD")
    assert seen[-1][1]["filter"] == "epic==GBPUSD"
    await c.get_candles("H1", 10, instrument="GBP_USD")
    assert seen[-1][0].endswith("/prices/GBPUSD")


async def test_quote_stream_subscribes_every_market():
    ws = FakeWs([
        {"status": "OK", "destination": "marketData.subscribe", "correlationId": "subscribe",
         "payload": {"subscriptions": {"EURUSD": "PROCESSED", "GBPUSD": "PROCESSED"}}},
        {"status": "OK", "destination": "quote", "payload": {"epic": "GBPUSD", "bid": 1.3, "ofr": 1.3001}},
        ConnectionResetError("gone"),
    ])
    c = make(session_then(lambda r: httpx.Response(200, json={})), ws_connect=lambda url: ws)
    c.register_market("GBP_USD")
    msgs = []
    with pytest.raises(CapitalTransportError):
        async for m in c.stream_quotes():
            msgs.append(m)
    assert ws.sent[0]["payload"] == {"epics": ["EURUSD", "GBPUSD"]}
    assert msgs[1].instrument == "GBP_USD" and msgs[1].ask == 1.3001
