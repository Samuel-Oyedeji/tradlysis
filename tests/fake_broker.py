"""In-memory fake of the Capital.com REST endpoints the system uses (served via httpx.MockTransport)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from app.market_data.candles import Bar
from app.market_data.timeutil import GRANULARITY_SECONDS, parse_time, utcnow

API_KEY = "test-api-key"
IDENTIFIER = "bot@example.com"
PASSWORD = "api-key-password"
ACCOUNT_ID = "123456789012345678"
EPIC = "EURUSD"

RESOLUTION_TO_GRANULARITY = {
    "MINUTE": "M1", "MINUTE_5": "M5", "MINUTE_15": "M15", "MINUTE_30": "M30",
    "HOUR": "H1", "HOUR_4": "H4", "DAY": "D", "WEEK": "W",
}


def api_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


@dataclass
class FakeBroker:
    nav: float = 100000.0
    currency: str = "USD"
    bid: float = 1.1025
    ask: float = 1.1026
    market_status: str = "TRADEABLE"
    hedging_mode: bool = False
    account_without_balance: bool = False  # simulate an /accounts entry with no balance object
    # behaviour switches for POST /positions
    order_mode: str = "fill"  # fill | reject | reject_confirm | no_confirm | timeout_before | timeout_after | http500
    fill_offset: float = 0.0  # added to the fill price of BUYs (subtracted for SELLs): simulated slippage
    candles: dict[str, list[Bar]] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    confirms: dict[str, dict[str, Any]] = field(default_factory=dict)
    hidden_confirms: dict[str, dict[str, Any]] = field(default_factory=dict)
    activities: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    order_posts: list[dict[str, Any]] = field(default_factory=list)
    close_requests: list[str] = field(default_factory=list)
    logins: int = 0
    price_requests: int = 0
    token: str = ""
    next_id: int = 1000

    def _id(self) -> str:
        self.next_id += 1
        return f"0000{self.next_id}-0001-54c4-0000-0000805600{self.next_id % 100:02d}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def expire_session(self) -> None:
        self.token = "expired"

    # ------------------------------------------------------------------ helpers for tests

    def add_external_position(self, units: int = 5000, price: float = 1.1, sl: float | None = None) -> str:
        return self._open_position("BUY" if units > 0 else "SELL", abs(units), price, sl, None)

    def close_position_at(self, deal_id: str, price: float, source: str = "SL") -> None:
        item = self.positions.pop(deal_id)
        pos = item["position"]
        sign = 1 if pos["direction"] == "BUY" else -1
        pl = (price - pos["level"]) * pos["size"] * sign
        self.nav += pl
        now = utcnow()
        self.activities.append({
            "date": api_ts(now), "dateUTC": api_ts(now), "epic": EPIC, "dealId": self._id(), "source": source,
            "type": "POSITION", "status": "ACCEPTED",
            "details": {"dealReference": f"p_{deal_id}", "direction": "SELL" if sign > 0 else "BUY",
                        "size": pos["size"], "level": price,
                        "actions": [{"actionType": "POSITION_CLOSED", "affectedDealId": deal_id}]},
        })
        self.transactions.append({
            "date": api_ts(now), "dateUtc": api_ts(now), "instrumentName": "EUR/USD", "transactionType": "TRADE",
            "note": "Trade closed", "reference": deal_id[-8:], "size": f"{pl:.2f}", "currency": self.currency,
        })

    def _open_position(self, direction: str, size: float, price: float, sl: float | None, tp: float | None) -> str:
        deal_id = self._id()
        now = utcnow()
        pos: dict[str, Any] = {
            "contractSize": 1, "createdDate": api_ts(now), "createdDateUTC": api_ts(now), "dealId": deal_id,
            "dealReference": f"p_{deal_id}", "size": size, "leverage": 30, "upl": 0.0, "direction": direction,
            "level": price, "currency": self.currency, "guaranteedStop": False,
        }
        if sl is not None:
            pos["stopLevel"] = sl
        if tp is not None:
            pos["profitLevel"] = tp
        self.positions[deal_id] = {"position": pos, "market": {"instrumentName": "EUR/USD", "epic": EPIC,
                                                               "instrumentType": "CURRENCIES", "lotSize": 1,
                                                               "bid": self.bid, "offer": self.ask}}
        self.activities.append({
            "date": api_ts(now), "dateUTC": api_ts(now), "epic": EPIC, "dealId": deal_id, "source": "USER",
            "type": "POSITION", "status": "ACCEPTED",
            "details": {"dealReference": f"p_{deal_id}", "direction": direction, "size": size, "level": price,
                        "stopLevel": sl, "profitLevel": tp,
                        "actions": [{"actionType": "POSITION_OPENED", "affectedDealId": deal_id}]},
        })
        return deal_id

    def _fill(self, body: dict[str, Any]) -> str:
        """Open a position for a POST /positions body; returns the deal reference."""
        buy = body["direction"] == "BUY"
        price = round((self.ask + self.fill_offset) if buy else (self.bid - self.fill_offset), 5)
        deal_id = self._open_position(body["direction"], body["size"], price, body.get("stopLevel"), body.get("profitLevel"))
        ref = f"o_{deal_id}"
        self.confirms[ref] = {
            "date": api_ts(utcnow()), "status": "OPEN", "reason": "SUCCESS", "dealStatus": "ACCEPTED", "epic": EPIC,
            "dealReference": ref, "dealId": f"order-{deal_id}", "affectedDeals": [{"dealId": deal_id, "status": "OPENED"}],
            "level": price, "size": body["size"], "direction": body["direction"], "guaranteedStop": False,
            "trailingStop": False,
        }
        return ref

    def release_confirms(self) -> None:
        self.confirms.update(self.hidden_confirms)
        self.hidden_confirms.clear()

    # ------------------------------------------------------------------ request routing

    def handle(self, req: httpx.Request) -> httpx.Response:
        assert req.headers.get("X-CAP-API-KEY") == API_KEY
        path = req.url.path.removeprefix("/api/v1")
        if path == "/session" and req.method == "POST":
            body = json.loads(req.content)
            if body.get("identifier") != IDENTIFIER or body.get("password") != PASSWORD:
                return httpx.Response(401, json={"errorCode": "error.invalid.details"})
            self.logins += 1
            self.token = f"cst-{self.logins}"
            return httpx.Response(
                200,
                headers={"CST": self.token, "X-SECURITY-TOKEN": f"xst-{self.logins}"},
                json={"accountType": "CFD", "currencyIsoCode": self.currency, "currentAccountId": ACCOUNT_ID,
                      "streamingHost": "wss://stream.test/",
                      "accounts": [{"accountId": ACCOUNT_ID, "accountName": "USD", "preferred": True,
                                    "accountType": "CFD"}]},
            )
        if req.headers.get("CST") != self.token or not self.token:
            return httpx.Response(401, json={"errorCode": "error.invalid.session.token"})

        if path == "/accounts":
            upl = sum(p["position"]["upl"] for p in self.positions.values())
            if self.account_without_balance:
                return httpx.Response(200, json={"accounts": [{"accountId": ACCOUNT_ID, "currency": self.currency}]})
            return httpx.Response(200, json={"accounts": [{
                "accountId": ACCOUNT_ID, "accountName": "USD", "status": "ENABLED", "accountType": "CFD",
                "preferred": True, "currency": self.currency,
                "balance": {"balance": self.nav, "deposit": self.nav - upl, "profitLoss": upl, "available": self.nav},
            }]})
        if path == "/accounts/preferences":
            return httpx.Response(200, json={"hedgingMode": self.hedging_mode, "leverages": {}})
        m = re.fullmatch(r"/markets/([^/]+)", path)
        if m:
            if m.group(1) != EPIC:
                return httpx.Response(404, json={"errorCode": "error.not-found.epic"})
            return httpx.Response(200, json={
                "instrument": {"epic": EPIC, "name": "EUR/USD", "lotSize": 1, "type": "CURRENCIES", "currency": "USD",
                               "marginFactor": 3.33, "marginFactorUnit": "PERCENTAGE"},
                "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 100},
                                 "minStepDistance": {"unit": "POINTS", "value": 0.00001}},
                "snapshot": {"marketStatus": self.market_status, "bid": self.bid, "offer": self.ask,
                             "decimalPlacesFactor": 5, "scalingFactor": 1},
            })
        if path == "/positions" and req.method == "GET":
            return httpx.Response(200, json={"positions": list(self.positions.values())})
        m = re.fullmatch(r"/positions/([^/]+)", path)
        if m and req.method == "GET":
            item = self.positions.get(m.group(1))
            return httpx.Response(200, json=item) if item else httpx.Response(404, json={"errorCode": "error.not-found.dealId"})
        if m and req.method == "DELETE":
            deal_id = m.group(1)
            self.close_requests.append(deal_id)
            if deal_id not in self.positions:
                return httpx.Response(404, json={"errorCode": "error.not-found.dealId"})
            buy = self.positions[deal_id]["position"]["direction"] == "BUY"
            price = self.bid if buy else self.ask
            self.close_position_at(deal_id, price, source="USER")
            ref = f"p_{deal_id}"
            self.confirms[ref] = {"dealStatus": "ACCEPTED", "status": "CLOSED", "reason": "SUCCESS", "dealReference": ref,
                                  "dealId": deal_id, "level": price, "affectedDeals": [{"dealId": deal_id, "status": "FULLY_CLOSED"}]}
            return httpx.Response(200, json={"dealReference": ref})
        if path == "/positions" and req.method == "POST":
            body = json.loads(req.content)
            self.order_posts.append(body)
            mode = self.order_mode
            if mode == "timeout_before":
                raise httpx.ReadTimeout("simulated timeout", request=req)
            if mode == "timeout_after":
                self._fill(body)
                raise httpx.ReadTimeout("simulated timeout after fill", request=req)
            if mode == "reject":
                return httpx.Response(400, json={"errorCode": "error.invalid.size.minvalue: 100"})
            if mode == "http500":
                return httpx.Response(500, json={"errorCode": "error.internal"})
            if mode == "reject_confirm":
                ref = f"o_{self._id()}"
                self.confirms[ref] = {"dealStatus": "REJECTED", "status": "REJECTED", "reason": "INSUFFICIENT_FUNDS",
                                      "dealReference": ref, "epic": EPIC}
                return httpx.Response(200, json={"dealReference": ref})
            ref = self._fill(body)
            if mode == "no_confirm":
                self.hidden_confirms[ref] = self.confirms.pop(ref)
            return httpx.Response(200, json={"dealReference": ref})
        m = re.fullmatch(r"/confirms/([^/]+)", path)
        if m:
            c = self.confirms.get(m.group(1))
            return httpx.Response(200, json=c) if c else httpx.Response(404, json={"errorCode": "error.not-found.dealReference"})
        if path in ("/history/activity", "/history/transactions"):
            start = parse_time(req.url.params["from"])
            end = parse_time(req.url.params["to"])
            assert (end - start).total_seconds() <= 86400, "history range longer than one day"
            if path == "/history/activity":
                assert req.url.params.get("detailed") == "true"
                items = [a for a in self.activities if start <= parse_time(a["dateUTC"]) <= end.replace(microsecond=999999)]
                return httpx.Response(200, json={"activities": items})
            items = [t for t in self.transactions if start <= parse_time(t["dateUtc"]) <= end.replace(microsecond=999999)]
            return httpx.Response(200, json={"transactions": items})
        m = re.fullmatch(r"/prices/([^/]+)", path)
        if m:
            g = RESOLUTION_TO_GRANULARITY[req.url.params["resolution"]]
            count = int(req.url.params["max"])
            assert count <= 1000
            bars = self.candles.get(g, [])
            if "from" in req.url.params:
                lo, hi = parse_time(req.url.params["from"]), parse_time(req.url.params["to"])
                # Like the real API: a range wider than ``max`` bars is refused.
                if (hi - lo).total_seconds() >= count * GRANULARITY_SECONDS[g]:
                    return httpx.Response(400, json={"errorCode": "error.invalid.max.daterange"})
                bars = [b for b in bars if lo <= b.time <= hi]
                if not bars:
                    return httpx.Response(404, json={"errorCode": "error.prices.not-found"})
            self.price_requests += 1
            bars = bars[-count:]
            half = 0.00005
            return httpx.Response(200, json={"prices": [
                {"snapshotTime": b.time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "snapshotTimeUTC": b.time.strftime("%Y-%m-%dT%H:%M:%S"),
                 "openPrice": {"bid": b.open - half, "ask": b.open + half},
                 "highPrice": {"bid": b.high - half, "ask": b.high + half},
                 "lowPrice": {"bid": b.low - half, "ask": b.low + half},
                 "closePrice": {"bid": b.close - half, "ask": b.close + half},
                 "lastTradedVolume": b.volume} for b in bars], "instrumentType": "CURRENCIES"})
        return httpx.Response(404, json={"errorCode": f"fake broker: no route for {req.method} {path}"})


def make_client(broker: FakeBroker, **kw):
    from app.broker.capital import CapitalClient

    return CapitalClient("https://api.test", API_KEY, IDENTIFIER, PASSWORD, transport=broker.transport(),
                         max_get_retries=0, **kw)
