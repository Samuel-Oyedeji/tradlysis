"""Thin async client for the OANDA v20 REST and streaming APIs.

Reference: https://developer.oanda.com/rest-live-v20/introduction/

Design notes:
  * Read-only GET calls are retried on transport errors, 429 and 5xx (they are idempotent).
  * Order submission is **never** retried here. A timeout on an order request means the
    outcome is unknown; the executor resolves it by looking the order up by client ID.
  * The broker is the source of truth; this client does no caching of account state.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OandaError(Exception):
    """The broker answered with an error status."""

    def __init__(self, status_code: int, body: Any, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        err = ""
        if isinstance(body, dict):
            err = body.get("errorMessage") or body.get("errorCode") or ""
        super().__init__(message or f"OANDA HTTP {status_code}: {err or body}")


class OandaTransportError(Exception):
    """The request did not produce a response (timeout, connection reset, ...)."""


@dataclass(frozen=True)
class InstrumentInfo:
    name: str
    pip_location: int
    display_precision: int
    trade_units_precision: int
    minimum_trade_size: float
    margin_rate: float

    @property
    def pip_size(self) -> float:
        return 10.0 ** self.pip_location

    def format_price(self, price: float) -> str:
        return f"{price:.{self.display_precision}f}"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> InstrumentInfo:
        return cls(
            name=data["name"],
            pip_location=int(data["pipLocation"]),
            display_precision=int(data["displayPrecision"]),
            trade_units_precision=int(data.get("tradeUnitsPrecision", 0)),
            minimum_trade_size=float(data.get("minimumTradeSize", 1)),
            margin_rate=float(data.get("marginRate", 0.05)),
        )


# Fallback when instrument details are unavailable (unit tests, offline tools).
DEFAULT_INSTRUMENTS = {
    "EUR_USD": InstrumentInfo("EUR_USD", -4, 5, 0, 1.0, 0.0333),
}


class OandaClient:
    def __init__(
        self,
        rest_url: str,
        stream_url: str,
        token: str,
        account_id: str,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        stream_transport: httpx.AsyncBaseTransport | None = None,
        max_get_retries: int = 3,
    ) -> None:
        self.account_id = account_id
        self.max_get_retries = max_get_retries
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept-Datetime-Format": "RFC3339",
        }
        self._rest = httpx.AsyncClient(
            base_url=rest_url, headers=headers, timeout=timeout, transport=transport
        )
        # Streams stay open indefinitely; OANDA sends a heartbeat every ~5s, so a 30s read
        # timeout reliably detects a dead connection.
        self._stream = httpx.AsyncClient(
            base_url=stream_url,
            headers=headers,
            timeout=httpx.Timeout(connect=timeout, read=30.0, write=timeout, pool=timeout),
            transport=stream_transport,
        )

    async def aclose(self) -> None:
        await self._rest.aclose()
        await self._stream.aclose()

    # ---------------------------------------------------------------- low level

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.max_get_retries + 1):
            try:
                resp = await self._rest.get(path, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = OandaTransportError(f"GET {path}: {exc!r}")
            else:
                if resp.status_code == 200:
                    return resp.json()
                body = _safe_json(resp)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = OandaError(resp.status_code, body)
                else:
                    raise OandaError(resp.status_code, body)
            if attempt < self.max_get_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        assert last_exc is not None
        raise last_exc

    async def _send(self, method: str, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Non-idempotent request. Returns (status, json) for any HTTP response."""
        try:
            resp = await self._rest.request(method, path, json=body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise OandaTransportError(f"{method} {path}: {exc!r}") from exc
        return resp.status_code, _safe_json(resp)

    def _acct(self, suffix: str = "") -> str:
        return f"/v3/accounts/{self.account_id}{suffix}"

    # ---------------------------------------------------------------- account

    async def get_accounts(self) -> list[dict[str, Any]]:
        return (await self._get("/v3/accounts")).get("accounts", [])

    async def get_account_summary(self) -> dict[str, Any]:
        return (await self._get(self._acct("/summary")))["account"]

    async def get_instrument(self, instrument: str) -> InstrumentInfo:
        data = await self._get(self._acct("/instruments"), {"instruments": instrument})
        items = data.get("instruments", [])
        if not items:
            raise OandaError(404, data, f"Instrument {instrument} not available on account")
        return InstrumentInfo.from_api(items[0])

    async def get_pricing(
        self, instruments: list[str], include_home_conversions: bool = True
    ) -> dict[str, Any]:
        params = {
            "instruments": ",".join(instruments),
            "includeHomeConversions": "true" if include_home_conversions else "false",
        }
        return await self._get(self._acct("/pricing"), params)

    # ---------------------------------------------------------------- market data

    async def get_candles(
        self,
        instrument: str,
        granularity: str,
        count: int = 500,
        price: str = "MBA",
    ) -> list[dict[str, Any]]:
        params = {"granularity": granularity, "count": min(count, 5000), "price": price}
        data = await self._get(f"/v3/instruments/{instrument}/candles", params)
        return data.get("candles", [])

    async def stream_prices(self, instruments: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield PRICE and HEARTBEAT messages until the connection drops."""
        params = {"instruments": ",".join(instruments), "snapshot": "true"}
        try:
            async with self._stream.stream(
                "GET", self._acct("/pricing/stream"), params=params
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    raise OandaError(resp.status_code, _parse_json_bytes(body))
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("Malformed stream line: %r", line[:200])
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise OandaTransportError(f"price stream: {exc!r}") from exc

    # ---------------------------------------------------------------- orders & trades

    async def create_order(self, order: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await self._send("POST", self._acct("/orders"), {"order": order})

    async def get_order(self, order_specifier: str) -> dict[str, Any] | None:
        """Look up an order by ID or ``@clientOrderID``. Returns None if it does not exist."""
        try:
            return (await self._get(self._acct(f"/orders/{order_specifier}")))["order"]
        except OandaError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def get_open_trades(self) -> list[dict[str, Any]]:
        return (await self._get(self._acct("/openTrades"))).get("trades", [])

    async def get_trade(self, trade_specifier: str) -> dict[str, Any] | None:
        try:
            return (await self._get(self._acct(f"/trades/{trade_specifier}")))["trade"]
        except OandaError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def close_trade(self, trade_specifier: str, units: str = "ALL") -> tuple[int, dict[str, Any]]:
        return await self._send("PUT", self._acct(f"/trades/{trade_specifier}/close"), {"units": units})

    async def get_transactions_since(self, transaction_id: str) -> dict[str, Any]:
        return await self._get(self._acct("/transactions/sinceid"), {"id": transaction_id})


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"raw": resp.text[:2000]}


def _parse_json_bytes(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"raw": body[:2000].decode(errors="replace")}
