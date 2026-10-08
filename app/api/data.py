"""Read-only data API: the bot's own tables over HTTPS, for analysis tools and assistants.

Authenticated with ``Authorization: Bearer <DATA_API_TOKEN>`` (set on the Config page; empty
switches the API off), separately from the dashboard login.

Guarantees:
  * read-only: only GET routes, and every query runs in a READ ONLY transaction;
  * only the bot's tables (``app/db/models.py``), never other tables in the shared database;
    there is deliberately no raw-SQL endpoint;
  * secrets are masked: ``app_config`` values of secret settings are never returned.

Routes:
  GET /api/data/tables                  every table with its columns, time column and row count
  GET /api/data/tables/{table}          rows: ?limit=&offset=&order=-created_at&columns=a,b
                                        &since=&until= (on the table's time column) &f.<column>=<value>
  GET /api/data/diagnose?days=7         where each experiment's decision cycles stop (app/diagnose.py)
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import Table, Text, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.config.store import SECRET_KEYS, SECRET_MASK, Configuration
from app.db.models import Base
from app.db.session import Database

MAX_ROWS = 1000
TABLES: dict[str, Table] = {t.name: t for t in Base.metadata.sorted_tables}
# The column ``since``/``until`` and the default order use.
TIME_COLUMNS = ("created_at", "candle_time", "taken_at", "time", "event_time", "open_time", "computed_at",
                "updated_at", "published_at")


def time_column(table: Table) -> str | None:
    return next((c for c in TIME_COLUMNS if c in table.c), None)


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _mask(table: str, row: dict[str, Any]) -> dict[str, Any]:
    if table == "app_config" and row.get("key") in SECRET_KEYS and "value" in row:
        row["value"] = SECRET_MASK
    return row


async def _read_only(session: AsyncSession) -> None:
    await session.execute(text("SET TRANSACTION READ ONLY"))


def build_router(
    base: Settings,
    get_db: Callable[[Request], Database],
    load_config: Callable[[Database], Awaitable[Configuration]],
) -> APIRouter:
    router = APIRouter(prefix="/api/data", tags=["data"])

    async def require_token(
        request: Request, authorization: str | None = Header(default=None)
    ) -> None:
        config = await load_config(get_db(request))
        token = config.settings.data_api_token
        if not token:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "The data API is off (no DATA_API_TOKEN set).")
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(given.strip().encode(), token.encode()):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing bearer token",
                                headers={"WWW-Authenticate": "Bearer"})

    @router.get("/tables", dependencies=[Depends(require_token)])
    async def tables(request: Request) -> dict[str, Any]:
        out = []
        async with get_db(request).session() as s:
            await _read_only(s)
            for name, table in TABLES.items():
                count = await s.scalar(select(func.count()).select_from(table))
                out.append({
                    "table": name,
                    "rows": count,
                    "time_column": time_column(table),
                    "columns": {c.name: str(c.type) for c in table.columns},
                })
        return {"tables": out}

    @router.get("/tables/{name}", dependencies=[Depends(require_token)])
    async def rows(
        name: str,
        request: Request,
        limit: int = Query(100, ge=1, le=MAX_ROWS),
        offset: int = Query(0, ge=0),
        order: str | None = Query(None, description="column, '-' prefix for descending"),
        columns: str | None = Query(None, description="comma-separated subset of columns"),
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, Any]:
        table = TABLES.get(name)
        if table is None:
            raise HTTPException(404, f"unknown table {name!r}; see /api/data/tables")
        cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else [c.name for c in table.columns]
        unknown = [c for c in cols if c not in table.c]
        if unknown:
            raise HTTPException(400, f"unknown column(s): {', '.join(unknown)}")
        if name == "app_config" and "value" in cols and "key" not in cols:
            cols.append("key")  # needed to mask secret values
        q = select(*(table.c[c] for c in cols))

        for param, value in request.query_params.multi_items():
            if not param.startswith("f."):
                continue
            col = param[2:]
            if col not in table.c:
                raise HTTPException(400, f"unknown filter column {col!r}")
            q = q.where(table.c[col].is_(None) if value == "null" else cast(table.c[col], Text) == value)
        tcol = time_column(table)
        if (since or until) and tcol is None:
            raise HTTPException(400, f"{name} has no time column for since/until")
        if since is not None:
            q = q.where(table.c[tcol] >= since)
        if until is not None:
            q = q.where(table.c[tcol] < until)

        order_col = (order or "").lstrip("-") or tcol or next(iter(table.primary_key.columns)).name
        if order_col not in table.c:
            raise HTTPException(400, f"unknown order column {order_col!r}")
        descending = order.startswith("-") if order else True
        q = q.order_by(table.c[order_col].desc() if descending else table.c[order_col].asc())
        q = q.limit(limit + 1).offset(offset)

        async with get_db(request).session() as s:
            await _read_only(s)
            result = (await s.execute(q)).mappings().all()
        more = len(result) > limit
        items = [_mask(name, dict(r)) for r in result[:limit]]
        return _jsonable({"table": name, "count": len(items), "offset": offset, "has_more": more, "rows": items})

    @router.get("/diagnose", dependencies=[Depends(require_token)])
    async def diagnose(request: Request, days: int = Query(7, ge=1, le=90)) -> dict[str, Any]:
        from app.diagnose import diagnosis

        db = get_db(request)
        return {"days": days, "text": await diagnosis(db, base, days)}

    return router
