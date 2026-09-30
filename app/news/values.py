"""Parsing of economic-calendar values ("3.1%", "250K", "-0.2B") and surprise computation."""

from __future__ import annotations

import re

_MULTIPLIER = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
_NUM_RE = re.compile(r"^\s*[<>]?\s*(-?\d+(?:\.\d+)?)\s*([KMBT%]?)\s*$", re.IGNORECASE)


def parse_value(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUM_RE.match(text.replace(",", ""))
    if not m:
        return None
    value = float(m.group(1))
    suffix = m.group(2).upper()
    return value * _MULTIPLIER.get(suffix, 1.0)


def surprise(actual: float | None, forecast: float | None) -> tuple[float | None, str | None]:
    if actual is None or forecast is None:
        return None, None
    diff = actual - forecast
    if abs(diff) < 1e-12:
        return 0.0, "INLINE"
    return diff, "ABOVE" if diff > 0 else "BELOW"


# Releases where a higher number is bad for the currency.
_INVERSE_KEYWORDS = ("unemployment", "jobless", "claimant", "claims")


def currency_impact(title: str, surprise_direction: str | None) -> str | None:
    """Deterministic first-order read of a data surprise for the releasing currency.

    Above-forecast data is treated as currency-bullish, except for releases where a higher
    number is bad news (unemployment, jobless claims). Returns BULLISH / BEARISH / NEUTRAL,
    or None when there is no actual-vs-forecast comparison yet. Nuanced cases (central-bank
    tone, revisions) are left to the LLM interpretation layer.
    """
    if surprise_direction is None:
        return None
    if surprise_direction == "INLINE":
        return "NEUTRAL"
    above = surprise_direction == "ABOVE"
    if any(k in title.lower() for k in _INVERSE_KEYWORDS):
        above = not above
    return "BULLISH" if above else "BEARISH"


def pair_impact(currency: str, impact: str | None, base: str, quote: str) -> str | None:
    """Translate a currency impact into the pair direction (e.g. USD bullish -> EUR_USD down)."""
    if impact not in ("BULLISH", "BEARISH"):
        return None if impact is None else "NEUTRAL"
    up = impact == "BULLISH"
    if currency == base:
        return "UP" if up else "DOWN"
    if currency == quote:
        return "DOWN" if up else "UP"
    return None
