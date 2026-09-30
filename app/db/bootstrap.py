"""Create the bot's tables when they are missing. It never alters or drops anything.

The engine, API and analyzer call :func:`ensure_tables` at start-up (``DB_AUTO_CREATE_TABLES``), so
a fresh database, or a shared one where some tables were created by hand, works without running
Prisma first. Only the bot's own tables (``app/db/models.py``) are ever created; other tables in
the database are not touched. Existing tables are left exactly as they are: schema *changes*
(new columns etc.) still come from Prisma migrations.

Tables it creates get row-level security enabled with no policies. That changes nothing for
the bot, which owns the tables, but keeps them out of Supabase's public REST API (anon key).

    python -m app.db.bootstrap           # create missing tables now
    python -m app.db.bootstrap --check   # only report what is missing
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.models import Base
from app.db.session import Database

log = logging.getLogger(__name__)
# Serialises start-up across processes (engine, api, analyzer) so they never race on CREATE TABLE.
BOOTSTRAP_LOCK_KEY = 7_214_990_331


@dataclass
class SchemaReport:
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)

    @property
    def expected_tables(self) -> list[str]:
        return [t.name for t in Base.metadata.sorted_tables]

    @property
    def complete(self) -> bool:
        return not self.missing_tables and not self.missing_columns


async def _report(conn: AsyncConnection) -> SchemaReport:
    def run(sync_conn) -> SchemaReport:
        insp = inspect(sync_conn)
        existing = set(insp.get_table_names())
        report = SchemaReport()
        for table in Base.metadata.sorted_tables:
            if table.name not in existing:
                report.missing_tables.append(table.name)
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            missing = [c.name for c in table.columns if c.name not in have]
            if missing:
                report.missing_columns[table.name] = missing
        return report

    return await conn.run_sync(run)


async def check_tables(db: Database) -> SchemaReport:
    """Which of the bot's tables (and columns) are missing. Read-only."""
    async with db.engine.connect() as conn:
        return await _report(conn)


async def ensure_tables(db: Database, *, enable_rls: bool = True) -> list[str]:
    """Create the bot's missing tables (with their indexes and foreign keys). Returns their names."""
    async with db.engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": BOOTSTRAP_LOCK_KEY})
        report = await _report(conn)
        missing = [t for t in Base.metadata.sorted_tables if t.name in report.missing_tables]
        if missing:
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=missing, checkfirst=True))
            if enable_rls:
                for table in missing:
                    await conn.execute(text(f'ALTER TABLE "{table.name}" ENABLE ROW LEVEL SECURITY'))
    created = [t.name for t in missing]
    if created:
        log.warning("Created missing tables: %s", ", ".join(created))
    if report.missing_columns:
        log.error(
            "Tables exist but lack columns (apply the Prisma migrations): %s",
            "; ".join(f"{t}: {', '.join(c)}" for t, c in report.missing_columns.items()),
        )
    return created


async def auto_create_tables(db: Database, settings) -> list[str]:
    """Start-up hook for the engine, API and analyzer (``DB_AUTO_CREATE_TABLES``)."""
    if not settings.db_auto_create_tables:
        return []
    return await ensure_tables(db)


async def _main() -> int:
    from app.config.settings import get_settings

    parser = argparse.ArgumentParser(description="Create the Tradlysis tables that are missing from the database")
    parser.add_argument("--check", action="store_true", help="only report, do not create anything")
    args = parser.parse_args()
    db = Database(get_settings())
    try:
        if args.check:
            report = await check_tables(db)
        else:
            created = await ensure_tables(db)
            print(f"Created {len(created)} table(s): {', '.join(created)}" if created else "No tables were missing.")
            report = await check_tables(db)
    finally:
        await db.dispose()
    present = len(report.expected_tables) - len(report.missing_tables)
    print(f"{present}/{len(report.expected_tables)} tables present")
    if report.missing_tables:
        print("Missing tables: " + ", ".join(report.missing_tables))
    for table, cols in report.missing_columns.items():
        print(f"Table {table} is missing columns (apply the Prisma migrations): {', '.join(cols)}")
    return 0 if report.complete else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
