"""Market Data Engine: live price stream, price persistence, candle sync and staleness checks."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.alerts.notifier import Notifier
from app.broker.capital import CapitalClient, CapitalError, CapitalTransportError
from app.config.settings import Settings
from app.db.models import Candle, MarketPrice
from app.db.session import Database
from app.market_data.candles import Bar
from app.market_data.state import MarketState, PriceTick
from app.market_data.timeutil import utcnow

log = logging.getLogger(__name__)

COMPONENT = "market_data"
# Timeframes kept in the database. D, W and M (month) feed the reference levels.
SYNC_GRANULARITIES = ("M5", "M15", "H1", "H4", "D", "W", "M")
MARKET_STATUS_POLL_SECONDS = 60.0


class MarketDataService:
    def __init__(
        self,
        settings: Settings,
        client: CapitalClient,
        db: Database,
        notifier: Notifier,
        state: MarketState,
    ) -> None:
        self.settings = settings
        self.client = client
        self.db = db
        self.notifier = notifier
        self.state = state
        self._last_persisted: PriceTick | None = None
        self._stale_alerted = False
        self._status_checked_at: datetime | None = None

    # ------------------------------------------------------------------ stream

    async def run_stream(self, stop: asyncio.Event) -> None:
        backoff = 1.0
        was_connected_once = False
        while not stop.is_set():
            try:
                async for quote in self.client.stream_quotes():
                    now = utcnow()
                    if quote is not None:
                        self.state.on_tick(PriceTick.from_quote(quote, self.state.broker_tradeable), now)
                    self.state.on_heartbeat(now)
                    if not self.state.stream_connected:
                        self.state.stream_connected = True
                        self.state.stream_connected_since = now
                        backoff = 1.0
                        if was_connected_once:
                            await self.notifier.info(
                                COMPONENT, "STREAM_RECONNECTED", "Price stream reconnected", alert=True,
                                dedup_key="stream_reconnected",
                            )
                        was_connected_once = True
                    if stop.is_set():
                        break
                # Server closed the stream cleanly; treat as a disconnect.
                if not stop.is_set():
                    raise CapitalTransportError("price stream closed by server")
            except asyncio.CancelledError:
                raise
            except (CapitalTransportError, CapitalError, ValueError, KeyError) as exc:
                self.state.stream_connected = False
                self.state.reconnects += 1
                level_fn = self.notifier.error if isinstance(exc, CapitalError) else self.notifier.warning
                await level_fn(
                    COMPONENT,
                    "STREAM_DISCONNECTED",
                    f"Price stream disconnected: {exc}. Reconnecting in {backoff:.0f}s",
                    details={"reconnects": self.state.reconnects},
                    dedup_key="stream_disconnected",
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self.state.stream_connected = False

    async def run_price_persister(self, stop: asyncio.Event) -> None:
        interval = self.settings.price_persist_interval_seconds
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
            tick = self.state.last_tick
            if tick is None or tick is self._last_persisted:
                continue
            try:
                async with self.db.session() as s:
                    s.add(
                        MarketPrice(
                            instrument=tick.instrument,
                            time=tick.time,
                            bid=tick.bid,
                            ask=tick.ask,
                            mid=tick.mid,
                            spread_pips=tick.spread_pips(self.state.pip_size),
                            tradeable=tick.tradeable,
                        )
                    )
                self._last_persisted = tick
            except Exception:
                log.exception("Failed to persist price")

    async def refresh_market_status(self) -> None:
        """Poll the broker's market status (TRADEABLE, CLOSED, ...); fails closed on errors."""
        try:
            status = await self.client.get_market_status()
        except Exception as exc:
            log.warning("market status fetch failed: %s", exc)
            status = "UNKNOWN"
        self.state.broker_market_status = status
        self.state.broker_tradeable = status == "TRADEABLE"
        self._status_checked_at = utcnow()

    async def run_health_monitor(self, stop: asyncio.Event) -> None:
        """Alert on stale prices during market hours and keep the broker market status fresh."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except TimeoutError:
                pass
            now = utcnow()
            if (
                self._status_checked_at is None
                or (now - self._status_checked_at).total_seconds() >= MARKET_STATUS_POLL_SECONDS
            ):
                await self.refresh_market_status()
            if not self.state.market_open(now):
                self._stale_alerted = False
                continue
            stale = self.state.is_price_stale(now, self.settings.stale_price_seconds * 4)
            if stale and not self._stale_alerted:
                self._stale_alerted = True
                await self.notifier.warning(
                    COMPONENT,
                    "STALE_PRICES",
                    "No fresh prices during market hours",
                    details=self.state.status(now),
                    dedup_key="stale_prices",
                )
            elif not stale and self._stale_alerted:
                self._stale_alerted = False
                await self.notifier.info(COMPONENT, "PRICES_RECOVERED", "Fresh prices are flowing again")

    # ------------------------------------------------------------------ candles

    async def sync_candles(self, granularity: str, count: int) -> int:
        bars = await self.client.get_candles(granularity, count=count)
        await self.upsert_bars(granularity, bars)
        return len(bars)

    async def upsert_bars(self, granularity: str, bars: list[Bar]) -> None:
        if not bars:
            return
        rows = [
            {
                "instrument": self.settings.instrument,
                "granularity": granularity,
                "time": b.time,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "bid_close": b.bid_close,
                "ask_close": b.ask_close,
                "volume": b.volume,
                "complete": b.complete,
                "source": "capital",
            }
            for b in bars
        ]
        async with self.db.session() as s:
            for i in range(0, len(rows), 500):
                stmt = insert(Candle).values(rows[i : i + 500])
                stmt = stmt.on_conflict_do_update(
                    index_elements=["instrument", "granularity", "time"],
                    set_={
                        "open": stmt.excluded.open,
                        "high": stmt.excluded.high,
                        "low": stmt.excluded.low,
                        "close": stmt.excluded.close,
                        "bid_close": stmt.excluded.bid_close,
                        "ask_close": stmt.excluded.ask_close,
                        "volume": stmt.excluded.volume,
                        "complete": stmt.excluded.complete,
                        "updated_at": func.now(),
                    },
                )
                await s.execute(stmt)

    async def backfill(self) -> None:
        for g in SYNC_GRANULARITIES:
            n = await self.sync_candles(g, self.settings.candle_history_count)
            log.info("Backfilled %d %s candles", n, g)

    async def load_bars(self, granularity: str, limit: int = 500, *, before: datetime | None = None) -> list[Bar]:
        """Most recent complete candles in ascending time order."""
        q = select(Candle).where(
            Candle.instrument == self.settings.instrument,
            Candle.granularity == granularity,
            Candle.complete.is_(True),
        )
        if before is not None:
            q = q.where(Candle.time < before)
        q = q.order_by(Candle.time.desc()).limit(limit)
        async with self.db.session() as s:
            rows = (await s.scalars(q)).all()
        return [
            Bar(
                time=r.time,
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
                complete=r.complete,
                bid_close=r.bid_close,
                ask_close=r.ask_close,
            )
            for r in reversed(rows)
        ]
