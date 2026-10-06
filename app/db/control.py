"""Persistent control flags (kill switch, circuit breakers, heartbeat) in ``control_state``."""

from __future__ import annotations

from datetime import UTC, datetime
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


async def reset_breaker(session: AsyncSession, key: ControlKey, user: str) -> None:
    """Manually reset a circuit breaker and re-base what it measures, so it does not re-trip at once.

    Drawdown is then measured from the current account value; the daily loss from the current account
    value until the next trading day. The reconciler sets the new base from its next account reading.
    """
    now = datetime.now(UTC).isoformat()
    await set_control(session, key, {"tripped": False, "reset_by": user, "at": now}, f"api:{user}")
    if key == ControlKey.DRAWDOWN_BREAKER:
        await set_control(session, ControlKey.PEAK_NAV, {"value": "0"}, f"api:{user}")
    elif key == ControlKey.DAILY_LOSS_BREAKER:
        # No trading day: the reconciler treats it as a new day and records the current value.
        await set_control(session, ControlKey.DAY_START_NAV, {"trading_day": None, "reset_at": now}, f"api:{user}")
