"""Support/resistance zones and key reference levels."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.market_data.candles import Bar
from app.technicals.structure import Swing


@dataclass
class Level:
    kind: str  # SUPPORT | RESISTANCE
    source: str  # SWING_CLUSTER | PREV_DAY_HIGH | PREV_DAY_LOW | PREV_WEEK_HIGH | PREV_WEEK_LOW
    price_low: float
    price_high: float
    touches: int = 1
    strength: float = 0.0
    timeframes: list[str] = field(default_factory=list)

    @property
    def price(self) -> float:
        return (self.price_low + self.price_high) / 2

    def distance_to(self, price: float) -> float:
        """Distance from ``price`` to the nearest edge of the zone (0 if inside)."""
        if self.price_low <= price <= self.price_high:
            return 0.0
        return min(abs(price - self.price_low), abs(price - self.price_high))


def cluster_swings(
    swings_by_tf: dict[str, Sequence[Swing]],
    tolerance: float,
    *,
    total_bars_by_tf: dict[str, int] | None = None,
) -> list[Level]:
    """Merge nearby swing prices (within ``tolerance``) into zones.

    Strength rewards more touches, higher timeframes and recency. Kind is assigned later
    relative to the current price.
    """
    tf_weight = {"H4": 2.0, "H1": 1.0, "M15": 0.5}
    points: list[tuple[float, str, float]] = []  # (price, timeframe, recency 0..1)
    for tf, swings in swings_by_tf.items():
        n = (total_bars_by_tf or {}).get(tf) or (max((s.index for s in swings), default=0) + 1)
        for s in swings:
            recency = s.index / max(n - 1, 1)
            points.append((s.price, tf, recency))
    points.sort(key=lambda p: p[0])

    zones: list[Level] = []
    cluster: list[tuple[float, str, float]] = []

    def flush() -> None:
        if not cluster:
            return
        prices = [p[0] for p in cluster]
        tfs = sorted({p[1] for p in cluster})
        strength = sum(tf_weight.get(p[1], 1.0) * (0.5 + 0.5 * p[2]) for p in cluster)
        zones.append(
            Level(
                kind="",
                source="SWING_CLUSTER",
                price_low=min(prices),
                price_high=max(prices),
                touches=len(cluster),
                strength=round(strength, 3),
                timeframes=tfs,
            )
        )

    for p in points:
        if cluster and p[0] - cluster[0][0] > tolerance:
            flush()
            cluster = []
        cluster.append(p)
    flush()
    return zones


def reference_levels(daily: Sequence[Bar], weekly: Sequence[Bar]) -> list[Level]:
    """Previous day and previous week high/low (from completed candles)."""
    out: list[Level] = []
    if daily:
        d = daily[-1]
        out.append(Level("", "PREV_DAY_HIGH", d.high, d.high, 1, 1.5, ["D"]))
        out.append(Level("", "PREV_DAY_LOW", d.low, d.low, 1, 1.5, ["D"]))
    if weekly:
        w = weekly[-1]
        out.append(Level("", "PREV_WEEK_HIGH", w.high, w.high, 1, 2.5, ["W"]))
        out.append(Level("", "PREV_WEEK_LOW", w.low, w.low, 1, 2.5, ["W"]))
    return out


def assign_kinds(levels: list[Level], price: float) -> list[Level]:
    """Label levels below price as SUPPORT and above as RESISTANCE (inside a zone: by its mid)."""
    for lv in levels:
        lv.kind = "SUPPORT" if lv.price <= price else "RESISTANCE"
    return levels


def nearest(levels: Sequence[Level], price: float, kind: str) -> Level | None:
    candidates = [lv for lv in levels if lv.kind == kind]
    if not candidates:
        return None
    return min(candidates, key=lambda lv: lv.distance_to(price))


def sorted_by_distance(levels: Sequence[Level], price: float, kind: str, limit: int = 3) -> list[Level]:
    return sorted((lv for lv in levels if lv.kind == kind), key=lambda lv: lv.distance_to(price))[:limit]
