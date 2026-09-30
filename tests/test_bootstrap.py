"""Table auto-creation (app/db/bootstrap.py) against a real (test) PostgreSQL database.

Each test works in its own schema (via search_path) so the migration-built ``public`` schema
used by the other tests is never touched.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import to_asyncpg_url
from app.db.bootstrap import check_tables, ensure_tables
from app.db.models import Base

EXPECTED = {t.name for t in Base.metadata.sorted_tables}


@pytest.fixture
async def schema_db(migrated_db_url, request):
    """A Database-like object whose connections default to a fresh, empty schema."""
    schema = f"bs_{request.node.name[:40].lower()}"
    admin = create_async_engine(to_asyncpg_url(migrated_db_url))
    async with admin.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        to_asyncpg_url(migrated_db_url), connect_args={"server_settings": {"search_path": schema}}
    )
    yield SimpleNamespace(engine=engine, schema=schema, admin=admin)
    await engine.dispose()
    async with admin.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    await admin.dispose()


def _describe(sync_conn, schema: str) -> dict:
    """Everything about the bot's tables in ``schema`` that must match the Prisma migration."""
    insp = inspect(sync_conn)

    def norm(value):
        return None if value is None else re.sub(rf'"?\b{re.escape(schema)}"?\.', "", str(value))

    out = {}
    for t in sorted(EXPECTED):
        out[t] = {
            "columns": [(c["name"], str(c["type"]), c["nullable"], norm(c["default"]))
                        for c in insp.get_columns(t, schema=schema)],
            "pk": insp.get_pk_constraint(t, schema=schema)["constrained_columns"],
            "indexes": sorted((i["name"], tuple(i["column_names"]), bool(i["unique"]))
                              for i in insp.get_indexes(t, schema=schema)),
            "unique_constraints": sorted(u["name"] for u in insp.get_unique_constraints(t, schema=schema)),
            "fks": sorted((f["name"], tuple(f["constrained_columns"]), f["referred_table"],
                           tuple(f["referred_columns"]), tuple(sorted(f["options"].items())))
                          for f in insp.get_foreign_keys(t, schema=schema)),
        }
    return out


async def test_created_tables_match_the_prisma_migration(schema_db):
    created = await ensure_tables(schema_db)
    assert set(created) == EXPECTED
    async with schema_db.admin.connect() as conn:
        from_models = await conn.run_sync(_describe, schema_db.schema)
        from_migration = await conn.run_sync(_describe, "public")
    assert from_models == from_migration


async def test_only_missing_tables_are_created_and_others_untouched(schema_db):
    async with schema_db.engine.begin() as conn:
        # Another app's table, and two of ours created earlier (e.g. by hand), holding data.
        await conn.execute(text("CREATE TABLE other_app_users (id int primary key, email text)"))
        await conn.execute(text("INSERT INTO other_app_users VALUES (1, 'a@example.com')"))
    subset = [Base.metadata.tables["control_state"], Base.metadata.tables["system_events"]]
    async with schema_db.engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=subset))
        await conn.execute(text(
            "INSERT INTO control_state (key, value, updated_by) VALUES ('kill_switch', '{\"active\": true}', 'me')"))

    report = await check_tables(schema_db)
    assert set(report.missing_tables) == EXPECTED - {"control_state", "system_events"}

    created = await ensure_tables(schema_db)
    assert set(created) == EXPECTED - {"control_state", "system_events"}
    assert (await check_tables(schema_db)).complete
    assert await ensure_tables(schema_db) == []  # idempotent

    async with schema_db.engine.connect() as conn:
        assert (await conn.execute(text("SELECT email FROM other_app_users"))).scalar() == "a@example.com"
        assert (await conn.execute(text("SELECT count(*) FROM control_state"))).scalar() == 1
        rls = dict((await conn.execute(text(
            "SELECT relname, relrowsecurity FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND c.relkind = 'r'"))).all())
    assert rls["orders"] is True and rls["trades"] is True  # created by the bot
    assert rls["control_state"] is False and rls["other_app_users"] is False  # left exactly as they were


async def test_missing_columns_are_reported_not_altered(schema_db):
    await ensure_tables(schema_db)
    async with schema_db.engine.begin() as conn:
        await conn.execute(text("ALTER TABLE orders DROP COLUMN http_status"))
    assert await ensure_tables(schema_db) == []
    report = await check_tables(schema_db)
    assert report.missing_columns == {"orders": ["http_status"]} and not report.complete
