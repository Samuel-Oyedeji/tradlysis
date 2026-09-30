"""In-memory fake of the OANDA v20 endpoints the system uses (served via httpx.MockTransport)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.market_data.candles import Bar

ACCOUNT_ID = "101-001-0000000-001"


@dataclass
class FakeBroker:
    nav: float = 100000.0
    currency: str = "USD"
    bid: float = 1.1025
    ask: float = 1.1026
    # behaviour switches for POST /orders
    order_mode: str = "fill"  # fill | reject | cancel | timeout_before | timeout_after | http500
    candles: dict[str, list[Bar]] = field(default_factory=dict)
    open_trades: dict[str, dict[str, Any]] = field(default_factory=dict)
    closed_trades: dict[str, dict[str, Any]] = field(default_factory=dict)
    orders_by_client_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    order_posts: list[dict[str, Any]] = field(default_factory=list)
    next_id: int = 1000

    def _id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    # ------------------------------------------------------------------ helpers for tests

    def add_external_trade(self, units: int = 5000, price: float = 1.1, sl: float | None = None) -> str:
        tid = self._id()
        self.open_trades[tid] = self._trade(tid, units, price, sl, None, client_id=None)
        return tid

    def close_trade_at(self, trade_id: str, price: float, reason: str = "STOP_LOSS_ORDER") -> None:
        t = self.open_trades.pop(trade_id)
        units = int(t["currentUnits"])
        pl = (price - float(t["price"])) * units
        t.update(
            state="CLOSED", currentUnits="0", averageClosePrice=f"{price:.5f}", realizedPL=f"{pl:.4f}",
            closeTime="2026-09-29T12:00:00.000000000Z", financing="0.0000",
        )
        if reason == "STOP_LOSS_ORDER" and t.get("stopLossOrder"):
            t["stopLossOrder"]["state"] = "FILLED"
        if reason == "TAKE_PROFIT_ORDER" and t.get("takeProfitOrder"):
            t["takeProfitOrder"]["state"] = "FILLED"
        self.closed_trades[trade_id] = t
        self.nav += pl
        self.transactions.append(
            {"id": self._id(), "type": "ORDER_FILL", "time": "2026-09-29T12:00:00Z", "reason": reason,
             "pl": f"{pl:.4f}", "tradesClosed": [{"tradeID": trade_id}], "accountID": ACCOUNT_ID}
        )

    def _trade(self, tid, units, price, sl, tp, client_id) -> dict[str, Any]:
        t: dict[str, Any] = {
            "id": tid, "instrument": "EUR_USD", "price": f"{price:.5f}", "openTime": "2026-09-29T10:00:00.000000000Z",
            "initialUnits": str(units), "currentUnits": str(units), "state": "OPEN", "realizedPL": "0.0000",
            "unrealizedPL": "0.0000",
        }
        if client_id:
            t["clientExtensions"] = {"id": client_id, "tag": "tradlysis"}
        if sl is not None:
            t["stopLossOrder"] = {"id": self._id(), "price": f"{sl:.5f}", "state": "PENDING"}
        if tp is not None:
            t["takeProfitOrder"] = {"id": self._id(), "price": f"{tp:.5f}", "state": "PENDING"}
        return t

    def _fill(self, order: dict[str, Any]) -> dict[str, Any]:
        units = int(order["units"])
        price = self.ask if units > 0 else self.bid
        client_id = order["clientExtensions"]["id"]
        tid = self._id()
        sl = float(order["stopLossOnFill"]["price"])
        tp = float(order["takeProfitOnFill"]["price"])
        self.open_trades[tid] = self._trade(tid, units, price, sl, tp, client_id)
        order_id = self._id()
        fill_id = self._id()
        self.orders_by_client_id[client_id] = {
            "id": order_id, "state": "FILLED", "fillingTransactionID": fill_id, "tradeOpenedID": tid,
            "filledTime": "2026-09-29T10:00:01.000000000Z", "clientExtensions": order["clientExtensions"],
        }
        fill = {"id": fill_id, "type": "ORDER_FILL", "time": "2026-09-29T10:00:01.000000000Z", "orderID": order_id,
                "price": f"{price:.5f}", "units": str(units), "reason": "MARKET_ORDER",
                "tradeOpened": {"tradeID": tid, "units": str(units), "price": f"{price:.5f}"}, "accountID": ACCOUNT_ID}
        self.transactions.append(fill)
        return {"orderCreateTransaction": {"id": order_id, "type": "MARKET_ORDER"}, "orderFillTransaction": fill,
                "lastTransactionID": fill_id}

    # ------------------------------------------------------------------ request routing

    def handle(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        acct = f"/v3/accounts/{ACCOUNT_ID}"
        if path == f"{acct}/summary":
            return httpx.Response(200, json={"account": {
                "id": ACCOUNT_ID, "currency": self.currency, "balance": f"{self.nav:.4f}", "NAV": f"{self.nav:.4f}",
                "unrealizedPL": "0.0000", "marginUsed": "0.0000", "marginAvailable": f"{self.nav:.4f}",
                "openTradeCount": len(self.open_trades), "openPositionCount": 1 if self.open_trades else 0,
                "pendingOrderCount": 0, "lastTransactionID": self.transactions[-1]["id"] if self.transactions else "1",
            }})
        if path == f"{acct}/instruments":
            return httpx.Response(200, json={"instruments": [{
                "name": "EUR_USD", "pipLocation": -4, "displayPrecision": 5, "tradeUnitsPrecision": 0,
                "minimumTradeSize": "1", "marginRate": "0.0333"}]})
        if path == f"{acct}/pricing":
            return httpx.Response(200, json={"prices": [], "homeConversions": []})
        if path == f"{acct}/openTrades":
            return httpx.Response(200, json={"trades": list(self.open_trades.values())})
        m = re.fullmatch(rf"{acct}/trades/([^/]+)", path)
        if m and req.method == "GET":
            t = self.open_trades.get(m.group(1)) or self.closed_trades.get(m.group(1))
            return httpx.Response(200, json={"trade": t}) if t else httpx.Response(404, json={"errorCode": "NO_SUCH_TRADE"})
        m = re.fullmatch(rf"{acct}/trades/([^/]+)/close", path)
        if m and req.method == "PUT":
            tid = m.group(1)
            if tid not in self.open_trades:
                return httpx.Response(404, json={"errorCode": "NO_SUCH_TRADE"})
            price = self.bid
            self.close_trade_at(tid, price, reason="MARKET_ORDER_TRADE_CLOSE")
            return httpx.Response(200, json={"orderFillTransaction": {"id": self._id(), "price": f"{price:.5f}"}})
        m = re.fullmatch(rf"{acct}/orders/@(.+)", path)
        if m:
            o = self.orders_by_client_id.get(m.group(1))
            return httpx.Response(200, json={"order": o}) if o else httpx.Response(404, json={"errorCode": "ORDER_DOESNT_EXIST"})
        if path == f"{acct}/orders" and req.method == "POST":
            order = json.loads(req.content)["order"]
            self.order_posts.append(order)
            mode = self.order_mode
            if mode == "timeout_before":
                self.order_mode = "fill"  # the retry succeeds
                raise httpx.ReadTimeout("simulated timeout", request=req)
            if mode == "timeout_after":
                self._fill(order)
                self.order_mode = "fill"
                raise httpx.ReadTimeout("simulated timeout after fill", request=req)
            if mode == "reject":
                return httpx.Response(400, json={"orderRejectTransaction": {"rejectReason": "INSUFFICIENT_MARGIN"},
                                                 "errorCode": "INSUFFICIENT_MARGIN"})
            if mode == "cancel":
                return httpx.Response(201, json={"orderCreateTransaction": {"id": self._id()},
                                                 "orderCancelTransaction": {"id": self._id(), "reason": "BOUNDS_VIOLATION"}})
            if mode == "http500":
                return httpx.Response(500, json={"errorMessage": "internal"})
            return httpx.Response(201, json=self._fill(order))
        if path == f"{acct}/transactions/sinceid":
            since = int(req.url.params["id"])
            txs = [t for t in self.transactions if int(t["id"]) > since]
            return httpx.Response(200, json={"transactions": txs,
                                             "lastTransactionID": txs[-1]["id"] if txs else str(since)})
        m = re.fullmatch(r"/v3/instruments/([^/]+)/candles", path)
        if m:
            g = req.url.params["granularity"]
            count = int(req.url.params["count"])
            bars = self.candles.get(g, [])[-count:]
            return httpx.Response(200, json={"instrument": m.group(1), "granularity": g, "candles": [
                {"time": b.time.isoformat().replace("+00:00", "Z"), "volume": b.volume, "complete": b.complete,
                 "mid": {"o": str(b.open), "h": str(b.high), "l": str(b.low), "c": str(b.close)}} for b in bars]})
        return httpx.Response(404, json={"errorMessage": f"fake broker: no route for {req.method} {path}"})
