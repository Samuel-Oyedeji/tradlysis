"""Allowed values for text status columns (the schema stores them as plain text)."""

from enum import StrEnum


class Direction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class DecisionAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class DecisionSource(StrEnum):
    LLM = "LLM"
    PREFILTER = "PREFILTER"
    ERROR = "ERROR"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # row written, request not yet sent
    SUBMITTED = "SUBMITTED"  # accepted by broker, fill state not yet known
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"  # request outcome unknown (timeout / disconnect); must be reconciled
    FAILED = "FAILED"  # never reached the broker


OPEN_ORDER_STATUSES = (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN)


class OrderPurpose(StrEnum):
    ENTRY = "ENTRY"
    CLOSE = "CLOSE"


class TradeState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class EventLevel(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class Impact(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    HOLIDAY = "HOLIDAY"
    UNKNOWN = "UNKNOWN"


class ControlKey(StrEnum):
    KILL_SWITCH = "kill_switch"  # {"active": bool, "reason": str}
    FLATTEN_REQUEST = "flatten_request"  # {"requested": bool, "reason": str}
    DAILY_LOSS_BREAKER = "daily_loss_breaker"  # {"tripped": bool, "trading_day": str, ...}
    DRAWDOWN_BREAKER = "drawdown_breaker"  # {"tripped": bool, ...}; manual reset only
    PEAK_NAV = "peak_nav"  # {"value": str}
    DAY_START_NAV = "day_start_nav"  # {"trading_day": str, "value": str}
    LAST_TRANSACTION_ID = "last_transaction_id"  # {"value": str}
    ENGINE_HEARTBEAT = "engine_heartbeat"  # live status published by the engine
    CONFIG_VERSION = "config_version"  # {"version": int, "at": str, "by": str}; bumped on every config save
    CONFIG_IMPORTED = "config_imported"  # {"keys": [...]}; set once the environment was copied in
