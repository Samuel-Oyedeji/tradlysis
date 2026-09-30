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
