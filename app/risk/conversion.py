"""Currency conversion factors for position sizing.

When the account currency is one side of the instrument the factors follow from the price. For
any other account currency the orchestrator looks up cross rates at the broker and passes them in
``cross_rates``: ``{currency: account-currency value of 1 unit of currency}``.
"""

from __future__ import annotations


def conversion_rates(
    account_currency: str,
    instrument: str,
    mid: float,
    cross_rates: dict[str, float] | None = None,
) -> tuple[float | None, float | None]:
    """Return (quote_home_rate for losses, base_home_rate for position value)."""
    base, _, quote = instrument.partition("_")
    table = cross_rates or {}

    if account_currency == quote:
        quote_rate: float | None = 1.0
    elif account_currency == base:
        quote_rate = 1.0 / mid if mid else None
    else:
        quote_rate = table.get(quote)

    if account_currency == base:
        base_rate: float | None = 1.0
    elif account_currency == quote:
        base_rate = mid
    else:
        base_rate = table.get(base)
    return quote_rate, base_rate
