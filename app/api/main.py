"""FastAPI control plane and mobile dashboard.

The API only reads the database and writes control flags and configuration. It talks to the broker
only to download candles for a backtest (``/backtest``); the engine's executor remains the only
component that submits orders (a "flatten" request is a flag the engine picks up).

Experiment pages take ``?experiment=<slug>`` (default: the first enabled experiment). The config
page (``/config``) additionally needs CONFIG_PASSWORD, which unlocks it for a while.

Run with ``uvicorn app.api.main:app --host 0.0.0.0 --port 8000``.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from bisect import bisect_left
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import Date, cast, desc, func, select

from app.api import data as data_api
from app.api import history
from app.backtest import BacktestBusy, BacktestJobs, job_params
from app.config import store
from app.config.settings import Settings, get_settings
from app.config.store import Configuration, ExperimentConfig, ExperimentInput
from app.db.bootstrap import auto_create_tables
from app.db.control import experiment_controls, get_all_controls, scoped, set_control
from app.db.control import reset_breaker as reset_breaker_control
from app.db.enums import ControlKey, TradeState
from app.db.models import (
    AccountSnapshot,
    AnalysisReport,
    Decision,
    DecisionRequest,
    NewsEvent,
    NewsInterpretation,
    Order,
    RiskCheck,
    SystemEvent,
    Trade,
)
from app.db.session import Database
from app.experiments import excursion
from app.market_data.timeutil import utcnow

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
HEARTBEAT_MAX_AGE_SECONDS = 60
CSRF_HEADER_VALUE = "tradlysis"
CONFIG_CACHE_SECONDS = 3.0
CONFIG_UNLOCK_SECONDS = 15 * 60
CONFIG_MAX_FAILED_UNLOCKS = 5  # per CONFIG_UNLOCK_LOCKOUT_SECONDS
CONFIG_UNLOCK_LOCKOUT_SECONDS = 5 * 60

security = HTTPBasic(auto_error=False)


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = Field(default="", max_length=200)
    experiment: str | None = None  # None: every experiment (the account-wide switch)


class ReasonBody(BaseModel):
    reason: str = Field(default="", max_length=200)
    experiment: str | None = None  # None: every position on the account


class BacktestBody(BaseModel):
    experiments: list[str] = Field(default_factory=list, max_length=20)  # empty: every enabled experiment
    days: int = 90
    set: dict[str, str] = Field(default_factory=dict)  # setting -> value for this run
    vary: dict[str, str] = Field(default_factory=dict)  # setting -> "v1,v2" to compare
    model: bool = False  # also ask the decision model about every trade (OpenRouter calls)


# Experiment settings the backtest page offers to change: the ones the rules and the risk engine use
# (not the model, news or execution settings, which a backtest does not simulate).
BACKTEST_GROUPS = ("Strategy", "Trade filters", "Risk")
BACKTEST_SKIP = {"min_decision_confidence", "max_slippage_pips", "max_margin_usage_pct"}


class UnlockBody(BaseModel):
    password: str = Field(max_length=500)


class GlobalConfigBody(BaseModel):
    # key -> new value; null removes the stored value (back to the ENV or default value)
    values: dict[str, str | None]


class ExperimentBody(BaseModel):
    slug: str | None = Field(default=None, max_length=63)  # creating only
    name: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    instrument: str | None = Field(default=None, max_length=20)
    strategy: str | None = Field(default=None, max_length=40)
    enabled: bool | None = None
    capital: str | None = Field(default=None, max_length=40)
    settings: dict[str, str | None] | None = None

    def to_input(self) -> ExperimentInput:
        return ExperimentInput(
            name=self.name, description=self.description, instrument=self.instrument, strategy=self.strategy,
            enabled=self.enabled, capital=self.capital, settings=self.settings,
        )


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or Database(settings)
        if db is None:
            try:
                await auto_create_tables(app.state.db, settings)
                await store.import_env_once(app.state.db, settings)
            except Exception:
                log.exception("could not check/create database tables or import the configuration")
        yield
        await app.state.db.dispose()

    app = FastAPI(title="Tradlysis control plane", version="0.1.0", lifespan=lifespan)
    # Shared CSS/JS only (no data); every page and API route stays behind authentication.
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")
    app.state.settings = settings

    def get_db(request: Request) -> Database:
        return request.app.state.db

    def require_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
        if not settings.dashboard_password:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "DASHBOARD_PASSWORD is not configured; the API is locked.",
            )
        ok = credentials is not None and (
            secrets.compare_digest(credentials.username.encode(), settings.dashboard_username.encode())
            and secrets.compare_digest(credentials.password.encode(), settings.dashboard_password.encode())
        )
        if not ok:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "Invalid credentials",
                headers={"WWW-Authenticate": 'Basic realm="tradlysis"'},
            )
        return credentials.username  # type: ignore[union-attr]

    def require_csrf(x_requested_with: str | None = Header(default=None)) -> None:
        # Browsers cannot attach custom headers to cross-site form posts, so requiring one
        # stops CSRF against the control endpoints (basic-auth credentials are sent automatically).
        if x_requested_with != CSRF_HEADER_VALUE:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing X-Requested-With header")

    # ------------------------------------------------------------------ configuration & experiments

    config_cache: dict[str, Any] = {"at": 0.0, "value": None}
    config_lock = asyncio.Lock()

    async def load_config(database: Database, *, fresh: bool = False) -> Configuration:
        async with config_lock:
            if fresh or config_cache["value"] is None or time.monotonic() - config_cache["at"] > CONFIG_CACHE_SECONDS:
                config = await store.load_configuration(database, settings)
                if not config.experiments:
                    # Nothing stored yet (e.g. a database the engine never started on): the ENV's experiment.
                    config.experiments = store.single_experiment(config.settings).experiments
                config_cache.update(at=time.monotonic(), value=config)
            return config_cache["value"]

    async def get_config(database: Database = Depends(get_db)) -> Configuration:
        return await load_config(database)

    # Backtests (dashboard page and data API share them: one runs at a time).
    backtests = BacktestJobs()
    app.state.backtests = backtests

    # Read-only data API for analysis tools (bearer token, not the dashboard login).
    app.include_router(data_api.build_router(settings, get_db, load_config, backtests))

    async def get_experiment(
        experiment: str | None = Query(None, max_length=63), config: Configuration = Depends(get_config)
    ) -> ExperimentConfig:
        slug = experiment or config.default_slug()
        exp = config.get(slug or "")
        if exp is None:
            raise HTTPException(404, f"unknown experiment {experiment!r}")
        return exp

    unlock_tokens: dict[str, tuple[str, float]] = {}
    failed_unlocks: list[float] = []

    def require_config_unlock(
        user: str = Depends(require_auth), x_config_token: str | None = Header(default=None)
    ) -> str:
        if not settings.config_password:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CONFIG_PASSWORD is not set; the config page is locked.")
        now = time.monotonic()
        entry = unlock_tokens.get(x_config_token or "")
        if entry is None or entry[1] < now or entry[0] != user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Config page locked", headers={"X-Config-Locked": "1"})
        unlock_tokens[x_config_token or ""] = (user, now + CONFIG_UNLOCK_SECONDS)  # sliding expiry
        return user

    # ------------------------------------------------------------------ public

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def dashboard(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/experiments", include_in_schema=False)
    async def experiments_page(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "experiments.html", headers={"Cache-Control": "no-store"})

    @app.get("/analysis", include_in_schema=False)
    async def analysis_page(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "analysis.html", headers={"Cache-Control": "no-store"})

    @app.get("/backtest", include_in_schema=False)
    async def backtest_page(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "backtest.html", headers={"Cache-Control": "no-store"})

    @app.get("/history", include_in_schema=False)
    @app.get("/history/{item_id}", include_in_schema=False)
    async def history_page(item_id: str | None = None, _: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "history.html", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ history

    @app.get("/api/history")
    async def get_history(
        filter: str = Query("all", pattern="^(all|traded|stopped)$"),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        async with database.session() as s:
            chains = await history.load_chains(s, exp.slug)
            decided = [c.request.candle_time + history.DECISION_BAR for c in chains if c.request is not None]
            bars = (
                await history.load_bars(s, exp.instrument, min(decided), max(decided) + history.HYPOTHETICAL_HORIZON)
                if decided
                else []
            )
        bar_times = [b.time for b in bars]
        items = []
        for c in chains:
            hypo = None
            if c.request is not None and c.trade is None and c.request.trade_plan:
                start = c.request.candle_time + history.DECISION_BAR
                hypo = history.hypothetical_outcome(c.request.trade_plan, start, bars[bisect_left(bar_times, start):])
            items.append(history.item_summary(c, hypo))
        items.sort(key=lambda i: i["time"] or "", reverse=True)

        stopped = [i for i in items if i["category"] == "STOPPED"]
        summary = {
            "taken": sum(1 for i in items if i["category"] == "TRADED"),
            "won": sum(1 for i in items if i["outcome"] == "WON"),
            "lost": sum(1 for i in items if i["outcome"] == "LOST"),
            "open": sum(1 for i in items if i["outcome"] == "OPEN"),
            "stopped": len(stopped),
            "stopped_would_win": sum(1 for i in stopped if (i["hypothetical"] or {}).get("result") == "WOULD_WIN"),
            "stopped_would_lose": sum(1 for i in stopped if (i["hypothetical"] or {}).get("result") == "WOULD_LOSE"),
            "stopped_by": dict(Counter(i["outcome"] for i in stopped)),
        }
        if filter == "traded":
            items = [i for i in items if i["category"] == "TRADED"]
        elif filter == "stopped":
            items = stopped
        return {"summary": summary, "total": len(items), "items": items[offset : offset + limit]}

    @app.get("/api/history/{item_id}")
    async def get_history_item(
        item_id: str, _: str = Depends(require_auth), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        async with database.session() as s:
            chain = await history.load_chain(s, item_id)
            if chain is None:
                raise HTTPException(404, "not found")
            req, t = chain.request, chain.trade
            start = (req.candle_time + history.DECISION_BAR) if req else t.open_time  # type: ignore[union-attr]
            end = start + history.HYPOTHETICAL_HORIZON
            if t is not None and t.close_time is not None:
                end = t.close_time
            elif t is not None:
                end = utcnow()
            instrument = req.instrument if req else t.instrument  # type: ignore[union-attr]
            bars = await history.load_bars(s, instrument, start - timedelta(hours=3), end + timedelta(hours=2))
            if t is not None:
                ex = await excursion.trade_excursion(s, t, utcnow())
                chain.excursion = ex.to_dict() if ex else None
        hypo = None
        if req is not None and t is None and req.trade_plan:
            hypo = history.hypothetical_outcome(req.trade_plan, start, [b for b in bars if b.time >= start])
            if hypo and hypo.get("resolved_at"):
                cut = datetime.fromisoformat(hypo["resolved_at"]) + timedelta(hours=2)
                bars = [b for b in bars if b.time <= cut]
        plan = (req.trade_plan if req else None) or {}
        levels = {
            "entry": (t.open_price if t else plan.get("entry")),
            "stop_loss": (t.stop_loss if t and t.stop_loss else plan.get("stop_loss")),
            "take_profit": (t.take_profit if t and t.take_profit else plan.get("take_profit")),
            "exit": t.close_price if t else None,
        }
        markers = {
            "decision": start.isoformat(),
            "open": t.open_time.isoformat() if t else None,
            "close": t.close_time.isoformat() if t and t.close_time else None,
            "hypothetical_resolved": (hypo or {}).get("resolved_at"),
        }
        return _jsonable(
            {
                "item": history.item_summary(chain, hypo),
                "timeline": history.build_timeline(chain, hypo),
                "price_path": [{"t": b.time.isoformat(), "h": b.high, "l": b.low, "c": b.close} for b in bars[-400:]],
                "levels": levels,
                "markers": markers,
            }
        )

    # ------------------------------------------------------------------ status

    @app.get("/api/status")
    async def get_status(
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        config: Configuration = Depends(get_config),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """Engine and control state, seen from one experiment (``controls`` merges its own flags
        over the account-wide kill switch; ``experiment_status`` is its part of the heartbeat)."""
        async with database.session() as s:
            all_controls = await get_all_controls(s)
        hb = all_controls.get(str(ControlKey.ENGINE_HEARTBEAT))
        engine_alive = False
        hb_age = None
        if hb and hb.get("at"):
            hb_age = (utcnow() - datetime.fromisoformat(hb["at"])).total_seconds()
            engine_alive = hb_age <= HEARTBEAT_MAX_AGE_SECONDS
        mine = experiment_controls(all_controls, exp.slug)
        global_kill = all_controls.get(str(ControlKey.KILL_SWITCH)) or {}
        own_kill = mine.get(str(ControlKey.KILL_SWITCH)) or {}
        controls = {
            **mine,
            "kill_switch": global_kill if global_kill.get("active") else own_kill,
            "global_kill_switch": global_kill,
            "experiment_kill_switch": own_kill,
        }
        engine_exp = ((hb or {}).get("experiments") or {}).get(exp.slug)
        return {
            "now": utcnow().isoformat(),
            "mode": settings.trading_mode.value,
            "experiment": exp.slug,
            "experiment_info": exp.summary(),
            "experiments": [e.summary() for e in config.experiments],
            "engine_alive": engine_alive,
            "engine_state": (hb or {}).get("state", "RUNNING") if hb else None,
            "engine_reason": (hb or {}).get("reason"),
            "running": engine_exp is not None,
            "heartbeat_age_seconds": None if hb_age is None else round(hb_age, 1),
            "engine": hb,
            "experiment_status": engine_exp,
            "market": ((hb or {}).get("markets") or {}).get(exp.instrument),
            "controls": controls,
            "config_version": config.version,
            "config_pending": bool(hb and hb.get("config_version") not in (None, config.version)),
        }

    @app.get("/api/experiments")
    async def experiments_overview(
        _: str = Depends(require_auth),
        config: Configuration = Depends(get_config),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """Every experiment side by side: equity, results, open trades and state."""
        slugs = [e.slug for e in config.experiments]
        async with database.session() as s:
            all_controls = await get_all_controls(s)
            rows = (
                await s.execute(
                    select(
                        Trade.experiment,
                        func.count().filter(Trade.state == str(TradeState.CLOSED)),
                        func.count().filter(Trade.state == str(TradeState.CLOSED), Trade.r_multiple > 0),
                        func.coalesce(func.sum(Trade.r_multiple).filter(Trade.state == str(TradeState.CLOSED)), 0),
                        func.coalesce(func.sum(Trade.realized_pl).filter(Trade.state == str(TradeState.CLOSED)), 0),
                        func.count().filter(Trade.state == str(TradeState.OPEN)),
                        func.coalesce(func.sum(Trade.unrealized_pl).filter(Trade.state == str(TradeState.OPEN)), 0),
                    )
                    .where(Trade.experiment.in_(slugs))
                    .group_by(Trade.experiment)
                )
            ).all()
            last = dict(
                (
                    await s.execute(
                        select(DecisionRequest.experiment, func.max(DecisionRequest.candle_time))
                        .where(DecisionRequest.experiment.in_(slugs))
                        .group_by(DecisionRequest.experiment)
                    )
                ).all()
            )
        stats = {r[0]: r[1:] for r in rows}
        hb = all_controls.get(str(ControlKey.ENGINE_HEARTBEAT)) or {}
        alive = bool(hb.get("at")) and (utcnow() - datetime.fromisoformat(hb["at"])).total_seconds() <= HEARTBEAT_MAX_AGE_SECONDS
        global_kill = bool((all_controls.get(str(ControlKey.KILL_SWITCH)) or {}).get("active"))
        out = []
        for e in config.experiments:
            closed, wins, total_r, pl, n_open, upl = stats.get(e.slug, (0, 0, 0, 0, 0, 0))
            mine = experiment_controls(all_controls, e.slug)
            running = alive and e.slug in (hb.get("experiments") or {})
            eq = ((hb.get("experiments") or {}).get(e.slug) or {}).get("equity") if running else None
            halted = (
                "Kill switch on" if global_kill or (mine.get("kill_switch") or {}).get("active")
                else "Daily limit hit" if (mine.get("daily_loss_breaker") or {}).get("tripped")
                else "Drawdown limit hit" if (mine.get("drawdown_breaker") or {}).get("tripped")
                else None
            )
            out.append({
                **e.summary(),
                "running": running,
                "halted": halted,
                "equity": eq,
                "closed_trades": closed,
                "wins": wins,
                "win_rate": round(wins / closed, 3) if closed else None,
                "total_r": round(float(total_r), 2),
                "realized_pl": round(float(pl), 2),
                "open_trades": n_open,
                "unrealized_pl": round(float(upl), 2),
                "last_decision": last[e.slug].isoformat() if last.get(e.slug) else None,
                "risk_per_trade_pct": e.settings.risk_per_trade_pct,
                "min_risk_reward": e.settings.min_risk_reward,
                "model": e.settings.openrouter_model,
            })
        return {"experiments": out, "engine_alive": alive, "account": hb.get("account")}

    @app.get("/api/account")
    async def get_account(
        hours: int = Query(168, ge=1, le=24 * 365),
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """The shared broker account (``latest``, ``series``) and the experiment's own equity:
        capital plus realized P/L after each closed trade (``equity_series``), plus open P/L now."""
        since = utcnow() - timedelta(hours=hours)
        async with database.session() as s:
            rows = (
                await s.scalars(
                    select(AccountSnapshot).where(AccountSnapshot.taken_at >= since).order_by(AccountSnapshot.taken_at)
                )
            ).all()
            closed = (
                await s.execute(
                    select(Trade.close_time, Trade.realized_pl)
                    .where(Trade.experiment == exp.slug, Trade.state == str(TradeState.CLOSED),
                           Trade.close_time.is_not(None))
                    .order_by(Trade.close_time)
                )
            ).all()
            open_pl = await s.scalar(
                select(func.coalesce(func.sum(Trade.unrealized_pl), 0))
                .where(Trade.experiment == exp.slug, Trade.state == str(TradeState.OPEN))
            )
        equity = None
        if exp.capital is not None:
            balance = start_balance = exp.capital
            points = []
            for close_time, pl in closed:
                if close_time < since:
                    start_balance += pl or 0
                balance += pl or 0
                if close_time >= since:
                    points.append({"t": close_time.isoformat(), "balance": float(balance)})
            realized = sum((pl or 0 for _, pl in closed), start=exp.capital * 0)
            equity = {
                "capital": float(exp.capital),
                "realized_pl": float(realized),
                "unrealized_pl": float(open_pl or 0),
                "balance": float(exp.capital + realized),
                "equity": float(exp.capital + realized + (open_pl or 0)),
                "start": {"t": since.isoformat(), "balance": float(start_balance)},
                "series": points,
            }
        step = max(1, len(rows) // 300)
        series = [
            {"t": r.taken_at.isoformat(), "nav": float(r.nav), "balance": float(r.balance)}
            for r in rows[::step]
        ]
        if rows and (not series or series[-1]["t"] != rows[-1].taken_at.isoformat()):
            series.append({"t": rows[-1].taken_at.isoformat(), "nav": float(rows[-1].nav), "balance": float(rows[-1].balance)})
        latest = rows[-1] if rows else None
        return {
            "experiment": equity,
            "latest": None
            if latest is None
            else {
                "taken_at": latest.taken_at.isoformat(),
                "currency": latest.currency,
                "balance": float(latest.balance),
                "nav": float(latest.nav),
                "unrealized_pl": float(latest.unrealized_pl),
                "margin_available": float(latest.margin_available),
                "open_trade_count": latest.open_trade_count,
            },
            "series": series,
        }

    # ------------------------------------------------------------------ trades & decisions

    @app.get("/api/trades")
    async def get_trades(
        state: str | None = Query(None, pattern="^(OPEN|CLOSED)$"),
        limit: int = Query(50, ge=1, le=500),
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        # The experiment's trades, plus positions no experiment owns (opened by hand on the account).
        q = (
            select(Trade)
            .where((Trade.experiment == exp.slug) | Trade.experiment.is_(None))
            .order_by(desc(Trade.open_time))
            .limit(limit)
        )
        if state:
            q = q.where(Trade.state == state)
        now = utcnow()
        async with database.session() as s:
            rows = (await s.scalars(q)).all()
            close_reasons = await excursion.bot_close_reasons(s)
            measured = {t.id: await excursion.trade_excursion(s, t, now) for t in rows}
        return [
            {
                "id": t.id,
                "experiment": t.experiment,
                "broker_trade_id": t.broker_trade_id,
                "instrument": t.instrument,
                "direction": t.direction,
                "units": t.initial_units,
                "current_units": t.current_units,
                "open_price": t.open_price,
                "open_time": t.open_time.isoformat(),
                "stop_loss": t.stop_loss,
                "take_profit": t.take_profit,
                "state": t.state,
                "close_price": t.close_price,
                "close_time": t.close_time.isoformat() if t.close_time else None,
                "close_reason": t.close_reason,
                "realized_pl": float(t.realized_pl) if t.realized_pl is not None else None,
                "unrealized_pl": float(t.unrealized_pl) if t.unrealized_pl is not None else None,
                "r_multiple": t.r_multiple,
                "unexpected": t.unexpected,
                "outcome": excursion.close_category(t, close_reasons),
                "excursion": measured[t.id].to_dict() if measured.get(t.id) else None,
            }
            for t in rows
        ]

    @app.get("/api/decisions")
    async def get_decisions(
        limit: int = Query(50, ge=1, le=500),
        only_llm: bool = False,
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        q = (
            select(DecisionRequest, Decision, RiskCheck)
            .outerjoin(Decision, Decision.request_id == DecisionRequest.id)
            .outerjoin(RiskCheck, RiskCheck.request_id == DecisionRequest.id)
            .where(DecisionRequest.experiment == exp.slug)
            .order_by(desc(DecisionRequest.candle_time))
            .limit(limit)
        )
        if only_llm:
            q = q.where(DecisionRequest.llm_called.is_(True))
        async with database.session() as s:
            rows = (await s.execute(q)).all()
        out = []
        for req, dec, rc in rows:
            strat = req.strategy_result or {}
            out.append(
                {
                    "id": req.id,
                    "candle_time": req.candle_time.isoformat(),
                    "candidate": bool(strat.get("candidate")),
                    "direction": strat.get("direction"),
                    "failure_codes": strat.get("failure_codes", []),
                    "llm_called": req.llm_called,
                    "decision": dec.decision if dec else None,
                    "source": dec.source if dec else None,
                    "confidence": dec.confidence if dec else None,
                    "reason_codes": (dec.reason_codes or []) if dec else [],
                    "risk_approved": rc.approved if rc else None,
                    "rejection_reasons": (rc.rejection_reasons or []) if rc else [],
                    "spread_pips": (req.snapshot or {}).get("price", {}).get("spread_pips"),
                    "news_risk": (req.snapshot or {}).get("news", {}).get("risk"),
                }
            )
        return out

    @app.get("/api/decisions/{request_id}")
    async def get_decision_detail(
        request_id: int, _: str = Depends(require_auth), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        async with database.session() as s:
            req = await s.get(DecisionRequest, request_id)
            if req is None:
                raise HTTPException(404, "not found")
            dec = await s.scalar(select(Decision).where(Decision.request_id == request_id))
            rc = await s.scalar(select(RiskCheck).where(RiskCheck.request_id == request_id))
            order = await s.scalar(select(Order).where(Order.risk_check_id == rc.id)) if rc else None
        return _jsonable(
            {
                "request": _row(req),
                "decision": _row(dec) if dec else None,
                "risk_check": _row(rc) if rc else None,
                "order": _row(order) if order else None,
            }
        )

    # ------------------------------------------------------------------ events, news, analysis

    @app.get("/api/events")
    async def get_events(
        limit: int = Query(100, ge=1, le=1000),
        min_level: str = Query("INFO", pattern="^(INFO|WARNING|ERROR|CRITICAL)$"),
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        """The experiment's events and account-wide ones (other experiments' events are left out)."""
        levels = ["INFO", "WARNING", "ERROR", "CRITICAL"]
        allowed = levels[levels.index(min_level) :]
        tag = SystemEvent.details["experiment"].astext
        async with database.session() as s:
            rows = (
                await s.scalars(
                    select(SystemEvent)
                    .where(SystemEvent.level.in_(allowed), (tag == exp.slug) | tag.is_(None))
                    .order_by(desc(SystemEvent.created_at))
                    .limit(limit)
                )
            ).all()
        return [
            {
                "id": e.id,
                "created_at": e.created_at.isoformat(),
                "level": e.level,
                "component": e.component,
                "event_type": e.event_type,
                "message": e.message,
                "experiment": (e.details or {}).get("experiment"),
            }
            for e in rows
        ]

    @app.get("/api/news")
    async def get_news(
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        now = utcnow()
        base, quote = exp.settings.instrument_currencies
        async with database.session() as s:
            events = (
                await s.scalars(
                    select(NewsEvent)
                    .where(
                        NewsEvent.event_time >= now - timedelta(hours=12),
                        NewsEvent.event_time <= now + timedelta(hours=48),
                        NewsEvent.currency.in_([base, quote, "ALL"]),
                        NewsEvent.impact.in_(["HIGH", "MEDIUM"]),
                    )
                    .order_by(NewsEvent.event_time)
                )
            ).all()
            interps = (
                await s.scalars(
                    select(NewsInterpretation)
                    .where(NewsInterpretation.currency.in_([base, quote]))
                    .order_by(desc(NewsInterpretation.created_at))
                    .limit(20)
                )
            ).all()
        return {
            "events": [
                {
                    "time": e.event_time.isoformat(),
                    "currency": e.currency,
                    "impact": e.impact,
                    "title": e.title,
                    "forecast": e.forecast,
                    "previous": e.previous,
                    "actual": e.actual,
                }
                for e in events
            ],
            "interpretations": [
                {
                    "created_at": i.created_at.isoformat(),
                    "currency": i.currency,
                    "tone": i.tone,
                    "bias": i.currency_bias,
                    "confidence": i.confidence,
                    "summary": i.summary,
                    "valid": i.valid,
                }
                for i in interps
            ],
        }

    @app.get("/api/analysis/latest")
    async def get_analysis(
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        async with database.session() as s:
            row = await s.scalar(
                select(AnalysisReport)
                .where(AnalysisReport.experiment == exp.slug)
                .order_by(desc(AnalysisReport.created_at))
                .limit(1)
            )
        if row is None:
            return {"report": None}
        return {"report": {"created_at": row.created_at.isoformat(), "metrics": row.metrics}}

    @app.post("/api/analysis/run", dependencies=[Depends(require_csrf)])
    async def run_analysis(
        _: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """Compute and store a fresh report now (what ``python -m app.experiments.analyzer --once`` does)."""
        from app.experiments.analyzer import run_once

        metrics = await run_once(database, exp.settings, capital=exp.capital)
        return {"report": {"created_at": utcnow().isoformat(), "metrics": metrics}}

    @app.get("/api/analysis/timeline")
    async def analysis_timeline(
        days: int = Query(default=30, ge=1, le=365),
        _: str = Depends(require_auth),
        experiment: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """What the experiment did per UTC day: cycles, setups, model calls, signals, approvals, trades."""
        exp = experiment.slug
        since = utcnow() - timedelta(days=days)

        def utc_day(col):
            return cast(func.timezone("UTC", col), Date).label("day")

        req_day = utc_day(DecisionRequest.candle_time)
        in_scope = (DecisionRequest.experiment == exp, DecisionRequest.candle_time >= since)
        out: dict[str, dict[str, Any]] = {}

        def row(day) -> dict[str, Any]:
            key = day.isoformat()
            return out.setdefault(key, {
                "day": key, "cycles": 0, "setups": 0, "model_calls": 0, "signals": 0, "approved": 0,
                "opened": 0, "closed": 0, "wins": 0, "total_r": 0.0, "realized_pl": 0.0,
            })

        async with database.session() as s:
            for day, cycles, setups, calls in await s.execute(
                select(
                    req_day,
                    func.count(),
                    func.count().filter(DecisionRequest.strategy_result["candidate"].astext == "true"),
                    func.count().filter(DecisionRequest.llm_called.is_(True)),
                ).where(*in_scope).group_by(req_day)
            ):
                r = row(day)
                r.update(cycles=cycles, setups=setups, model_calls=calls)
            for day, signals in await s.execute(
                select(req_day, func.count())
                .join(Decision, Decision.request_id == DecisionRequest.id)
                .where(*in_scope, Decision.decision.in_(["BUY", "SELL"]))
                .group_by(req_day)
            ):
                row(day)["signals"] = signals
            for day, approved in await s.execute(
                select(req_day, func.count())
                .join(RiskCheck, RiskCheck.request_id == DecisionRequest.id)
                .where(*in_scope, RiskCheck.approved.is_(True))
                .group_by(req_day)
            ):
                row(day)["approved"] = approved
            open_day = utc_day(Trade.open_time)
            for day, opened in await s.execute(
                select(open_day, func.count())
                .where(Trade.experiment == exp, Trade.open_time >= since)
                .group_by(open_day)
            ):
                row(day)["opened"] = opened
            close_day = utc_day(Trade.close_time)
            for day, closed, wins, total_r, pl in await s.execute(
                select(
                    close_day,
                    func.count(),
                    func.count().filter(Trade.r_multiple > 0),
                    func.coalesce(func.sum(Trade.r_multiple), 0),
                    func.coalesce(func.sum(Trade.realized_pl), 0),
                )
                .where(Trade.experiment == exp, Trade.close_time.is_not(None), Trade.close_time >= since)
                .group_by(close_day)
            ):
                row(day).update(closed=closed, wins=wins, total_r=round(float(total_r), 3), realized_pl=round(float(pl), 2))
        return {"days": sorted(out.values(), key=lambda r: r["day"], reverse=True)}

    # ------------------------------------------------------------------ controls

    def check_experiment(config: Configuration, slug: str | None) -> None:
        if slug is not None and config.get(slug) is None:
            raise HTTPException(404, f"unknown experiment {slug!r}")

    # ------------------------------------------------------------------ backtests

    @app.get("/api/backtest/options")
    async def backtest_options(
        _: str = Depends(require_auth), config: Configuration = Depends(get_config)
    ) -> dict[str, Any]:
        """What the backtest page offers: experiments, the settings it can change, recent runs."""
        fields = [
            f for f in store.FIELDS
            if f.scope == store.EXPERIMENT_SCOPE and f.group in BACKTEST_GROUPS and f.kind == "number"
            and f.key not in BACKTEST_SKIP
        ]
        return {
            "experiments": [
                {**e.summary(), "values": {f.key: store.display_value(e.settings, f.key) for f in fields}}
                for e in config.experiments
            ],
            "settings": [
                {"key": f.key, "label": f.label, "help": f.help, "group": f.group, "strategies": list(f.strategies)}
                for f in fields
            ],
            "recent": [j.view(result=False) for j in backtests.recent()],
        }

    @app.post("/api/backtest", dependencies=[Depends(require_csrf)])
    async def start_backtest(
        body: BacktestBody,
        _: str = Depends(require_auth),
        config: Configuration = Depends(get_config),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        for slug in body.experiments:
            check_experiment(config, slug)
        try:
            params = job_params(body.experiments, body.days, body.set, body.vary, body.model)
            job = backtests.start(params, lambda: load_config(database, fresh=True))
        except store.ConfigError as exc:
            raise HTTPException(400, str(exc)) from None
        except BacktestBusy as exc:
            raise HTTPException(409, str(exc)) from None
        return data_api._jsonable(job.view(text=False))

    @app.get("/api/backtest/{job_id}")
    async def backtest_job(job_id: str, _: str = Depends(require_auth)) -> dict[str, Any]:
        job = backtests.get(job_id)
        if job is None:
            raise HTTPException(404, "unknown or expired backtest")
        return data_api._jsonable(job.view(text=False))

    @app.post("/api/controls/kill-switch", dependencies=[Depends(require_csrf)])
    async def kill_switch(
        body: KillSwitchBody,
        user: str = Depends(require_auth),
        config: Configuration = Depends(get_config),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        check_experiment(config, body.experiment)
        value = {"active": body.active, "reason": body.reason or ("dashboard" if body.active else ""), "at": utcnow().isoformat()}
        scope = body.experiment or "all experiments"
        async with database.session() as s:
            await set_control(s, scoped(ControlKey.KILL_SWITCH, body.experiment), value, f"api:{user}")
            s.add(
                SystemEvent(
                    level="WARNING" if body.active else "INFO",
                    component="api",
                    event_type="KILL_SWITCH_ON" if body.active else "KILL_SWITCH_OFF",
                    message=f"Kill switch for {scope} {'activated' if body.active else 'released'} by {user}: {body.reason}",
                    details={"experiment": body.experiment} if body.experiment else {},
                )
            )
        return {"ok": True, "kill_switch": value}

    @app.post("/api/controls/flatten", dependencies=[Depends(require_csrf)])
    async def flatten(
        body: ReasonBody,
        user: str = Depends(require_auth),
        config: Configuration = Depends(get_config),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        check_experiment(config, body.experiment)
        value = {"requested": True, "reason": body.reason or f"dashboard ({user})", "at": utcnow().isoformat()}
        async with database.session() as s:
            await set_control(s, scoped(ControlKey.FLATTEN_REQUEST, body.experiment), value, f"api:{user}")
        return {"ok": True, "flatten_request": value}

    @app.post("/api/controls/reset-breaker/{which}", dependencies=[Depends(require_csrf)])
    async def reset_breaker(
        which: str,
        user: str = Depends(require_auth),
        exp: ExperimentConfig = Depends(get_experiment),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        key = {"daily": ControlKey.DAILY_LOSS_BREAKER, "drawdown": ControlKey.DRAWDOWN_BREAKER}.get(which)
        if key is None:
            raise HTTPException(404, "unknown breaker")
        async with database.session() as s:
            await reset_breaker_control(s, key, user, exp.slug)
            s.add(
                SystemEvent(
                    level="WARNING", component="api", event_type="BREAKER_RESET",
                    message=f"{exp.slug}: {which} breaker reset by {user}", details={"experiment": exp.slug},
                )
            )
        return {"ok": True}

    # ------------------------------------------------------------------ config page

    @app.get("/config", include_in_schema=False)
    async def config_page(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "config.html", headers={"Cache-Control": "no-store"})

    @app.post("/api/config/unlock", dependencies=[Depends(require_csrf)])
    async def unlock_config(body: UnlockBody, user: str = Depends(require_auth)) -> dict[str, Any]:
        if not settings.config_password:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CONFIG_PASSWORD is not set; the config page is locked.")
        now = time.monotonic()
        failed_unlocks[:] = [t for t in failed_unlocks if now - t < CONFIG_UNLOCK_LOCKOUT_SECONDS]
        if len(failed_unlocks) >= CONFIG_MAX_FAILED_UNLOCKS:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many wrong passwords; try again in a few minutes.")
        if not secrets.compare_digest(body.password.encode(), settings.config_password.encode()):
            failed_unlocks.append(now)
            await asyncio.sleep(1.0)
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong config password")
        for token, (_, expires) in list(unlock_tokens.items()):
            if expires < now:
                unlock_tokens.pop(token, None)
        token = secrets.token_urlsafe(32)
        unlock_tokens[token] = (user, now + CONFIG_UNLOCK_SECONDS)
        return {"token": token, "expires_in": CONFIG_UNLOCK_SECONDS}

    @app.post("/api/config/lock", dependencies=[Depends(require_csrf)])
    async def lock_config(x_config_token: str | None = Header(default=None), _: str = Depends(require_auth)) -> dict[str, Any]:
        unlock_tokens.pop(x_config_token or "", None)
        return {"ok": True}

    @app.get("/api/config")
    async def get_configuration(
        _: str = Depends(require_config_unlock), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        config = await load_config(database, fresh=True)
        env_set = settings.model_fields_set

        def field_view(f: store.FieldInfo, value_settings: Settings, stored: dict[str, str], fallback: str) -> dict[str, Any]:
            value = store.display_value(value_settings, f.key)
            source = "app" if f.key in stored else fallback
            return {
                "key": f.key, "env": f.key.upper(), "group": f.group, "label": f.label, "help": f.help,
                "kind": f.kind, "choices": list(f.choices), "secret": f.secret, "source": source,
                "strategies": list(f.strategies),
                # Secrets are never sent back; only whether one is set.
                "value": None if f.secret else value,
                "is_set": bool(value),
            }

        globals_ = [
            field_view(f, config.settings, config.stored, "env" if f.key in env_set else "default")
            for f in store.FIELDS if f.scope == store.GLOBAL_SCOPE
        ]
        exp_fields = [f for f in store.FIELDS if f.scope == store.EXPERIMENT_SCOPE]
        return {
            "version": config.version,
            "mode": settings.trading_mode.value,
            "global": globals_,
            "experiment_fields": [field_view(f, config.settings, {}, "default") for f in exp_fields],
            "experiments": [
                {
                    **e.summary(),
                    "fields": [
                        field_view(f, e.settings, e.overrides, "global")
                        for f in exp_fields
                        if not f.strategies or e.strategy in f.strategies
                    ],
                }
                for e in config.experiments
            ],
            "strategies": store.STRATEGIES,
            "env_only": [k.upper() for k in store.ENV_ONLY_KEYS],
            "env_overridden": sorted(k.upper() for k in env_set & set(config.stored)),
            "changes": await store.recent_changes(database, 30),
        }

    @app.put("/api/config", dependencies=[Depends(require_csrf)])
    async def save_configuration(
        body: GlobalConfigBody, user: str = Depends(require_config_unlock), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        changed = await store.save_global(database, settings, body.values, f"api:{user}")
        await load_config(database, fresh=True)
        return {"ok": True, "changed": changed}

    @app.post("/api/config/experiments", dependencies=[Depends(require_csrf)])
    async def create_experiment(
        body: ExperimentBody, user: str = Depends(require_config_unlock), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        slug = (body.slug or "").strip()
        changed = await store.save_experiment(database, settings, slug, body.to_input(), f"api:{user}", create=True)
        await load_config(database, fresh=True)
        return {"ok": True, "slug": slug, "changed": changed}

    @app.put("/api/config/experiments/{slug}", dependencies=[Depends(require_csrf)])
    async def update_experiment(
        slug: str, body: ExperimentBody, user: str = Depends(require_config_unlock), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        changed = await store.save_experiment(database, settings, slug, body.to_input(), f"api:{user}")
        await load_config(database, fresh=True)
        return {"ok": True, "changed": changed}

    @app.exception_handler(ValueError)
    async def value_error_handler(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    return app


def _row(obj: Any) -> dict[str, Any]:
    return {c.key: getattr(obj, c.key) for c in obj.__mapper__.column_attrs}


def _jsonable(value: Any) -> Any:
    import json

    return json.loads(json.dumps(value, default=str))


# Settings are read at import; the database connects on startup (lifespan).
app = create_app()
