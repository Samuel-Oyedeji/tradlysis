"""Persistent control flags (kill switch, circuit breakers, heartbeat) in ``control_state``."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ControlKey
from app.db.models import ControlState


async def get_control(session: AsyncSession, key: ControlKey | str) -> dict[str, Any] | None:
    row = await session.scalar(select(ControlState).where(ControlState.key == str(key)))
    return row.value if row else None


async def get_all_controls(session: AsyncSession) -> dict[str, dict[str, Any]]:
    rows = (await session.scalars(select(ControlState))).all()
    return {r.key: r.value for r in rows}


async def set_control(
    session: AsyncSession, key: ControlKey | str, value: dict[str, Any], updated_by: str
) -> None:
    stmt = insert(ControlState).values(key=str(key), value=value, updated_by=updated_by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[ControlState.key],
        set_={"value": stmt.excluded.value, "updated_by": updated_by, "updated_at": stmt.excluded.updated_at},
    )
    await session.execute(stmt)


async def is_kill_switch_active(session: AsyncSession) -> tuple[bool, str]:
    value = await get_control(session, ControlKey.KILL_SWITCH)
    if not value:
        return False, ""
    return bool(value.get("active")), str(value.get("reason", ""))
