"""Types every strategy returns: the rule checks, an optional fixed trade plan, and failure codes.

A strategy decides *whether its predefined setup is technically present* and, if so, proposes
entry / stop-loss / take-profit. The LLM only confirms or declines the setup; it can never change
these prices. The risk engine sizes the position and has final authority.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TradePlan:
    direction: str
    setup: str
    entry: float
    stop_loss: float
    take_profit: float
    risk_pips: float
    reward_pips: float
    risk_reward: float
    stop_source: str
    target_source: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TradePlan:
        return cls(**data)


@dataclass
class Condition:
    name: str
    passed: bool
    detail: str = ""


@dataclass
class StrategyResult:
    setup: str
    direction: str | None  # direction implied by the higher timeframe, if any
    candidate: bool  # all technical conditions passed
    conditions: list[Condition]
    trade_plan: TradePlan | None
    failure_codes: list[str]
    # Strategy-specific facts shown to the model and stored with the request (e.g. the range of a breakout).
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "setup": self.setup,
            "direction": self.direction,
            "candidate": self.candidate,
            "conditions": [asdict(c) for c in self.conditions],
            "trade_plan": self.trade_plan.to_dict() if self.trade_plan else None,
            "failure_codes": self.failure_codes,
        }
        if self.context:
            out["context"] = self.context
        return out

    def condition(self, name: str) -> Condition | None:
        return next((c for c in self.conditions if c.name == name), None)
