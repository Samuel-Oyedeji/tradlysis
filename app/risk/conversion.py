"""Currency conversion factors for position sizing.

OANDA pricing responses include ``homeConversions`` when ``includeHomeConversions=true``:
``[{"currency": "USD", "accountGain": "...", "accountLoss": "...", "positionValue": "..."}]``
where each factor converts an amount in ``currency`` into the account (home) currency.
"""

from __future__ import annotations

from typing import Any


def conversion_rates(
    account_currency: str,
    instrument: str,
    mid: float,
    home_conversions: list[dict[str, Any]] | None = None,
) -> tuple[float | None, float | None]:
    """Return (quote_home_rate for losses, base_home_rate for position value)."""
    base, _, quote = instrument.partition("_")
    table = {c.get("currency"): c for c in (home_conversions or [])}

    if account_currency == quote:
        quote_rate: float | None = 1.0
    elif account_currency == base:
        quote_rate = 1.0 / mid if mid else None
    elif quote in table:
        quote_rate = float(table[quote].get("accountLoss") or table[quote].get("positionValue"))
    else:
        quote_rate = None

    if account_currency == base:
        base_rate: float | None = 1.0
    elif account_currency == quote:
        base_rate = mid
    elif base in table:
        base_rate = float(table[base].get("positionValue") or table[base].get("accountLoss"))
    else:
        base_rate = None
    return quote_rate, base_rate
