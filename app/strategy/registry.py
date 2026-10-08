"""The strategies an experiment can run: their names, setups, decision prompts and the market
regimes that suit them. The evaluation functions are dispatched in ``app/engine.py``."""

from __future__ import annotations

from dataclasses import dataclass

from app.decision.prompts import LONDON_BREAKOUT, RANGE_BREAKOUT, TREND_FOLLOWING, TREND_PULLBACK, DecisionPrompt
from app.technicals.regime import MarketRegimeLabel as R


@dataclass(frozen=True)
class StrategySpec:
    key: str  # experiments.strategy
    label: str
    setup: str  # StrategyResult.setup / the decision schema's Setup
    prompt: DecisionPrompt
    suitable_regimes: frozenset[R]


STRATEGIES: dict[str, StrategySpec] = {
    "trend_pullback": StrategySpec(
        "trend_pullback", "Trend pullback", "TREND_PULLBACK", TREND_PULLBACK,
        frozenset({R.STRONG_UPTREND, R.STRONG_DOWNTREND}),
    ),
    "range_breakout": StrategySpec(
        "range_breakout", "Range breakout", "RANGE_BREAKOUT", RANGE_BREAKOUT,
        frozenset({R.BREAKOUT_UP, R.BREAKOUT_DOWN, R.COMPRESSION, R.RANGE}),
    ),
    "trend_following": StrategySpec(
        "trend_following", "Trend following (4h)", "TREND_FOLLOWING", TREND_FOLLOWING,
        frozenset({R.STRONG_UPTREND, R.STRONG_DOWNTREND, R.BREAKOUT_UP, R.BREAKOUT_DOWN}),
    ),
    "london_breakout": StrategySpec(
        "london_breakout", "London breakout", "LONDON_BREAKOUT", LONDON_BREAKOUT,
        frozenset({R.BREAKOUT_UP, R.BREAKOUT_DOWN, R.COMPRESSION, R.RANGE}),
    ),
}
SPEC_BY_SETUP = {spec.setup: spec for spec in STRATEGIES.values()}
