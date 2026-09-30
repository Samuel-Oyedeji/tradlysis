"""Broker-neutral data types.

The broker client (``app/broker/capital.py``) translates the broker's API into these types so the
services (market data, executor, reconciliation, risk) never depend on the broker's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class InstrumentInfo:
    name: str  # internal instrument name, e.g. EUR_USD
    pip_location: int
    display_precision: int
    trade_units_precision: int
    minimum_trade_size: float
    margin_rate: float
    epic: str = ""  # broker market identifier (Capital.com "epic", e.g. EURUSD)
    lot_size: float = 1.0  # base-currency units per 1 unit of deal size

    @property
    def pip_size(self) -> float:
        return 10.0 ** self.pip_location

    def format_price(self, price: float) -> str:
        return f"{price:.{self.display_precision}f}"


# Fallback when instrument details are unavailable (unit tests, offline tools).
DEFAULT_INSTRUMENTS = {
    "EUR_USD": InstrumentInfo("EUR_USD", -4, 5, 0, 1.0, 0.0333, "EURUSD"),
}


@dataclass
class AccountState:
    account_id: str
    currency: str
    balance: Decimal  # cash balance (excludes open P/L)
    nav: Decimal  # equity: balance + unrealized P/L
    unrealized_pl: Decimal
    margin_used: Decimal
    margin_available: Decimal
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrokerPosition:
    deal_id: str
    instrument: str
    direction: str  # BUY | SELL
    units: int  # signed: positive = long
    open_price: float
    open_time: datetime
    stop_loss: float | None
    take_profit: float | None
    unrealized_pl: Decimal
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Quote:
    instrument: str
    time: datetime
    bid: float
    ask: float
