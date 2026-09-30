"""prisma/schema.prisma is the source of truth; app/db/models.py must mirror it exactly."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import ARRAY, BigInteger, Boolean, DateTime, Float, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.models import Base
from tests.conftest import ROOT

PRISMA_TYPES = {
    "BigInt": BigInteger,
    "Int": Integer,
    "Float": Float,
    "String": Text,
    "Boolean": Boolean,
    "DateTime": DateTime,
    "Json": JSONB,
    "Decimal": Numeric,
}


def parse_prisma(path) -> dict[str, dict[str, tuple[str, bool, bool]]]:
    """table -> column -> (prisma type, nullable, is_list)."""
    text = path.read_text()
    blocks = re.findall(r"^model\s+(\w+)\s*\{(.*?)^\}", text, re.S | re.M)
    model_names = {name for name, _ in blocks}
    tables: dict[str, dict[str, tuple[str, bool, bool]]] = {}
    for name, body in blocks:
        table = name
        cols: dict[str, tuple[str, bool, bool]] = {}
        for raw in body.splitlines():
            line = raw.split("//")[0].strip()
            if not line or line.startswith("///"):
                continue
            if line.startswith("@@"):
                m = re.match(r'@@map\("([^"]+)"\)', line)
                if m:
                    table = m.group(1)
                continue
            parts = line.split()
            field, ftype = parts[0], parts[1]
            base = ftype.rstrip("?").removesuffix("[]")
            if base in model_names:
                continue  # relation field, not a column
            m = re.search(r'@map\("([^"]+)"\)', line)
            col = m.group(1) if m else field
            is_list = ftype.endswith("[]")
            nullable = ftype.endswith("?") or is_list  # Prisma scalar lists are nullable columns
            cols[col] = (base, nullable, is_list)
        tables[table] = cols
    return tables


PRISMA = parse_prisma(ROOT / "prisma" / "schema.prisma")


def test_same_tables():
    assert set(PRISMA) == set(Base.metadata.tables)


@pytest.mark.parametrize("table", sorted(PRISMA))
def test_columns_match(table):
    sa_table = Base.metadata.tables[table]
    assert set(PRISMA[table]) == set(sa_table.columns.keys()), table
    for col, (ptype, nullable, is_list) in PRISMA[table].items():
        c = sa_table.columns[col]
        assert c.nullable == nullable or c.primary_key, f"{table}.{col} nullable mismatch"
        if is_list:
            assert isinstance(c.type, ARRAY), f"{table}.{col} should be an array"
        else:
            assert isinstance(c.type, PRISMA_TYPES[ptype]), f"{table}.{col}: {c.type!r} vs {ptype}"
        if ptype == "DateTime":
            assert c.type.timezone, f"{table}.{col} must be timezone-aware"


async def test_migrations_match_models(db):
    """Reflect the database built from the migration SQL and compare with the models."""
    from sqlalchemy import inspect

    async with db.engine.connect() as conn:
        def reflect(sync_conn):
            insp = inspect(sync_conn)
            return {
                t: {c["name"]: c["nullable"] for c in insp.get_columns(t)}
                for t in insp.get_table_names()
                if t != "_prisma_migrations"
            }

        actual = await conn.run_sync(reflect)
    expected = {
        t.name: {c.name: (c.nullable and not c.primary_key) for c in t.columns} for t in Base.metadata.sorted_tables
    }
    assert actual == expected
