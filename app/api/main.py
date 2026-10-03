"""FastAPI control plane and mobile dashboard.

The API only reads the database and writes control flags. It never talks to the broker:
the engine's executor remains the only component that submits orders (a "flatten" request
is a flag the engine picks up).

Run with ``uvicorn app.api.main:app --host 0.0.0.0 --port 8000``.
"""

from __future__ import annotations

import logging
import secrets
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

from app.api import history
from app.config.settings import Settings, get_settings
from app.db.bootstrap import auto_create_tables
from app.db.control import get_all_controls, set_control
from app.db.enums import ControlKey
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

security = HTTPBasic(auto_error=False)


class KillSwitchBody(BaseModel):
    active: bool
    reason: str = Field(default="", max_length=200)


class ReasonBody(BaseModel):
    reason: str = Field(default="", max_length=200)


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = db or Database(settings)
        if db is None:
            try:
                await auto_create_tables(app.state.db, settings)
            except Exception:
                log.exception("could not check/create database tables")
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

    # ------------------------------------------------------------------ public

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @app.get("/", include_in_schema=False)
    async def dashboard(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/analysis", include_in_schema=False)
    async def analysis_page(_: str = Depends(require_auth)) -> FileResponse:
        return FileResponse(STATIC_DIR / "analysis.html", headers={"Cache-Control": "no-store"})

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
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        async with database.session() as s:
            chains = await history.load_chains(s, settings.experiment_name)
            decided = [c.request.candle_time + history.DECISION_BAR for c in chains if c.request is not None]
            bars = (
                await history.load_bars(s, settings.instrument, min(decided), max(decided) + history.HYPOTHETICAL_HORIZON)
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
            bars = await history.load_bars(s, settings.instrument, start - timedelta(hours=3), end + timedelta(hours=2))
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
    async def get_status(_: str = Depends(require_auth), database: Database = Depends(get_db)) -> dict[str, Any]:
        async with database.session() as s:
            controls = await get_all_controls(s)
        hb = controls.pop(str(ControlKey.ENGINE_HEARTBEAT), None)
        engine_alive = False
        hb_age = None
        if hb and hb.get("at"):
            hb_age = (utcnow() - datetime.fromisoformat(hb["at"])).total_seconds()
            engine_alive = hb_age <= HEARTBEAT_MAX_AGE_SECONDS
        return {
            "now": utcnow().isoformat(),
            "mode": settings.trading_mode.value,
            "experiment": settings.experiment_name,
            "engine_alive": engine_alive,
            "heartbeat_age_seconds": None if hb_age is None else round(hb_age, 1),
            "engine": hb,
            "controls": controls,
        }

    @app.get("/api/account")
    async def get_account(
        hours: int = Query(168, ge=1, le=24 * 365),
        _: str = Depends(require_auth),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        since = utcnow() - timedelta(hours=hours)
        async with database.session() as s:
            rows = (
                await s.scalars(
                    select(AccountSnapshot).where(AccountSnapshot.taken_at >= since).order_by(AccountSnapshot.taken_at)
                )
            ).all()
        step = max(1, len(rows) // 300)
        series = [
            {"t": r.taken_at.isoformat(), "nav": float(r.nav), "balance": float(r.balance)}
            for r in rows[::step]
        ]
        if rows and (not series or series[-1]["t"] != rows[-1].taken_at.isoformat()):
            series.append({"t": rows[-1].taken_at.isoformat(), "nav": float(rows[-1].nav), "balance": float(rows[-1].balance)})
        latest = rows[-1] if rows else None
        return {
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
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        q = select(Trade).order_by(desc(Trade.open_time)).limit(limit)
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
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        q = (
            select(DecisionRequest, Decision, RiskCheck)
            .outerjoin(Decision, Decision.request_id == DecisionRequest.id)
            .outerjoin(RiskCheck, RiskCheck.request_id == DecisionRequest.id)
            .where(DecisionRequest.experiment == settings.experiment_name)
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
        database: Database = Depends(get_db),
    ) -> list[dict[str, Any]]:
        levels = ["INFO", "WARNING", "ERROR", "CRITICAL"]
        allowed = levels[levels.index(min_level) :]
        async with database.session() as s:
            rows = (
                await s.scalars(
                    select(SystemEvent)
                    .where(SystemEvent.level.in_(allowed))
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
            }
            for e in rows
        ]

    @app.get("/api/news")
    async def get_news(_: str = Depends(require_auth), database: Database = Depends(get_db)) -> dict[str, Any]:
        now = utcnow()
        base, quote = settings.instrument_currencies
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
                    select(NewsInterpretation).order_by(desc(NewsInterpretation.created_at)).limit(20)
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
    async def get_analysis(_: str = Depends(require_auth), database: Database = Depends(get_db)) -> dict[str, Any]:
        async with database.session() as s:
            row = await s.scalar(
                select(AnalysisReport)
                .where(AnalysisReport.experiment == settings.experiment_name)
                .order_by(desc(AnalysisReport.created_at))
                .limit(1)
            )
        if row is None:
            return {"report": None}
        return {"report": {"created_at": row.created_at.isoformat(), "metrics": row.metrics}}

    @app.post("/api/analysis/run", dependencies=[Depends(require_csrf)])
    async def run_analysis(_: str = Depends(require_auth), database: Database = Depends(get_db)) -> dict[str, Any]:
        """Compute and store a fresh report now (what ``python -m app.experiments.analyzer --once`` does)."""
        from app.experiments.analyzer import run_once

        metrics = await run_once(database, settings)
        return {"report": {"created_at": utcnow().isoformat(), "metrics": metrics}}

    @app.get("/api/analysis/timeline")
    async def analysis_timeline(
        days: int = Query(default=30, ge=1, le=365),
        _: str = Depends(require_auth),
        database: Database = Depends(get_db),
    ) -> dict[str, Any]:
        """What the experiment did per UTC day: cycles, setups, model calls, signals, approvals, trades."""
        exp = settings.experiment_name
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

    @app.post("/api/controls/kill-switch", dependencies=[Depends(require_csrf)])
    async def kill_switch(
        body: KillSwitchBody, user: str = Depends(require_auth), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        value = {"active": body.active, "reason": body.reason or ("dashboard" if body.active else ""), "at": utcnow().isoformat()}
        async with database.session() as s:
            await set_control(s, ControlKey.KILL_SWITCH, value, f"api:{user}")
            s.add(
                SystemEvent(
                    level="WARNING" if body.active else "INFO",
                    component="api",
                    event_type="KILL_SWITCH_ON" if body.active else "KILL_SWITCH_OFF",
                    message=f"Kill switch {'activated' if body.active else 'released'} by {user}: {body.reason}",
                    details={},
                )
            )
        return {"ok": True, "kill_switch": value}

    @app.post("/api/controls/flatten", dependencies=[Depends(require_csrf)])
    async def flatten(body: ReasonBody, user: str = Depends(require_auth), database: Database = Depends(get_db)) -> dict[str, Any]:
        value = {"requested": True, "reason": body.reason or f"dashboard ({user})", "at": utcnow().isoformat()}
        async with database.session() as s:
            await set_control(s, ControlKey.FLATTEN_REQUEST, value, f"api:{user}")
        return {"ok": True, "flatten_request": value}

    @app.post("/api/controls/reset-breaker/{which}", dependencies=[Depends(require_csrf)])
    async def reset_breaker(
        which: str, user: str = Depends(require_auth), database: Database = Depends(get_db)
    ) -> dict[str, Any]:
        key = {"daily": ControlKey.DAILY_LOSS_BREAKER, "drawdown": ControlKey.DRAWDOWN_BREAKER}.get(which)
        if key is None:
            raise HTTPException(404, "unknown breaker")
        async with database.session() as s:
            await set_control(s, key, {"tripped": False, "reset_by": user, "at": utcnow().isoformat()}, f"api:{user}")
            if key == ControlKey.DRAWDOWN_BREAKER:
                # Re-base the drawdown measurement so the breaker does not immediately re-trip.
                await set_control(s, ControlKey.PEAK_NAV, {"value": "0"}, f"api:{user}")
            s.add(
                SystemEvent(
                    level="WARNING", component="api", event_type="BREAKER_RESET",
                    message=f"{which} breaker reset by {user}", details={},
                )
            )
        return {"ok": True}

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
