"""Trading engine process: wires every component together and runs the decision flow.

One engine runs every enabled experiment (see ``app/config/store.py``) on one broker account.
Market data, technicals and news are shared per instrument; each experiment then runs its own
decision cycle with its own strategy parameters, model and risk limits.

End-to-end flow (architecture section 13), once per completed M15 candle:
  receive prices -> update state -> sync candles -> technical state -> S/R + regime   (per instrument)
  -> news state -> strategy check -> market snapshot -> persist request -> decision model
  (only for candidates unless LLM_CALL_POLICY=always) -> validate -> risk engine ->
  executor -> reconciliation                                                          (per experiment)

Saving the configuration in the dashboard bumps its version; the engine notices, waits for a
running cycle to finish and starts again with the new configuration (in the same process).

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
from app.broker.capital import CapitalClient
from app.broker.types import InstrumentInfo
from app.config.settings import LlmCallPolicy, Settings, get_settings
from app.config.store import (
    Configuration,
    ExperimentConfig,
    get_version,
    import_env_once,
    load_configuration,
    set_experiment_capital,
    single_experiment,
)
from app.db.bootstrap import auto_create_tables
from app.db.control import experiment_controls, get_all_controls, scoped, set_control
from app.db.enums import OPEN_ORDER_STATUSES, ControlKey, OrderPurpose, OrderStatus, TradeState
from app.db.models import Decision, DecisionRequest, MarketRegime, Order, RiskCheck, Trade
from app.db.session import Database
from app.decision.openrouter import OpenRouterClient
from app.decision.service import DecisionOutcome, DecisionService, prefilter_wait
from app.execution.executor import OrderExecutor
from app.logging_setup import setup_logging
from app.market_data.service import MarketDataService, PriceStream
from app.market_data.state import MarketState, PriceTick
from app.market_data.timeutil import GRANULARITY_SECONDS, is_fx_market_open, next_boundary, trading_day, utcnow
from app.news.providers.base import ArticleProvider, CalendarProvider
from app.news.providers.forexfactory import ForexFactoryCalendar
from app.news.providers.rss import RssFeed
from app.news.service import NewsService
from app.news.state import NewsState
from app.reconciliation.reconciler import ExperimentBook, Reconciler
from app.risk.conversion import conversion_rates
from app.risk.engine import OpenTradeRisk, RiskContext, RiskResult, evaluate
from app.snapshot.builder import build_snapshot
from app.strategy.base import StrategyResult
from app.strategy.range_breakout import evaluate_range_breakout
from app.strategy.registry import STRATEGIES
from app.strategy.session_breakout import evaluate_session_breakout
from app.strategy.trend_following import evaluate_trend_following
from app.strategy.trend_pullback import evaluate_trend_pullback
from app.technicals.engine import TechnicalState, compute_technical_state, persist_technical_state

log = logging.getLogger("tradlysis.engine")
COMPONENT = "engine"
BARS_PER_TF = {"M15": 300, "H1": 300, "H4": 300, "D": 5, "W": 3, "M": 3}
# M5 is kept current for trade excursion measurement (app/experiments/excursion.py).
SYNC_COUNTS = {"M5": 6, "M15": 20, "H1": 10, "H4": 10, "D": 5, "W": 3, "M": 2}
CONFIG_POLL_SECONDS = 10.0


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


@dataclass
class Market:
    """One instrument's live state and data service, shared by every experiment trading it."""

    info: InstrumentInfo
    state: MarketState
    service: MarketDataService


@dataclass
class MarketCycle:
    """An instrument's technical state for one candle, computed once for all its experiments."""

    note: str = ""
    tech: TechnicalState | None = None
    tick: PriceTick | None = None
    now: datetime | None = None
    regime_recorded: bool = False


def startup_problem(config: Configuration) -> str | None:
    """Why the engine cannot start with this configuration (it then waits for a change)."""
    if not config.enabled:
        return "no experiment is enabled; enable one on the config page"
    try:
        config.settings.require_broker_credentials()
    except RuntimeError as exc:
        return f"{exc}; add them on the config page"
    unknown = [e.slug for e in config.enabled if e.strategy not in STRATEGIES]
    if unknown:
        return f"unknown strategy for {', '.join(unknown)}"
    return None


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        configuration: Configuration | None = None,
        db: Database | None = None,
        client: CapitalClient | None = None,
        llm: OpenRouterClient | None = None,
        calendar: CalendarProvider | None = None,
        feeds: list[ArticleProvider] | None = None,
        telegram: TelegramSender | None = None,
        watch_config: bool = False,
    ) -> None:
        """Dependencies can be injected (tests); by default they are built from settings.

        ``settings`` are the global settings; without a ``configuration`` they also describe the
        single experiment to run.
        """
        self.config = configuration or single_experiment(settings)
        settings = self.config.settings
        settings.require_database()
        settings.require_broker_credentials()
        if not self.config.enabled:
            raise RuntimeError("no enabled experiment")
        self.settings = settings
        self.watch_config = watch_config
        self.restart_requested = False
        self.db = db or Database(settings)
        self.telegram = telegram or TelegramSender(settings.telegram_bot_token, settings.telegram_chat_id)
        label = f"[{settings.trading_mode.value.upper()}]"
        self.notifier = Notifier(self.db, self.telegram, settings.alert_dedup_seconds, label=label)
        primary = self.config.enabled[0]
        self.client = client or CapitalClient(
            settings.capital_base_url,
            settings.capital_api_key,
            settings.capital_identifier,
            settings.capital_api_password,
            account_id=settings.capital_account_id,
            instrument=primary.instrument,
            epic=primary.settings.broker_epic,
            stream_url=settings.capital_stream_url,
            timeout=settings.capital_request_timeout_seconds,
        )
        for exp in self.config.enabled:
            self.client.register_market(exp.instrument, exp.settings.broker_epic)
        self.llm = llm or OpenRouterClient(
            settings.openrouter_api_key,
            settings.openrouter_base_url,
            settings.openrouter_app_url,
            settings.openrouter_app_name,
            timeout=settings.llm_timeout_seconds,
        )
        self.news = NewsService(
            settings,
            self.db,
            self.notifier,
            calendar or ForexFactoryCalendar(settings.news_calendar_url),
            feeds if feeds is not None else [RssFeed(cur, url) for cur, url in settings.rss_feeds],
            self.llm,
        )
        self.runners: dict[str, ExperimentRunner] = {
            exp.slug: ExperimentRunner(self, exp) for exp in self.config.enabled
        }
        # Set in setup() once instrument details are known.
        self.markets: dict[str, Market] = {}
        self.stream: PriceStream | None = None
        self.executor: OrderExecutor | None = None
        self.reconciler: Reconciler | None = None
        self.started_at = utcnow()
        self._cycle_lock = asyncio.Lock()

    # ------------------------------------------------------------------ convenience (first experiment)

    @property
    def primary(self) -> ExperimentRunner:
        return next(iter(self.runners.values()))

    @property
    def market(self) -> MarketDataService | None:
        m = self.markets.get(self.primary.instrument)
        return m.service if m else None

    @property
    def state(self) -> MarketState | None:
        m = self.markets.get(self.primary.instrument)
        return m.state if m else None

    @property
    def instrument(self) -> InstrumentInfo | None:
        m = self.markets.get(self.primary.instrument)
        return m.info if m else None

    # ------------------------------------------------------------------ lifecycle

    async def setup(self) -> dict[str, Any]:
        """Connect to the broker and build the broker-dependent components."""
        s = self.settings
        account = await self.client.get_account()
        for instrument in dict.fromkeys(r.instrument for r in self.runners.values()):
            info = await self.client.get_instrument(instrument)
            if info.lot_size != 1:
                # Sizing assumes one unit of deal size is one unit of the base currency.
                raise RuntimeError(
                    f"{info.epic or instrument} has lot size {info.lot_size}; position sizing assumes 1. Refusing to start."
                )
            state = MarketState(instrument, info.pip_size)
            service = MarketDataService(s, self.client, self.db, self.notifier, state)
            await service.refresh_market_status()
            self.markets[instrument] = Market(info, state, service)
        self.stream = PriceStream(self.client, self.notifier, {i: m.state for i, m in self.markets.items()})
        self.executor = OrderExecutor(s, self.client, self.db, self.notifier, {i: m.info for i, m in self.markets.items()})
        self.executor.notifiers = {slug: r.notifier for slug, r in self.runners.items()}
        books = {
            slug: ExperimentBook(slug, r.settings, r.exp.capital, r.notifier) for slug, r in self.runners.items()
        }
        self.reconciler = Reconciler(s, self.client, self.db, self.notifier, self.executor, books, self._save_capital)
        return {"account_id": account.account_id, "currency": account.currency, "nav": str(account.nav)}

    async def _save_capital(self, slug: str, capital: Decimal) -> None:
        await set_experiment_capital(self.db, slug, capital, COMPONENT)

    async def start(self) -> None:
        s = self.settings
        created = await auto_create_tables(self.db, s)
        summary = await self.setup()
        if created:
            await self.notifier.warning(COMPONENT, "TABLES_CREATED", f"Created missing tables: {', '.join(created)}")
        assert self.executor and self.reconciler
        await self.executor.resolve_unresolved_orders()
        await self.reconciler.reconcile_once()
        for m in self.markets.values():
            await m.service.backfill()
        experiments = "; ".join(
            f"{r.exp.name} ({r.instrument}, model {r.settings.openrouter_model})" for r in self.runners.values()
        )
        await self.notifier.info(
            COMPONENT,
            "ENGINE_STARTED",
            f"Engine started in {s.trading_mode.value} mode; account {summary['account_id']} currency "
            f"{summary['currency']}, NAV {summary['nav']}. Experiments: {experiments}",
            alert=True,
        )
        if not self.llm.enabled:
            await self.notifier.warning(
                COMPONENT, "LLM_DISABLED", "OPENROUTER_API_KEY not set: every opportunity will be recorded as WAIT",
                alert=True,
            )

    async def run(self, stop: asyncio.Event) -> None:
        tasks: list[asyncio.Task] = []
        try:
            await self.start()
            assert self.stream and self.reconciler
            tasks = [
                asyncio.create_task(self.stream.run_stream(stop), name="stream"),
                asyncio.create_task(self.reconciler.run(stop), name="reconciler"),
                asyncio.create_task(self.news.run_calendar_loop(stop), name="calendar"),
                asyncio.create_task(self.news.run_articles_loop(stop), name="articles"),
                asyncio.create_task(self._scheduler(stop), name="scheduler"),
                asyncio.create_task(self._control_watcher(stop), name="controls"),
                asyncio.create_task(self._heartbeat(stop), name="heartbeat"),
            ]
            for instrument, m in self.markets.items():
                tasks.append(asyncio.create_task(m.service.run_price_persister(stop), name=f"prices:{instrument}"))
                tasks.append(asyncio.create_task(m.service.run_health_monitor(stop), name=f"health:{instrument}"))
            if self.watch_config:
                tasks.append(asyncio.create_task(self._config_watcher(stop), name="config"))
            done, _ = await asyncio.wait(
                [asyncio.create_task(stop.wait(), name="stop"), *tasks],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in done:
                if t.get_name() not in ("stop", "config") and t.exception():
                    await self.notifier.critical(
                        COMPONENT, "TASK_CRASHED", f"Task {t.get_name()} crashed: {t.exception()!r}"
                    )
        finally:
            stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if tasks:
                why = " to apply the new configuration" if self.restart_requested else ""
                await self.notifier.info(COMPONENT, "ENGINE_STOPPED", f"Engine stopped{why}", alert=True)
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
        keys = [(str(ControlKey.FLATTEN_REQUEST), None)] + [
            (scoped(ControlKey.FLATTEN_REQUEST, slug), slug) for slug in self.runners
        ]
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
                return
            except TimeoutError:
                pass
            try:
                async with self.db.session() as s:
                    controls = await get_all_controls(s)
                for key, slug in keys:
                    req = controls.get(key) or {}
                    if not req.get("requested"):
                        continue
                    async with self.db.session() as s:
                        await set_control(
                            s, key, {"requested": False, "handled_at": utcnow().isoformat(), "reason": req.get("reason")},
                            COMPONENT,
                        )
                    await self.executor.flatten_all(str(req.get("reason") or "dashboard request"), slug)
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

    async def _config_watcher(self, stop: asyncio.Event) -> None:
        """Restart (via run() returning) once the configuration was saved in the dashboard."""
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=CONFIG_POLL_SECONDS)
                return
            except TimeoutError:
                pass
            try:
                async with self.db.session() as s:
                    version = await get_version(s)
            except Exception:
                log.exception("config version check failed")
                continue
            if version != self.config.version:
                self.restart_requested = True
                log.info("configuration changed (version %s -> %s); restarting", self.config.version, version)
                async with self._cycle_lock:  # let a running decision cycle (and its order) finish
                    stop.set()
                return

    def status(self) -> dict[str, Any]:
        now = utcnow()
        acct = self.reconciler.account if self.reconciler else None
        rec = self.reconciler
        return {
            "at": now.isoformat(),
            "state": "RUNNING",
            "started_at": self.started_at.isoformat(),
            "mode": self.settings.trading_mode.value,
            "config_version": self.config.version,
            "llm_enabled": self.llm.enabled,
            "telegram_enabled": self.telegram.enabled,
            "hedging_mode": rec.hedging_mode if rec else None,
            "markets": {i: m.state.status(now) for i, m in self.markets.items()},
            "account": None
            if acct is None
            else {
                "account_id": acct.account_id,
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
            "experiments": {slug: r.status() for slug, r in self.runners.items()},
        }

    # ------------------------------------------------------------------ decision cycle

    async def run_cycle(self, candle_time: datetime) -> dict[str, CycleSummary]:
        """One candle for every experiment. Returns each experiment's summary by slug."""
        async with self._cycle_lock:
            prepared: dict[str, MarketCycle] = {}
            for instrument, market in self.markets.items():
                try:
                    prepared[instrument] = await self._prepare_market(market, candle_time)
                except Exception as exc:
                    log.exception("market data for %s failed", instrument)
                    await self.notifier.error(
                        COMPONENT, "CYCLE_FAILED", f"{instrument} market data for {candle_time.isoformat()} failed: {exc!r}",
                        dedup_key=f"cycle_failed_{instrument}",
                    )
                    prepared[instrument] = MarketCycle(note=f"market data failed: {exc!r}")
            runners = list(self.runners.values())
            results = await asyncio.gather(
                *(r.run_cycle(candle_time, prepared[r.instrument]) for r in runners), return_exceptions=True
            )
            out: dict[str, CycleSummary] = {}
            for r, res in zip(runners, results, strict=True):
                if isinstance(res, BaseException):
                    log.error("decision cycle for %s failed", r.slug, exc_info=res)
                    await r.notifier.error(
                        COMPONENT, "CYCLE_FAILED", f"Decision cycle for {candle_time.isoformat()} failed: {res!r}",
                        dedup_key="cycle_failed",
                    )
                    res = CycleSummary(candle_time=candle_time.isoformat(), note=f"failed: {res!r}")
                r.last_cycle = res
                out[r.slug] = res
            return out

    async def _prepare_market(self, market: Market, candle_time: datetime) -> MarketCycle:
        instrument = market.info.name
        for g, count in SYNC_COUNTS.items():
            await market.service.sync_candles(g, count)
        bars = {g: await market.service.load_bars(g, n) for g, n in BARS_PER_TF.items()}
        m15 = bars["M15"]
        if not m15 or m15[-1].time < candle_time:
            note = f"{instrument} M15 candle not complete at broker yet"
            await self.notifier.warning(COMPONENT, "CANDLE_NOT_READY", note, dedup_key=f"candle_not_ready_{instrument}")
            return MarketCycle(note=note)
        if len(bars["H4"]) < 210 or len(bars["H1"]) < 210 or len(m15) < 210:
            note = f"insufficient {instrument} candle history"
            await self.notifier.warning(COMPONENT, "INSUFFICIENT_HISTORY", note)
            return MarketCycle(note="insufficient candle history")
        tick = market.state.last_tick
        if tick is None:
            await self.notifier.warning(COMPONENT, "NO_PRICE", f"no live {instrument} price yet")
            return MarketCycle(note="no live price yet")
        now = utcnow()
        tech = compute_technical_state(instrument, bars, tick.mid, market.info.pip_size, now)
        async with self.db.session() as sess:
            await persist_technical_state(sess, tech)
        return MarketCycle(tech=tech, tick=tick, now=now)


class ExperimentRunner:
    """Decision cycles for one experiment, with its own settings, model, notifier and risk pool."""

    def __init__(self, engine: TradingEngine, exp: ExperimentConfig) -> None:
        self.engine = engine
        self.exp = exp
        self.slug = exp.slug
        self.instrument = exp.instrument
        self.settings = exp.settings
        self.notifier = engine.notifier.for_experiment(exp.slug, exp.name)
        s = self.settings
        self.strategy = STRATEGIES[exp.strategy]
        self.decider = DecisionService(
            engine.llm, s.openrouter_model, s.llm_max_tokens, s.llm_temperature, self.strategy.prompt
        )
        self.last_cycle: CycleSummary | None = None

    def status(self) -> dict[str, Any]:
        s = self.settings
        rec = self.engine.reconciler
        eq = rec.equity.get(self.slug) if rec else None
        return {
            "name": self.exp.name,
            "instrument": self.instrument,
            "strategy": self.exp.strategy,
            "model": s.openrouter_model,
            "llm_call_policy": s.llm_call_policy.value,
            "equity": eq.to_dict() if eq else None,
            "last_cycle": self.last_cycle.__dict__ if self.last_cycle else None,
            "risk_limits": {
                "risk_per_trade_pct": s.risk_per_trade_pct,
                "max_total_risk_pct": s.max_total_risk_pct,
                "max_daily_loss_pct": s.max_daily_loss_pct,
                "max_drawdown_pct": s.max_drawdown_pct,
                "min_risk_reward": s.min_risk_reward,
                "max_spread_pips": s.max_spread_pips,
                "max_open_trades": s.max_open_trades,
            },
        }

    async def run_cycle(self, candle_time: datetime, mc: MarketCycle) -> CycleSummary:
        engine = self.engine
        s = self.settings
        summary = CycleSummary(candle_time=candle_time.isoformat())
        if mc.note:
            summary.note = mc.note
            return summary
        assert mc.tech is not None and mc.tick is not None and mc.now is not None
        tech, tick, now = mc.tech, mc.tick, mc.now

        # News for this experiment's currencies and rules, then the overall market regime
        # (needs the news blackout flag; recorded once per instrument).
        news = await engine.news.current_state(now, s)
        regime = tech.market_regime(news.blackout)
        if not mc.regime_recorded:
            mc.regime_recorded = True
            async with engine.db.session() as sess:
                sess.add(
                    MarketRegime(
                        instrument=self.instrument,
                        timeframe="OVERALL",
                        computed_at=now,
                        trend_regime=str(regime),
                        volatility_regime=str(tech.volatility_regime),
                        atr=tech.timeframes["H1"].atr14,
                        atr_percentile=tech.atr_percentile,
                        details={"h4": str(tech.timeframes["H4"].trend), "h1": str(tech.timeframes["H1"].trend)},
                    )
                )

        # Strategy + snapshot.
        strategy = evaluate_strategy(self.exp.strategy, tech, tick.bid, tick.ask, s)
        summary.candidate = strategy.candidate
        snapshot = build_snapshot(
            tech=tech, tick=tick, news=news, strategy=strategy, decision_time=now, market_regime=regime
        )

        # Persist the request (unique per experiment/instrument/candle => no duplicate cycles).
        request_id = await self._insert_request(candle_time, snapshot, strategy)
        if request_id is None:
            summary.note = "cycle already processed"
            return summary
        summary.request_id = request_id

        # Decision.
        outcome = await self._decide(snapshot, strategy, news)
        summary.llm_called = outcome.source != "PREFILTER"
        summary.decision = outcome.decision
        summary.confidence = outcome.confidence
        decision_id = await self._record_decision(request_id, outcome)
        if strategy.candidate:
            await self.notifier.info(
                COMPONENT, "SETUP", _setup_message(self.instrument, strategy, outcome),
                alert=True, dedup_key=f"setup_{request_id}",
            )
        if not outcome.is_trade:
            return summary

        # Risk engine (this experiment's limits and equity).
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

        # Execution + reconciliation.
        assert engine.executor and engine.reconciler
        order = await engine.executor.execute_trade(risk_check_id, request_id, risk, s)
        summary.order_status = order.status
        await engine.reconciler.reconcile_once()
        return summary

    async def _decide(self, snapshot: dict[str, Any], strategy: StrategyResult, news: NewsState) -> DecisionOutcome:
        s = self.settings
        if s.llm_call_policy == LlmCallPolicy.CANDIDATES_ONLY:
            if not strategy.candidate:
                return prefilter_wait(strategy.failure_codes)
            if news.blackout:
                return prefilter_wait(["NEWS_BLACKOUT"])
        if not self.engine.llm.enabled:
            return prefilter_wait(["LLM_NOT_CONFIGURED"])
        outcome = await self.decider.decide(snapshot)
        if not outcome.valid:
            await self.notifier.warning(
                COMPONENT, "DECISION_INVALID", f"Model response rejected ({outcome.validation_error}); recorded as WAIT",
                dedup_key="decision_invalid",
            )
        return outcome

    async def _insert_request(self, candle_time: datetime, snapshot: dict[str, Any], strategy: StrategyResult) -> int | None:
        async with self.engine.db.session() as sess:
            stmt = (
                insert(DecisionRequest)
                .values(
                    experiment=self.slug,
                    instrument=self.instrument,
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
        async with self.engine.db.session() as sess:
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
        async with self.engine.db.session() as sess:
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
                account_nav=risk.nav,  # the experiment's equity the trade was sized on
            )
            sess.add(rc)
            await sess.flush()
            return rc.id

    async def _trip_breakers(self, risk: RiskResult) -> None:
        # The reconciler normally trips breakers first; this covers the gap between its cycles.
        if not (risk.trip_daily_breaker or risk.trip_drawdown_breaker):
            return
        async with self.engine.db.session() as sess:
            if risk.trip_daily_breaker:
                await set_control(
                    sess, scoped(ControlKey.DAILY_LOSS_BREAKER, self.slug),
                    {"tripped": True, "trading_day": trading_day(utcnow()).isoformat()}, "risk",
                )
            if risk.trip_drawdown_breaker:
                await set_control(
                    sess, scoped(ControlKey.DRAWDOWN_BREAKER, self.slug),
                    {"tripped": True, "at": utcnow().isoformat()}, "risk",
                )

    async def _risk_context(
        self,
        now: datetime,
        outcome: DecisionOutcome,
        strategy: StrategyResult,
        news: NewsState,
        tick: PriceTick,
    ) -> RiskContext:
        engine = self.engine
        market = engine.markets[self.instrument]
        rec = engine.reconciler
        assert rec is not None
        s = self.settings
        acct = rec.account
        eq = rec.equity.get(self.slug)
        async with engine.db.session() as sess:
            controls = await get_all_controls(sess)
            # Any unresolved order on the account blocks new entries (its outcome may still change exposure).
            unresolved = await sess.scalar(
                select(func.count()).select_from(Order).where(Order.status.in_([str(x) for x in OPEN_ORDER_STATUSES]))
            )
            last_entry = await sess.scalar(
                select(func.max(Order.created_at))
                .join(RiskCheck, RiskCheck.id == Order.risk_check_id)
                .join(DecisionRequest, DecisionRequest.id == RiskCheck.request_id)
                .where(
                    DecisionRequest.experiment == self.slug,
                    Order.purpose == str(OrderPurpose.ENTRY),
                    Order.direction == outcome.decision,
                    Order.status.in_([str(OrderStatus.FILLED), *[str(x) for x in OPEN_ORDER_STATUSES]]),
                )
            )
            local_open = (await sess.scalars(select(Trade).where(Trade.state == str(TradeState.OPEN)))).all()

        owners = {t.broker_trade_id: t.experiment for t in local_open}
        open_trades: dict[str, OpenTradeRisk] = {}
        for t in local_open:
            if t.experiment == self.slug:
                open_trades[t.broker_trade_id] = OpenTradeRisk(t.instrument, t.current_units, t.open_price, t.stop_loss)
        others = 0
        for p in rec.broker_open_trades:
            owner = owners.get(p.deal_id)
            if owner == self.slug:
                open_trades[p.deal_id] = OpenTradeRisk(p.instrument, p.units, p.open_price, p.stop_loss)
            elif p.instrument == self.instrument and (not rec.hedging_mode or owner is None):
                # Netting account: any position on the pair would be reduced by our order. Hedging:
                # positions no experiment owns still block (they may be ours, not yet recorded).
                others += 1

        quote_rate = base_rate = None
        if acct is not None:
            cross: dict[str, float] = {}
            base, quote = s.instrument_currencies
            if acct.currency not in (base, quote):
                for ccy in (base, quote):
                    try:
                        rate = await engine.client.get_conversion_rate(ccy, acct.currency)
                    except Exception as exc:
                        log.warning("conversion rate %s->%s fetch failed: %s", ccy, acct.currency, exc)
                        rate = None
                    if rate:
                        cross[ccy] = rate
            quote_rate, base_rate = conversion_rates(acct.currency, self.instrument, tick.mid, cross)

        mine = experiment_controls(controls, self.slug)
        kill_global = controls.get(str(ControlKey.KILL_SWITCH)) or {}
        kill_exp = mine.get(str(ControlKey.KILL_SWITCH)) or {}
        kill = kill_global if kill_global.get("active") else kill_exp
        daily = mine.get(str(ControlKey.DAILY_LOSS_BREAKER)) or {}
        dd = mine.get(str(ControlKey.DRAWDOWN_BREAKER)) or {}
        day_nav = mine.get(str(ControlKey.DAY_START_NAV)) or {}
        peak = mine.get(str(ControlKey.PEAK_NAV)) or {}
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
            stream_connected=market.state.stream_connected,
            bid=tick.bid,
            ask=tick.ask,
            price_time=tick.time,
            tradeable=tick.tradeable and market.state.broker_tradeable,
            instrument=market.info,
            account_currency=acct.currency if acct else "",
            nav=eq.equity if eq else None,
            balance=eq.balance if eq else None,
            margin_available=acct.margin_available if acct else None,
            account_state_time=acct.fetched_at if acct else None,
            day_start_nav=Decimal(str(day_nav["value"])) if day_nav.get("value") else None,
            peak_nav=Decimal(str(peak["value"])) if peak.get("value") else None,
            quote_home_rate=quote_rate,
            base_home_rate=base_rate,
            open_trades=list(open_trades.values()),
            other_positions_on_instrument=others,
            account_nav=acct.nav if acct else None,
            unresolved_orders=int(unresolved or 0),
            last_entry_same_direction_at=last_entry,
            news_blackout=news.blackout,
            news_blackout_titles=[e["title"] for e in news.blackout_events],
            calendar_fresh=news.calendar_fresh,
        )


def evaluate_strategy(name: str, tech: TechnicalState, bid: float, ask: float, settings: Settings) -> StrategyResult:
    """Run an experiment's deterministic strategy (looked up at call time, so tests can patch them)."""
    if name == "range_breakout":
        return evaluate_range_breakout(tech, bid, ask, settings)
    if name == "trend_pullback":
        return evaluate_trend_pullback(tech, bid, ask, settings)
    if name == "trend_following":
        return evaluate_trend_following(tech, bid, ask, settings)
    if name == "london_breakout":
        return evaluate_session_breakout(tech, bid, ask, settings)
    raise ValueError(f"unknown strategy {name!r}")


def _setup_message(instrument: str, strategy: StrategyResult, outcome: DecisionOutcome) -> str:
    """Telegram text for a deterministic setup: the plan and what the model made of it."""
    plan = strategy.trade_plan
    lines = [f"{strategy.direction or '?'} {instrument} · {strategy.setup}"]
    if plan:
        lines.append(
            f"Entry {plan.entry:g} · SL {plan.stop_loss:g} ({plan.risk_pips:.1f} pips) · "
            f"TP {plan.take_profit:g} · R:R {plan.risk_reward:.1f}"
        )
    conf = "" if outcome.confidence is None else f" ({outcome.confidence:.0%})"
    if outcome.is_trade:
        lines.append(f"Model: {outcome.decision}{conf} → checking risk")
    elif outcome.source == "PREFILTER":
        lines.append(f"No trade: {', '.join(outcome.reason_codes)}")
    else:
        lines.append(f"Model: {outcome.decision}{conf}, no trade ({', '.join(outcome.reason_codes) or 'no reason given'})")
    return "\n".join(lines)


# ---------------------------------------------------------------------- process


async def wait_for_config_change(
    base: Settings, version: int, reason: str, shutdown: asyncio.Event, timeout: float | None = None
) -> None:
    """Publish a "waiting" heartbeat (shown on the dashboard) until the configuration changes."""
    log.warning("engine waiting: %s", reason)
    db = Database(base)
    try:
        await Notifier(db, None).warning(COMPONENT, "ENGINE_WAITING", f"Engine not trading: {reason}")
        started = utcnow()
        while not shutdown.is_set():
            try:
                async with db.session() as s:
                    await set_control(
                        s, ControlKey.ENGINE_HEARTBEAT,
                        {"at": utcnow().isoformat(), "state": "WAITING", "reason": reason,
                         "mode": base.trading_mode.value, "config_version": version, "experiments": {}},
                        COMPONENT,
                    )
                    if await get_version(s) != version:
                        return
            except Exception:
                log.exception("waiting heartbeat failed")
            if timeout is not None and (utcnow() - started).total_seconds() >= timeout:
                return
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=CONFIG_POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        await db.dispose()


async def run_once(base: Settings, shutdown: asyncio.Event) -> None:
    """Load the configuration and run the engine until shutdown or a configuration change."""
    db = Database(base)
    try:
        await auto_create_tables(db, base)
        imported = await import_env_once(db, base)
        if imported:
            log.warning("Copied %d settings from the environment into the database", len(imported))
        config = await load_configuration(db, base)
    except Exception as exc:
        log.exception("could not load the configuration")
        await db.dispose()
        await wait_for_config_change(base, -1, f"configuration could not be loaded: {exc!r}", shutdown, timeout=60)
        return
    problem = startup_problem(config)
    if problem:
        await db.dispose()
        await wait_for_config_change(base, config.version, problem, shutdown)
        return
    try:
        engine = TradingEngine(config.settings, configuration=config, db=db, watch_config=True)
    except Exception as exc:  # e.g. two experiments mapping one pair to different markets
        log.exception("engine could not be built")
        await db.dispose()
        await wait_for_config_change(base, config.version, f"configuration problem: {exc}", shutdown)
        return
    stop = asyncio.Event()
    relay = asyncio.create_task(shutdown.wait())
    relay.add_done_callback(lambda _: stop.set())
    try:
        await engine.run(stop)
    except Exception as exc:
        log.exception("engine failed")
        if not shutdown.is_set():
            # e.g. the broker refused the login: wait for a fix in the config, retry every minute.
            await wait_for_config_change(base, config.version, f"engine failed: {exc!r}", shutdown, timeout=60)
    finally:
        relay.cancel()


async def main() -> None:
    base = get_settings()
    setup_logging(base.log_level)
    base.require_database()
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown.set)
        except NotImplementedError:  # pragma: no cover (Windows)
            pass
    while not shutdown.is_set():
        await run_once(base, shutdown)


if __name__ == "__main__":
    asyncio.run(main())
