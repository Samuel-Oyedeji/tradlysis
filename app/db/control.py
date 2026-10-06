"""Persistent control flags (kill switch, circuit breakers, heartbeat) in ``control_state``.

Account-wide flags use the plain key (``kill_switch``, ``flatten_request``, ``engine_heartbeat``).
Each experiment has its own kill switch, flatten request, circuit breakers and equity marks under
``<key>:<experiment slug>`` (see :func:`scoped`).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import ControlKey
from app.db.models import ControlState

# Flags kept per experiment.
EXPERIMENT_KEYS = (
    ControlKey.KILL_SWITCH,
    ControlKey.FLATTEN_REQUEST,
    ControlKey.DAILY_LOSS_BREAKER,
    ControlKey.DRAWDOWN_BREAKER,
    ControlKey.PEAK_NAV,
    ControlKey.DAY_START_NAV,
)


def scoped(key: ControlKey | str, experiment: str | None) -> str:
    """The control key for one experiment (``None`` = the account-wide key)."""
    return f"{key}:{experiment}" if experiment else str(key)


def experiment_controls(controls: dict[str, dict[str, Any]], experiment: str) -> dict[str, dict[str, Any]]:
    """An experiment's flags from :func:`get_all_controls`, keyed by the plain key name."""
    suffix = f":{experiment}"
    return {k[: -len(suffix)]: v for k, v in controls.items() if k.endswith(suffix)}


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


async def is_kill_switch_active(session: AsyncSession, experiment: str | None = None) -> tuple[bool, str]:
    """Whether new orders are stopped: the account-wide kill switch, or the experiment's own."""
    for key in (str(ControlKey.KILL_SWITCH), scoped(ControlKey.KILL_SWITCH, experiment) if experiment else None):
        if key is None:
            continue
        value = await get_control(session, key)
        if value and value.get("active"):
            return True, str(value.get("reason", ""))
    return False, ""


async def reset_breaker(session: AsyncSession, key: ControlKey, user: str, experiment: str | None = None) -> None:
    """Manually reset a circuit breaker and re-base what it measures, so it does not re-trip at once.

    Drawdown is then measured from the experiment's current equity; the daily loss from its current
    equity until the next trading day. The reconciler sets the new base on its next pass.
    """
    now = datetime.now(UTC).isoformat()
    await set_control(session, scoped(key, experiment), {"tripped": False, "reset_by": user, "at": now}, f"api:{user}")
    if key == ControlKey.DRAWDOWN_BREAKER:
        await set_control(session, scoped(ControlKey.PEAK_NAV, experiment), {"value": "0"}, f"api:{user}")
    elif key == ControlKey.DAILY_LOSS_BREAKER:
        # No trading day: the reconciler treats it as a new day and records the current value.
        await set_control(
            session, scoped(ControlKey.DAY_START_NAV, experiment), {"trading_day": None, "reset_at": now}, f"api:{user}"
        )
