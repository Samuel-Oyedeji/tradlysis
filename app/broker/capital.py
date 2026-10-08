"""Async client for the Capital.com Public API (REST + WebSocket streaming).

Reference: https://open-api.capital.com/

Design notes:
  * Authentication is a session. ``POST /session`` with the API key (``X-CAP-API-KEY``), the
    login identifier and the API key's custom password returns ``CST`` and
    ``X-SECURITY-TOKEN`` headers. Sessions expire after 10 minutes of inactivity; the client
    logs in lazily and logs in again (once) when the API answers 401.
  * Read-only GET calls are retried on transport errors, 429 and 5xx (they are idempotent).
  * Opening or closing a position is **never** retried here. A timeout means the outcome is
    unknown; the executor resolves it from the broker's confirmations, positions and activity.
  * The broker is the source of truth; this client does no caching of account state.
  * Capital.com names markets by "epic" (``EURUSD``). The rest of the system uses the
    instrument name (``EUR_USD``); the client translates in both directions. One client (one
    session, one account) serves every market the experiments trade: markets are registered
    with :meth:`CapitalClient.register_market`, and market calls take an ``instrument``
    (default: the first one registered).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.broker.types import AccountState, BrokerPosition, InstrumentInfo, Quote
from app.market_data.candles import Bar
from app.market_data.timeutil import GRANULARITY_SECONDS, parse_time, utcnow

log = logging.getLogger(__name__)

DEFAULT_STREAM_URL = "wss://api-streaming-capital.backend-capital.com/connect"

# Our granularity names -> Capital.com price resolutions. Months ("M") are built from daily bars.
RESOLUTIONS = {
    "M1": "MINUTE",
    "M5": "MINUTE_5",
    "M15": "MINUTE_15",
    "M30": "MINUTE_30",
    "H1": "HOUR",
    "H4": "HOUR_4",
    "D": "DAY",
    "W": "WEEK",
}
MAX_PRICE_POINTS = 1000  # per request, documented maximum
# History endpoints reject ranges longer than one day.
HISTORY_WINDOW = timedelta(days=1) - timedelta(seconds=1)
# The streaming session must be pinged at least every 10 minutes; the reply doubles as a heartbeat.
WS_PING_SECONDS = 60.0
WS_IDLE_TIMEOUT_SECONDS = 150.0

REJECTED_STATUSES = {"REJECTED", "MODIFY_REJECT", "CANCEL_REJECT"}
CLOSE_SOURCES = {"SL", "TP", "CLOSE_OUT"}


class CapitalError(Exception):
    """The broker answered with an error status."""

    def __init__(self, status_code: int, body: Any, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.error_code = ""
        if isinstance(body, dict):
            self.error_code = str(body.get("errorCode") or body.get("message") or "")
        super().__init__(message or f"Capital.com HTTP {status_code}: {self.error_code or body}")


class CapitalDataError(CapitalError):
    """The broker answered, but the data is unusable (e.g. an account without a balance)."""


class CapitalTransportError(Exception):
    """The request did not produce a response (timeout, connection reset, ...)."""


WsConnect = Callable[[str], AbstractAsyncContextManager[Any]]


def _default_ws_connect(url: str) -> AbstractAsyncContextManager[Any]:
    from websockets.asyncio.client import connect

    # Keep-alive is done with the API's own ping message (see stream_quotes), not protocol pings.
    return connect(url, open_timeout=15, ping_interval=None, close_timeout=5)


class CapitalClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        identifier: str,
        password: str,
        *,
        account_id: str = "",
        instrument: str = "EUR_USD",
        epic: str = "",
        stream_url: str = "",
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
        ws_connect: WsConnect | None = None,
        max_get_retries: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.instrument = instrument
        self.epic = epic or instrument.replace("_", "")
        # instrument -> epic, for every market in use; the first one is the default.
        self.markets: dict[str, str] = {instrument: self.epic}
        # The configured account; after login this is the active account.
        self.account_id = account_id
        self.max_get_retries = max_get_retries
        self._identifier = identifier
        self._password = password
        self._stream_url_override = stream_url
        self.stream_url = stream_url or DEFAULT_STREAM_URL
        self._ws_connect = ws_connect or _default_ws_connect
        self._http = httpx.AsyncClient(
            base_url=f"{self.base_url}/api/v1",
            headers={"X-CAP-API-KEY": api_key, "Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
            transport=transport,
        )
        self._cst: str | None = None
        self._security_token: str | None = None
        self._session_lock = asyncio.Lock()
        self.session_info: dict[str, Any] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------------------------------------------------------------- session

    async def login(self) -> dict[str, Any]:
        """Create a session and switch to the configured account if it is not the active one."""
        body = {"identifier": self._identifier, "password": self._password, "encryptedPassword": False}
        try:
            resp = await self._http.post("/session", json=body)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise CapitalTransportError(f"POST /session: {exc!r}") from exc
        data = _safe_json(resp)
        if resp.status_code != 200:
            err = data.get("errorCode") or data
            raise CapitalError(resp.status_code, data, f"Capital.com login failed (HTTP {resp.status_code}): {err}")
        cst, token = resp.headers.get("CST"), resp.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            raise CapitalError(resp.status_code, data, "Capital.com login response did not include session tokens")
        self._cst, self._security_token = cst, token
        self.session_info = data

        current = str(data.get("currentAccountId") or "")
        if self.account_id and self.account_id != current:
            known = [str(a.get("accountId")) for a in data.get("accounts", [])]
            if self.account_id not in known:
                raise CapitalError(400, data, f"account {self.account_id} not found for this login (available: {known})")
            resp = await self._http.put("/session", json={"accountId": self.account_id}, headers=self._auth_headers())
            if resp.status_code != 200:
                raise CapitalError(resp.status_code, _safe_json(resp), f"could not switch to account {self.account_id}")
        elif not self.account_id:
            self.account_id = current

        streaming_host = data.get("streamingHost")
        if streaming_host and not self._stream_url_override:
            self.stream_url = str(streaming_host).rstrip("/") + "/connect"
        log.info("Capital.com session created for account %s", self.account_id)
        return data

    def _auth_headers(self) -> dict[str, str]:
        return {"CST": self._cst or "", "X-SECURITY-TOKEN": self._security_token or ""}

    async def _ensure_session(self) -> None:
        if self._cst:
            return
        async with self._session_lock:
            if not self._cst:
                await self.login()

    async def _relogin(self, stale_cst: str | None) -> None:
        async with self._session_lock:
            if self._cst == stale_cst:  # not already refreshed by another task
                self._cst = None
                await self.login()

    # ---------------------------------------------------------------- low level

    async def _request(
        self, method: str, path: str, *, params: dict[str, Any] | None = None, body: Any = None
    ) -> httpx.Response:
        await self._ensure_session()
        for attempt in (0, 1):
            cst = self._cst
            try:
                resp = await self._http.request(method, path, params=params, json=body, headers=self._auth_headers())
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                raise CapitalTransportError(f"{method} {path}: {exc!r}") from exc
            if resp.status_code == 401 and attempt == 0:
                # Expired or replaced session: the request was not processed. Log in and retry once.
                await self._relogin(cst)
                continue
            return resp
        raise AssertionError("unreachable")

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        delay = 1.0
        last_exc: Exception | None = None
        for attempt in range(self.max_get_retries + 1):
            try:
                resp = await self._request("GET", path, params=params)
            except CapitalTransportError as exc:
                last_exc = exc
            else:
                if resp.status_code == 200:
                    return _safe_json(resp)
                body = _safe_json(resp)
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_exc = CapitalError(resp.status_code, body)
                else:
                    raise CapitalError(resp.status_code, body)
            if attempt < self.max_get_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        assert last_exc is not None
        raise last_exc

    async def _send(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
        """Non-idempotent request. Returns (status, json) for any HTTP response."""
        resp = await self._request(method, path, body=body)
        return resp.status_code, _safe_json(resp)

    # ---------------------------------------------------------------- account

    async def get_accounts(self) -> list[dict[str, Any]]:
        return (await self._get("/accounts")).get("accounts", [])

    async def get_account(self) -> AccountState:
        accounts = await self.get_accounts()  # logs in first, which resolves self.account_id
        for a in accounts:
            if str(a.get("accountId")) == self.account_id:
                return account_state_from_api(a)
        raise CapitalError(404, {"accounts": accounts}, f"active account {self.account_id} not in the account list")

    async def get_preferences(self) -> dict[str, Any]:
        return await self._get("/accounts/preferences")

    # ---------------------------------------------------------------- markets

    def register_market(self, instrument: str, epic: str = "") -> str:
        """Add a market the system trades or watches; returns its epic."""
        epic = epic or instrument.replace("_", "")
        existing = self.markets.get(instrument)
        if existing and existing != epic:
            raise ValueError(f"{instrument} is already mapped to {existing}, not {epic}")
        self.markets[instrument] = epic
        return epic

    def epic_for(self, instrument: str | None = None) -> str:
        if instrument is None:
            return self.epic
        epic = self.markets.get(instrument)
        if epic is None:
            raise KeyError(f"market {instrument} is not registered")
        return epic

    def instrument_for_epic(self, epic: str | None) -> str:
        for instrument, e in self.markets.items():
            if e == epic:
                return instrument
        return str(epic or "")

    async def get_market(self, epic: str | None = None) -> dict[str, Any]:
        return await self._get(f"/markets/{epic or self.epic}")

    async def get_instrument(self, instrument: str | None = None) -> InstrumentInfo:
        name = instrument or self.instrument
        return instrument_from_market(name, await self.get_market(self.epic_for(name)))

    async def get_market_status(self, instrument: str | None = None) -> str:
        return str(((await self.get_market(self.epic_for(instrument))).get("snapshot") or {}).get("marketStatus", ""))

    async def get_mid_price(self, epic: str) -> float | None:
        """Current mid price of any market, or None if it does not exist."""
        try:
            snap = (await self.get_market(epic)).get("snapshot") or {}
        except CapitalError as exc:
            if exc.status_code in (400, 404):
                return None
            raise
        bid, offer = _f(snap.get("bid")), _f(snap.get("offer"))
        return (bid + offer) / 2 if bid and offer else None

    async def get_conversion_rate(self, currency: str, account_currency: str) -> float | None:
        """Account-currency value of one unit of ``currency``, from the direct or inverse FX market."""
        if currency == account_currency:
            return 1.0
        direct = await self.get_mid_price(f"{currency}{account_currency}")
        if direct:
            return direct
        inverse = await self.get_mid_price(f"{account_currency}{currency}")
        return 1.0 / inverse if inverse else None

    async def get_candles(self, granularity: str, count: int = 500, instrument: str | None = None) -> list[Bar]:
        now = utcnow()
        epic = self.epic_for(instrument)
        if granularity == "M":
            days = await self._get_prices(epic, "DAY", min(MAX_PRICE_POINTS, (count + 1) * 31 + 5))
            return monthly_bars(parse_price_bars(days, "D", now), now)
        if granularity not in RESOLUTIONS:
            raise ValueError(f"unsupported granularity {granularity}")
        raw = await self._get_prices(epic, RESOLUTIONS[granularity], min(count, MAX_PRICE_POINTS))
        return parse_price_bars(raw, granularity, now)

    async def get_candles_between(
        self, granularity: str, start: datetime, end: datetime, instrument: str | None = None
    ) -> list[Bar]:
        """Candles opening in [start, end), fetched in windows of at most MAX_PRICE_POINTS bars
        (the API refuses wider ranges). Windows the broker has no prices for (weekends, before its
        history begins) are skipped. Used by the backtester."""
        now = utcnow()
        epic = self.epic_for(instrument)
        if granularity == "M":
            return monthly_bars(await self.get_candles_between("D", start, end, instrument), now)
        if granularity not in RESOLUTIONS:
            raise ValueError(f"unsupported granularity {granularity}")
        step = timedelta(seconds=GRANULARITY_SECONDS[granularity] * MAX_PRICE_POINTS)
        out: dict[datetime, Bar] = {}
        t = start
        while t < end:
            stop = min(t + step, end)
            params = {
                "resolution": RESOLUTIONS[granularity], "max": MAX_PRICE_POINTS,
                "from": api_time(t), "to": api_time(stop - timedelta(seconds=1)),
            }
            try:
                data = await self._get(f"/prices/{epic}", params)
            except CapitalError as exc:
                if exc.status_code not in (400, 404):
                    raise
                data = {}  # no prices in this window
            for b in parse_price_bars(data.get("prices", []), granularity, now):
                if start <= b.time < end:
                    out[b.time] = b
            t = stop
        return [out[k] for k in sorted(out)]

    async def _get_prices(self, epic: str, resolution: str, max_points: int) -> list[dict[str, Any]]:
        data = await self._get(f"/prices/{epic}", {"resolution": resolution, "max": max_points})
        return data.get("prices", [])

    # ---------------------------------------------------------------- positions

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._get("/positions")
        return [self._position(p) for p in data.get("positions", [])]

    async def get_position(self, deal_id: str) -> BrokerPosition | None:
        try:
            return self._position(await self._get(f"/positions/{deal_id}"))
        except CapitalError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def open_position(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return await self._send("POST", "/positions", body)

    async def close_position(self, deal_id: str) -> tuple[int, dict[str, Any]]:
        return await self._send("DELETE", f"/positions/{deal_id}")

    async def get_confirmation(self, deal_reference: str) -> dict[str, Any] | None:
        """Deal confirmation for a deal reference. Returns None if the broker does not know it (yet)."""
        try:
            return await self._get(f"/confirms/{deal_reference}")
        except CapitalError as exc:
            if exc.status_code == 404:
                return None
            raise

    def _position(self, item: dict[str, Any]) -> BrokerPosition:
        pos = item.get("position") or {}
        market = item.get("market") or {}
        direction = str(pos.get("direction", "BUY"))
        size = int(round(float(pos.get("size", 0))))
        return BrokerPosition(
            deal_id=str(pos["dealId"]),
            instrument=self.instrument_for_epic(market.get("epic")),
            direction=direction,
            units=size if direction == "BUY" else -size,
            open_price=float(pos["level"]),
            open_time=parse_time(pos.get("createdDateUTC") or pos["createdDate"]),
            stop_loss=_f(pos.get("stopLevel")),
            take_profit=_f(pos.get("profitLevel")),
            unrealized_pl=Decimal(str(pos.get("upl", "0"))),
            raw=item,
        )

    # ---------------------------------------------------------------- history

    async def get_activity(
        self,
        start: datetime,
        end: datetime | None = None,
        *,
        market_only: bool = True,
        instrument: str | None = None,
    ) -> list[dict[str, Any]]:
        """Detailed account activity between start and end (clamped to the API's one-day window).

        ``market_only``: only activity on ``instrument`` if given, else on every registered market
        (one market is filtered by the API, several are filtered here).
        """
        end = end or utcnow()
        start = max(start, end - HISTORY_WINDOW)
        params: dict[str, Any] = {"from": api_time(start), "to": api_time(end), "detailed": "true"}
        epics = {self.epic_for(instrument)} if instrument else set(self.markets.values())
        if market_only and len(epics) == 1:
            params["filter"] = f"epic=={next(iter(epics))}"
        activities = (await self._get("/history/activity", params)).get("activities", [])
        if market_only and len(epics) > 1:
            activities = [a for a in activities if a.get("epic") in epics]
        return activities

    async def get_transactions(self, start: datetime, end: datetime | None = None) -> list[dict[str, Any]]:
        end = end or utcnow()
        start = max(start, end - HISTORY_WINDOW)
        params = {"from": api_time(start), "to": api_time(end)}
        return (await self._get("/history/transactions", params)).get("transactions", [])

    async def find_close_activity(self, deal_id: str, opened_at: datetime, max_days: int = 14) -> dict[str, Any] | None:
        """Find the activity that closed a position, searching back from now one day at a time."""
        end = utcnow()
        floor = max(opened_at - timedelta(minutes=1), end - timedelta(days=max_days))
        while end > floor:
            start = max(end - HISTORY_WINDOW, floor)
            activities = await self.get_activity(start, end)
            for a in sorted(activities, key=lambda x: str(x.get("dateUTC", "")), reverse=True):
                if is_close_activity(a, deal_id, opened_at):
                    return a
            end = start
        return None

    # ---------------------------------------------------------------- streaming

    def _ws_message(self, destination: str, correlation_id: str, payload: dict[str, Any] | None = None) -> str:
        msg: dict[str, Any] = {
            "destination": destination,
            "correlationId": correlation_id,
            "cst": self._cst,
            "securityToken": self._security_token,
        }
        if payload is not None:
            msg["payload"] = payload
        return json.dumps(msg)

    async def stream_quotes(self) -> AsyncIterator[Quote | None]:
        """Yield live quotes for every registered market until the connection drops.

        ``None`` is yielded as a heartbeat (subscription confirmed, ping answered).
        """
        await self._ensure_session()
        try:
            async with self._ws_connect(self.stream_url) as ws:
                epics = list(dict.fromkeys(self.markets.values()))
                await ws.send(self._ws_message("marketData.subscribe", "subscribe", {"epics": epics}))
                last_ping = last_msg = time.monotonic()
                seq = 0
                while True:
                    wait = max(0.05, WS_PING_SECONDS - (time.monotonic() - last_ping))
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=wait)
                    except TimeoutError:
                        if time.monotonic() - last_msg > WS_IDLE_TIMEOUT_SECONDS:
                            raise CapitalTransportError("price stream silent; reconnecting") from None
                        seq += 1
                        await ws.send(self._ws_message("ping", f"ping-{seq}"))
                        last_ping = time.monotonic()
                        continue
                    last_msg = time.monotonic()
                    try:
                        msg = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        log.warning("Malformed stream message: %r", str(raw)[:200])
                        continue
                    destination = msg.get("destination")
                    if msg.get("status") not in (None, "OK"):
                        if "session" in json.dumps(msg.get("payload", "")).lower():
                            self._cst = None  # force a fresh login before reconnecting
                        raise CapitalError(0, msg, f"stream error on {destination}: {msg.get('payload')}")
                    if destination == "quote":
                        quote = self._quote(msg.get("payload") or {})
                        if quote is not None:
                            yield quote
                    elif destination == "marketData.subscribe":
                        subs = (msg.get("payload") or {}).get("subscriptions") or {}
                        failed = [e for e in epics if subs.get(e) != "PROCESSED"]
                        if failed:
                            raise CapitalError(0, msg, f"subscription to {', '.join(failed)} failed: {subs}")
                        yield None
                    elif destination == "ping":
                        yield None
        except (CapitalError, CapitalTransportError):
            raise
        except (OSError, TimeoutError) as exc:
            raise CapitalTransportError(f"price stream: {exc!r}") from exc
        except Exception as exc:  # websockets.exceptions.* (connection closed, handshake errors)
            if type(exc).__module__.startswith("websockets"):
                raise CapitalTransportError(f"price stream: {exc!r}") from exc
            raise

    def _quote(self, p: dict[str, Any]) -> Quote | None:
        if p.get("epic") not in self.markets.values() or p.get("bid") is None or p.get("ofr") is None:
            return None
        ts = p.get("timestamp")
        t = datetime.fromtimestamp(int(ts) / 1000, tz=UTC) if ts else utcnow()
        return Quote(self.instrument_for_epic(p["epic"]), t, float(p["bid"]), float(p["ofr"]))


# ---------------------------------------------------------------------- parsing helpers


def account_state_from_api(a: dict[str, Any]) -> AccountState:
    """Map an entry of ``GET /accounts``.

    Capital.com's ``balance`` object: ``balance`` = equity (deposit + open P/L), ``deposit`` =
    cash balance, ``profitLoss`` = open P/L, ``available`` = funds available for new positions.
    """
    bal = a.get("balance")
    if not isinstance(bal, dict) or bal.get("balance") is None:
        raise CapitalDataError(0, a, f"account {a.get('accountId')} was returned without a balance")
    try:
        equity = Decimal(str(bal["balance"]))
        available = Decimal(str(bal.get("available", "0")))
        Decimal(str(bal.get("deposit", equity)))
        Decimal(str(bal.get("profitLoss", "0")))
    except ArithmeticError as exc:  # decimal.InvalidOperation
        raise CapitalDataError(0, a, f"account {a.get('accountId')} has a non-numeric balance") from exc
    return AccountState(
        account_id=str(a.get("accountId", "")),
        currency=str(a.get("currency", "")),
        balance=Decimal(str(bal.get("deposit", equity))),
        nav=equity,
        unrealized_pl=Decimal(str(bal.get("profitLoss", "0"))),
        margin_used=max(equity - available, Decimal(0)),
        margin_available=available,
        raw=a,
    )


def instrument_from_market(name: str, data: dict[str, Any]) -> InstrumentInfo:
    """Map ``GET /markets/{epic}``."""
    inst = data.get("instrument") or {}
    rules = data.get("dealingRules") or {}
    snap = data.get("snapshot") or {}
    decimals = int(snap.get("decimalPlacesFactor") or 5)
    # FX quotes carry one digit beyond the pip: EUR/USD 1.10255 -> pip 0.0001, USD/JPY 150.255 -> pip 0.01.
    pip_location = -(decimals - 1) if inst.get("type") == "CURRENCIES" else -decimals
    min_size = float((rules.get("minDealSize") or {}).get("value") or 1)
    increment = (rules.get("minSizeIncrement") or {}).get("value")
    margin = inst.get("marginFactor")
    if margin is not None and inst.get("marginFactorUnit", "PERCENTAGE") == "PERCENTAGE":
        margin_rate = float(margin) / 100
    else:
        margin_rate = 0.05  # unknown unit: assume 1:20, stricter than the usual retail FX 1:30
    return InstrumentInfo(
        name=name,
        pip_location=pip_location,
        display_precision=decimals,
        trade_units_precision=_decimal_places(increment if increment else min_size),
        minimum_trade_size=min_size,
        margin_rate=margin_rate,
        epic=str(inst.get("epic") or ""),
        lot_size=float(inst.get("lotSize") or 1),
    )


def parse_price_bars(raw: list[dict[str, Any]], granularity: str, now: datetime) -> list[Bar]:
    """Map ``GET /prices/{epic}`` points (bid/ask OHLC) into mid-price bars.

    The API has no "complete" flag: a bar is complete once its period has ended.
    """
    duration = timedelta(seconds=GRANULARITY_SECONDS[granularity])
    bars = []
    for p in raw:
        t = parse_time(p.get("snapshotTimeUTC") or p["snapshotTime"])
        close = p.get("closePrice") or {}
        bars.append(
            Bar(
                time=t,
                open=_mid(p["openPrice"]),
                high=_mid(p["highPrice"]),
                low=_mid(p["lowPrice"]),
                close=_mid(close),
                volume=int(p.get("lastTradedVolume") or 0),
                complete=t + duration <= now,
                bid_close=_f(close.get("bid")),
                ask_close=_f(close.get("ask")),
            )
        )
    return bars


def monthly_bars(days: list[Bar], now: datetime) -> list[Bar]:
    """Aggregate daily bars into calendar-month (UTC) bars.

    The earliest month is dropped because the daily history may start part-way through it.
    """
    groups: dict[tuple[int, int], list[Bar]] = {}
    for b in sorted(days, key=lambda x: x.time):
        groups.setdefault((b.time.year, b.time.month), []).append(b)
    out = []
    for year, month in sorted(groups)[1:]:
        g = groups[(year, month)]
        start = datetime(year, month, 1, tzinfo=UTC)
        next_start = datetime(year + month // 12, month % 12 + 1, 1, tzinfo=UTC)
        out.append(
            Bar(
                time=start,
                open=g[0].open,
                high=max(b.high for b in g),
                low=min(b.low for b in g),
                close=g[-1].close,
                volume=sum(b.volume for b in g),
                complete=next_start <= now,
                bid_close=g[-1].bid_close,
                ask_close=g[-1].ask_close,
            )
        )
    return out


def is_close_activity(a: dict[str, Any], deal_id: str, opened_at: datetime) -> bool:
    """Whether an activity-history entry records the closing of position ``deal_id``."""
    if a.get("type") != "POSITION" or a.get("status") in REJECTED_STATUSES:
        return False
    details = a.get("details") or {}
    actions = details.get("actions") or []
    related = a.get("dealId") == deal_id or any(x.get("affectedDealId") == deal_id for x in actions)
    if not related:
        return False
    if any(x.get("actionType") == "POSITION_CLOSED" for x in actions):
        return True
    if any(x.get("actionType") == "POSITION_OPENED" for x in actions):
        return False
    if a.get("source") in CLOSE_SOURCES:
        return True
    # No action details: any later accepted activity on a position that is gone is its close.
    date = a.get("dateUTC")
    return bool(date) and parse_time(str(date)) > opened_at + timedelta(seconds=1)


def api_time(dt: datetime) -> str:
    """Capital.com history/price filters take UTC as ``YYYY-MM-DDTHH:MM:SS`` (no zone)."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _mid(side: dict[str, Any]) -> float:
    bid, ask = _f(side.get("bid")), _f(side.get("ask"))
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    value = bid if bid is not None else ask
    if value is None:
        raise ValueError("price point has neither bid nor ask")
    return value


def _decimal_places(value: Any) -> int:
    d = Decimal(str(value)).normalize()
    exponent = d.as_tuple().exponent
    return max(0, -exponent) if isinstance(exponent, int) else 0


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"raw": resp.text[:2000]}
