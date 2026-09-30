"""Trading engine process: wires every component together and runs the decision flow.

End-to-end flow (architecture section 13), once per completed M15 candle:
  receive prices -> update state -> sync candles -> technical state -> S/R + regime ->
  news state -> strategy check -> market snapshot -> persist request -> decision model
  (only for candidates unless LLM_CALL_POLICY=always) -> validate -> risk engine ->
  executor -> reconciliation.

Run with ``python -m app.engine``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.alerts.notifier import Notifier
from app.alerts.telegram import TelegramSender
from app.broker.oanda import InstrumentInfo, OandaClient
from app.config.settings import LlmCallPolicy, Settings, get_settings
from app.db.control import get_all_controls, set_control
from app.db.enums import OPEN_ORDER_STATUSES, ControlKey, OrderPurpose, OrderStatus, TradeState
from app.db.models import Decision, DecisionRequest, MarketRegime, Order, RiskCheck, Trade
from app.db.session import Database
from app.decision.openrouter import OpenRouterClient
from app.decision.service import DecisionOutcome, DecisionService, prefilter_wait
from app.execution.executor import OrderExecutor
from app.logging_setup import setup_logging
from app.market_data.service import MarketDataService
from app.market_data.state import MarketState, PriceTick
from app.market_data.timeutil import GRANULARITY_SECONDS, is_fx_market_open, next_boundary, trading_day, utcnow
from app.news.providers.base import ArticleProvider, CalendarProvider
from app.news.providers.forexfactory import ForexFactoryCalendar
from app.news.providers.rss import RssFeed
from app.news.service import NewsService
from app.news.state import NewsState
from app.reconciliation.reconciler import Reconciler
from app.risk.conversion import conversion_rates
from app.risk.engine import OpenTradeRisk, RiskContext, RiskResult, evaluate
from app.snapshot.builder import build_snapshot
from app.strategy.trend_pullback import StrategyResult, evaluate_trend_pullback
from app.technicals.engine import compute_technical_state, persist_technical_state

log = logging.getLogger("tradlysis.engine")
COMPONENT = "engine"
BARS_PER_TF = {"M15": 300, "H1": 300, "H4": 300, "D": 5, "W": 3, "M": 3}
SYNC_COUNTS = {"M15": 20, "H1": 10, "H4": 10, "D": 5, "W": 3, "M": 2}


@dataclass
class CycleSummary:
    candle_time: str
    request_id: int | None = None
    candidate: bool = False
    llm_called: bool = False
    decision: str | None = None
    confidence: float | None = None
    approved: bool | None = None
    rejections: list[str] | None = None
    order_status: str | None = None
    note: str = ""


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        client: OandaClient | None = None,
        llm: OpenRouterClient | None = None,
        calendar: CalendarProvider | None = None,
        feeds: list[ArticleProvider] | None = None,
        telegram: TelegramSender | None = None,
    ) -> None:
        """Dependencies can be injected (tests); by default they are built from settings."""
        settings.require_database()
        settings.require_broker_credentials()
        self.settings = settings
        self.db = db or Database(settings)
        self.telegram = telegram or TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)
        label = f"[{settings.trading_mode.value.upper()}]"
        self.notifier = Notifier(self.db, self.telegram, settings.alert_dedup_seconds, label=label)
        self.client = client or OandaClient(
            settings.oanda_rest_url,
            settings.oanda_stream_url,
            settings.oanda_api_token,
            settings.oanda_account_id,
            timeout=settings.oanda_request_timeout_seconds,
        )
        self.llm = llm or OpenRouterClient(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            settings.openrouter_app_url,
            settings.openrouter_app_name,
            timeout=settings.llm_timeout_seconds,
        )
        self.decider = DecisionService(
            self.llm, settings.openrouter_model, settings.llm_max_tokens, settings.llm_temperature
        )
        self.news = NewsService(
            settings,
            self.db,
            self.notifier,
            calendar or ForexFactoryCalendar(settings.news_calendar_url),
            feeds if feeds is not None else [RssFeed(cur, url) for cur, url in settings.rss_feeds],
            self.llm,
        )
        # Set in start() once instrument details are known.
        self.instrument: InstrumentInfo | None = None
        self.state: MarketState | None = None
        self.market: MarketDataService | None = None
        self.executor: OrderExecutor | None = None
        self.reconciler: Reconciler | None = None
        self.last_cycle: CycleSummary | None = None
        self.started_at = utcnow()
        self._cycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> dict[str, Any]:
        """Connect to the broker and build the broker-dependent components."""
        s = self.settings
        summary = await self.client.get_account_summary()
        acct_id = s.oanda_account_id
        if s.is_demo and not acct_id.startswith("101-"):
            await self.notifier.warning(
                COMPONENT,
                "ACCOUNT_ID_UNUSUAL",
                f"Demo mode but account id {acct_id} does not look like a practice account (101-...)",
            )
        self.instrument = await self.client.get_instrument(s.instrument)
        self.state = MarketState(s.instrument, self.instrument.pip_size)
        self.market = MarketDataService(s, self.client, self.db, self.notifier, self.state)
        self.executor = OrderExecutor(s, self.client, self.db, self.notifier, self.instrument)
        self.reconciler = Reconciler(s, self.client, self.db, self.notifier, self.executor)
        return summary

    async def start(self) -> None:
        s = self.settings
        summary = await self.setup()
        assert self.executor and self.reconciler and self.market
        await self.executor.resolve_unresolved_orders()
        await self.reconciler.reconcile_once()
        await self.market.backfill()
        await self.notifier.info(
            COMPONENT,
            "ENGINE_STARTED",
            f"Engine started in {s.trading_mode.value} mode on {s.instrument}; account currency "
            f"{summary.get('currency')}, NAV {summary.get('NAV')}; model {s.openrouter_model}",
            alert=True,
        )
        if not self.llm.enabled:
            await self.notifier.warning(
                COMPONENT, "LLM_DISABLED", "OPENROUTER_API_KEY not set: every opportunity will be recorded as WAIT"
            )

    async def run(self, stop: asyncio.Event) -> None:
        await self.start()
        assert self.market and self.reconciler
        tasks = [
            asyncio.create_task(self.market.run_stream(stop), name="stream"),
            asyncio.create_task(self.market.run_price_persister(stop), name="price_persister"),
            asyncio.create_task(self.market.run_health_monitor(stop), name="health"),
            asyncio.create_task(self.reconciler.run(stop), name="reconciler"),
            asyncio.create_task(self.news.run_calendar_loop(stop), name="calendar"),
            asyncio.create_task(self.news.run_articles_loop(stop), name="articles"),
            asyncio.create_task(self._scheduler(stop), name="scheduler"),
            asyncio.create_task(self._control_watcher(stop), name="controls"),
            asyncio.create_task(self._heartbeat(stop), name="heartbeat"),
        ]
        try:
            done, _ = await asyncio.wait(
                [asyncio.create_task(stop.wait(), name="stop"), *tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                if t.get_name() != "stop" and t.exception():
                    await self.notifier.critical(
                        COMPONENT, "TASK_CRASHED", f"Task {t.get_name()} crashed: {t.exception()!r}"
                    )
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.notifier.info(COMPONENT, "ENGINE_STOPPED", "Engine stopped", alert=True)
            await self.close()

    async def close(self) -> None:
        await self.client.aclose()
        await self.llm.aclose()
        await self.telegram.aclose()
        await self.db.dispose()

    # ------------------------------------------------------------------ loops

    async def _scheduler(self, stop: asyncio.Event) -> None:
        g = self.settings.decision_granularity
        while not stop.is_set():
            now = utcnow()
            boundary = next_boundary(now, g)
            wake = boundary + timedelta(seconds=self.settings.decision_candle_delay_seconds)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max((wake - now).total_seconds(), 0.0))
                return
            except TimeoutError:
                pass
            candle_time = boundary - timedelta(seconds=GRANULARITY_SECONDS[g])
            if not is_fx_market_open(boundary - timedelta(seconds=1)):
                continue
            try:
                await self.run_cycle(candle_time)
            except Exception as exc:
                log.exception("decision cycle failed")
                await self.notifier.error(
                    COMPONENT, "CYCLE_FAILED", f"Decision cycle for {candle_time.isoformat()} failed: {exc!r}",
                    dedup_key="cycle_failed",
                )

    async def _control_watcher(self, stop: asyncio.Event) -> None:
        assert self.executor
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
                return
            except TimeoutError:
                pass
            try:
                async with self.db.session() as s:
                    controls = await get_all_controls(s)
                req = controls.get(str(ControlKey.FLATTEN_REQUEST)) or {}
                if req.get("requested"):
                    async with self.db.session() as s:
                        await set_control(
                            s, ControlKey.FLATTEN_REQUEST,
                            {"requested": False, "handled_at": utcnow().isoformat(), "reason": req.get("reason")},
                            COMPONENT,
                        )
                    await self.executor.flatten_all(str(req.get("reason") or "dashboard request"))
            except Exception:
                log.exception("control watcher error")

    async def _heartbeat(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                async with self.db.session() as s:
                    await set_control(s, ControlKey.ENGINE_HEARTBEAT, self.status(), COMPONENT)
            except Exception:
                log.exception("heartbeat error")
            try:
                await asyncio.wait_for(stop.wait(), timeout=10.0)
            except TimeoutError:
                pass

    def status(self) -> dict[str, Any]:
        now = utcnow()
        acct = self.reconciler.account if self.reconciler else None
        return {
            "at": now.isoformat(),
            "started_at": self.started_at.isoformat(),
            "mode": self.settings.trading_mode.value,
            "experiment": self.settings.experiment_name,
            "instrument": self.settings.instrument,
            "model": self.settings.openrouter_model,
            "llm_enabled": self.llm.enabled,
            "llm_call_policy": self.settings.llm_call_policy.value,
            "telegram_enabled": self.telegram.enabled,
            "market": self.state.status(now) if self.state else None,
            "account": None
            if acct is None
            else {
                "currency": acct.currency,
                "balance": str(acct.balance),
                "nav": str(acct.nav),
                "unrealized_pl": str(acct.unrealized_pl),
                "margin_available": str(acct.margin_available),
                "open_trade_count": acct.open_trade_count,
                "fetched_at": acct.fetched_at.isoformat(),
            },
            "news_calendar_last_success": self.news.last_calendar_success.isoformat()
            if self.news.last_calendar_success
            else None,
            "last_cycle": self.last_cycle.__dict__ if self.last_cycle else None,
            "risk_limits": {
                "risk_per_trade_pct": self.settings.risk_per_trade_pct,
                "max_total_risk_pct": self.settings.max_total_risk_pct,
                "max_daily_loss_pct": self.settings.max_daily_loss_pct,
                "max_drawdown_pct": self.settings.max_drawdown_pct,
                "min_risk_reward": self.settings.min_risk_reward,
                "max_spread_pips": self.settings.max_spread_pips,
                "max_open_trades": self.settings.max_open_trades,
            },
        }

    # ------------------------------------------------------------------ decision cycle

    async def run_cycle(self, candle_time: datetime) -> CycleSummary:
        async with self._cycle_lock:
            summary = await self._run_cycle(candle_time)
            self.last_cycle = summary
            return summary

    async def _run_cycle(self, candle_time: datetime) -> CycleSummary:
        assert self.market and self.state and self.instrument
        s = self.settings
        summary = CycleSummary(candle_time=candle_time.isoformat())

        # 1-2. Candles (broker candles are authoritative).
        for g, count in SYNC_COUNTS.items():
            await self.market.sync_candles(g, count)
        bars = {g: await self.market.load_bars(g, n) for g, n in BARS_PER_TF.items()}
        m15 = bars["M15"]
        if not m15 or m15[-1].time < candle_time:
            summary.note = "M15 candle not complete at broker yet"
            await self.notifier.warning(COMPONENT, "CANDLE_NOT_READY", summary.note, dedup_key="candle_not_ready")
            return summary
        if len(bars["H4"]) < 210 or len(bars["H1"]) < 210 or len(m15) < 210:
            summary.note = "insufficient candle history"
            await self.notifier.warning(COMPONENT, "INSUFFICIENT_HISTORY", summary.note)
            return summary

        tick = self.state.last_tick
        if tick is None:
            summary.note = "no live price yet"
            await self.notifier.warning(COMPONENT, "NO_PRICE", summary.note)
            return summary

        now = utcnow()
        # 3-4. Technicals, levels and regime.
        tech = compute_technical_state(s.instrument, bars, tick.mid, self.instrument.pip_size, now)
        async with self.db.session() as sess:
            await persist_technical_state(sess, tech)

        # 5. News, then the overall market regime (needs the news blackout flag).
        news = await self.news.current_state(now)
        regime = tech.market_regime(news.blackout)
        async with self.db.session() as sess:
            sess.add(
                MarketRegime(
                    instrument=s.instrument,
                    timeframe="OVERALL",
                    computed_at=now,
                    trend_regime=str(regime),
                    volatility_regime=str(tech.volatility_regime),
                    atr=tech.timeframes["H1"].atr14,
                    atr_percentile=tech.atr_percentile,
                    details={"h4": str(tech.timeframes["H4"].trend), "h1": str(tech.timeframes["H1"].trend)},
                )
            )

        # 6. Strategy + snapshot.
        strategy = evaluate_trend_pullback(tech, tick.bid, tick.ask, s)
        summary.candidate = strategy.candidate
        snapshot = build_snapshot(
            tech=tech, tick=tick, news=news, strategy=strategy, decision_time=now, market_regime=regime
        )

        # 7. Persist the request (unique per experiment/instrument/candle => no duplicate cycles).
        request_id = await self._insert_request(candle_time, snapshot, strategy)
        if request_id is None:
            summary.note = "cycle already processed"
            return summary
        summary.request_id = request_id

        # 8-10. Decision.
        outcome = await self._decide(snapshot, strategy, news)
        summary.llm_called = outcome.source != "PREFILTER"
        summary.decision = outcome.decision
        summary.confidence = outcome.confidence
        decision_id = await self._record_decision(request_id, outcome)
        if not outcome.is_trade:
            return summary

        # 11-13. Risk engine.
        ctx = await self._risk_context(now, outcome, strategy, news, tick)
        risk = evaluate(ctx, s)
        risk_check_id = await self._record_risk(request_id, decision_id, risk)
        summary.approved = risk.approved
        summary.rejections = risk.rejection_reasons
        await self._trip_breakers(risk)
        if not risk.approved:
            await self.notifier.info(
                COMPONENT,
                "TRADE_REJECTED",
                f"{outcome.decision} ({outcome.confidence}) rejected by risk engine: {', '.join(risk.rejection_reasons)}",
                alert=True,
                dedup_key=f"rejected_{request_id}",
            )
            return summary

        # 14-16. Execution + reconciliation.
        assert self.executor and self.reconciler
        order = await self.executor.execute_trade(risk_check_id, request_id, risk)
        summary.order_status = order.status
        await self.reconciler.reconcile_once()
        return summary

    async def _decide(self, snapshot: dict[str, Any], strategy: StrategyResult, news: NewsState) -> DecisionOutcome:
        s = self.settings
        if s.llm_call_policy == LlmCallPolicy.CANDIDATES_ONLY:
            if not strategy.candidate:
                return prefilter_wait(strategy.failure_codes)
            if news.blackout:
                return prefilter_wait(["NEWS_BLACKOUT"])
        if not self.llm.enabled:
            return prefilter_wait(["LLM_NOT_CONFIGURED"])
        outcome = await self.decider.decide(snapshot)
        if not outcome.valid:
            await self.notifier.warning(
                COMPONENT, "DECISION_INVALID", f"Model response rejected ({outcome.validation_error}); recorded as WAIT",
                dedup_key="decision_invalid",
            )
        return outcome

    async def _insert_request(self, candle_time: datetime, snapshot: dict[str, Any], strategy: StrategyResult) -> int | None:
        s = self.settings
        async with self.db.session() as sess:
            stmt = (
                insert(DecisionRequest)
                .values(
                    experiment=s.experiment_name,
                    instrument=s.instrument,
                    candle_time=candle_time,
                    snapshot=snapshot,
                    trade_plan=strategy.trade_plan.to_dict() if strategy.trade_plan else None,
                    strategy_result=strategy.to_dict(),
                )
                .on_conflict_do_nothing(index_elements=["experiment", "instrument", "candle_time"])
                .returning(DecisionRequest.id)
            )
            return (await sess.execute(stmt)).scalar_one_or_none()

    async def _record_decision(self, request_id: int, outcome: DecisionOutcome) -> int:
        async with self.db.session() as sess:
            req = await sess.get(DecisionRequest, request_id)
            assert req is not None
            if outcome.source != "PREFILTER":
                req.llm_called = True
                req.model = self.decider.model
                req.prompt_version = self.decider.prompt_version
                req.messages = outcome.messages
            raw = outcome.raw_response
            if outcome.rationale and isinstance(raw, dict):
                raw = {**raw, "rationale": outcome.rationale}
            d = Decision(
                request_id=request_id,
                source=str(outcome.source),
                decision=str(outcome.decision),
                setup=outcome.setup,
                confidence=outcome.confidence,
                reason_codes=outcome.reason_codes,
                valid=outcome.valid,
                validation_error=outcome.validation_error,
                raw_response=raw,
                latency_ms=outcome.latency_ms,
                prompt_tokens=outcome.prompt_tokens,
                completion_tokens=outcome.completion_tokens,
            )
            sess.add(d)
            await sess.flush()
            return d.id

    async def _record_risk(self, request_id: int, decision_id: int, risk: RiskResult) -> int:
        async with self.db.session() as sess:
            rc = RiskCheck(
                request_id=request_id,
                decision_id=decision_id,
                approved=risk.approved,
                checks=risk.checks_json(),
                rejection_reasons=risk.rejection_reasons,
                direction=risk.direction,
                units=risk.units,
                risk_amount=risk.risk_amount,
                risk_pct=risk.risk_pct,
                entry_price=risk.entry,
                stop_loss=risk.stop_loss,
                take_profit=risk.take_profit,
                risk_reward=risk.risk_reward,
                account_nav=risk.nav,
            )
            sess.add(rc)
            await sess.flush()
            return rc.id

    async def _trip_breakers(self, risk: RiskResult) -> None:
        # The reconciler normally trips breakers first; this covers the gap between its cycles.
        if not (risk.trip_daily_breaker or risk.trip_drawdown_breaker):
            return
        async with self.db.session() as sess:
            if risk.trip_daily_breaker:
                await set_control(
                    sess, ControlKey.DAILY_LOSS_BREAKER,
                    {"tripped": True, "trading_day": trading_day(utcnow()).isoformat()}, "risk",
                )
            if risk.trip_drawdown_breaker:
                await set_control(sess, ControlKey.DRAWDOWN_BREAKER, {"tripped": True, "at": utcnow().isoformat()}, "risk")

    async def _risk_context(
        self,
        now: datetime,
        outcome: DecisionOutcome,
        strategy: StrategyResult,
        news: NewsState,
        tick: PriceTick,
    ) -> RiskContext:
        assert self.state and self.instrument and self.reconciler
        s = self.settings
        acct = self.reconciler.account
        async with self.db.session() as sess:
            controls = await get_all_controls(sess)
            unresolved = await sess.scalar(
                select(func.count()).select_from(Order).where(Order.status.in_([str(x) for x in OPEN_ORDER_STATUSES]))
            )
            last_entry = await sess.scalar(
                select(func.max(Order.created_at)).where(
                    Order.purpose == str(OrderPurpose.ENTRY),
                    Order.direction == outcome.decision,
                    Order.status.in_([str(OrderStatus.FILLED), *[str(x) for x in OPEN_ORDER_STATUSES]]),
                )
            )
            local_open = (await sess.scalars(select(Trade).where(Trade.state == str(TradeState.OPEN)))).all()

        open_trades: dict[str, OpenTradeRisk] = {}
        for t in local_open:
            open_trades[t.broker_trade_id] = OpenTradeRisk(t.instrument, t.current_units, t.open_price, t.stop_loss)
        for bt in self.reconciler.broker_open_trades:
            sl = (bt.get("stopLossOrder") or {}).get("price")
            open_trades[bt["id"]] = OpenTradeRisk(
                bt["instrument"], int(Decimal(str(bt["currentUnits"]))), float(bt["price"]), float(sl) if sl else None
            )

        quote_rate = base_rate = None
        if acct is not None:
            home_conv = None
            base, quote = s.instrument_currencies
            if acct.currency not in (base, quote):
                try:
                    pricing = await self.client.get_pricing([s.instrument], include_home_conversions=True)
                    home_conv = pricing.get("homeConversions")
                except Exception as exc:
                    log.warning("pricing/home conversion fetch failed: %s", exc)
            quote_rate, base_rate = conversion_rates(acct.currency, s.instrument, tick.mid, home_conv)

        kill = controls.get(str(ControlKey.KILL_SWITCH)) or {}
        daily = controls.get(str(ControlKey.DAILY_LOSS_BREAKER)) or {}
        dd = controls.get(str(ControlKey.DRAWDOWN_BREAKER)) or {}
        day_nav = controls.get(str(ControlKey.DAY_START_NAV)) or {}
        peak = controls.get(str(ControlKey.PEAK_NAV)) or {}
        return RiskContext(
            now=now,
            decision=outcome.decision,
            decision_valid=outcome.valid,
            confidence=outcome.confidence,
            trade_plan=strategy.trade_plan,
            strategy_candidate=strategy.candidate,
            kill_switch_active=bool(kill.get("active")),
            kill_switch_reason=str(kill.get("reason", "")),
            daily_breaker_tripped=bool(daily.get("tripped")),
            drawdown_breaker_tripped=bool(dd.get("tripped")),
            market_open=is_fx_market_open(now),
            stream_connected=self.state.stream_connected,
            bid=tick.bid,
            ask=tick.ask,
            price_time=tick.time,
            tradeable=tick.tradeable,
            instrument=self.instrument,
            account_currency=acct.currency if acct else "",
            nav=acct.nav if acct else None,
            balance=acct.balance if acct else None,
            margin_available=acct.margin_available if acct else None,
            account_state_time=acct.fetched_at if acct else None,
            day_start_nav=Decimal(str(day_nav["value"])) if day_nav.get("value") else None,
            peak_nav=Decimal(str(peak["value"])) if peak.get("value") else None,
            quote_home_rate=quote_rate,
            base_home_rate=base_rate,
            open_trades=list(open_trades.values()),
            unresolved_orders=int(unresolved or 0),
            last_entry_same_direction_at=last_entry,
            news_blackout=news.blackout,
            news_blackout_titles=[e["title"] for e in news.blackout_events],
            calendar_fresh=news.calendar_fresh,
        )


async def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    engine = TradingEngine(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    await engine.run(stop)


if __name__ == "__main__":
    asyncio.run(main())
