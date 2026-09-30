"""Shared fixtures.

Database-backed tests need a disposable PostgreSQL database in TEST_DATABASE_URL, e.g.
    TEST_DATABASE_URL=postgresql://tradlysis:tradlysis@localhost:5432/tradlysis_test
The schema is rebuilt from prisma/migrations/*/migration.sql for every test session, which
also proves the migrations apply cleanly and match the SQLAlchemy models.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.market_data.candles import Bar

ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")


def make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        database_url=TEST_DATABASE_URL or "postgresql://u:p@localhost:5432/db",
        oanda_practice_api_token="test-token",
        oanda_practice_account_id="101-001-0000000-001",
        openrouter_api_key="test-key",
        dashboard_password="secret",
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


def trend_bars(
    n: int,
    start: float,
    step: float,
    *,
    minutes: int = 15,
    wiggle: float = 0.0,
    start_time: datetime | None = None,
    period: int = 6,
) -> list[Bar]:
    """Deterministic trending series with regular zig-zags so swings exist."""
    t0 = start_time or datetime(2026, 9, 1, tzinfo=UTC)
    bars = []
    price = start
    for i in range(n):
        phase = i % period
        drift = step if phase < period - 2 else -step * 0.8
        o = price
        c = price + drift
        h = max(o, c) + wiggle
        lo = min(o, c) - wiggle
        bars.append(Bar(t0 + timedelta(minutes=minutes * i), o, h, lo, c, 100, True))
        price = c
    return bars


# ----------------------------------------------------------------------- database


def _migration_sql() -> list[str]:
    return [p.read_text() for p in sorted((ROOT / "prisma" / "migrations").glob("*/migration.sql"))]


@pytest.fixture(scope="session")
def migrated_db_url() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not set")
    import asyncio

    import asyncpg

    async def rebuild() -> None:
        conn = await asyncpg.connect(TEST_DATABASE_URL.split("?")[0])
        try:
            await conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
            for sql in _migration_sql():
                await conn.execute(sql)
        finally:
            await conn.close()

    asyncio.run(rebuild())
    return TEST_DATABASE_URL


@pytest.fixture
async def db(migrated_db_url):
    from sqlalchemy import text

    from app.db.models import Base
    from app.db.session import Database

    database = Database(make_settings(database_url=migrated_db_url))
    async with database.session() as s:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await s.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield database
    await database.dispose()
