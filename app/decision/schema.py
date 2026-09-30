"""Schema for the decision model's response. Anything that does not validate becomes WAIT."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Action(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    WAIT = "WAIT"


class Setup(StrEnum):
    TREND_PULLBACK = "TREND_PULLBACK"
    NONE = "NONE"


class ReasonCode(StrEnum):
    HTF_BULLISH = "HTF_BULLISH"
    HTF_BEARISH = "HTF_BEARISH"
    HTF_UNCLEAR = "HTF_UNCLEAR"
    H1_ALIGNED = "H1_ALIGNED"
    H1_CONFLICT = "H1_CONFLICT"
    PULLBACK_TO_SUPPORT = "PULLBACK_TO_SUPPORT"
    PULLBACK_TO_RESISTANCE = "PULLBACK_TO_RESISTANCE"
    NO_PULLBACK = "NO_PULLBACK"
    MOMENTUM_CONFIRMED = "MOMENTUM_CONFIRMED"
    MOMENTUM_WEAK = "MOMENTUM_WEAK"
    STRUCTURE_INTACT = "STRUCTURE_INTACT"
    STRUCTURE_BROKEN = "STRUCTURE_BROKEN"
    RANGE_BOUND = "RANGE_BOUND"
    OVEREXTENDED = "OVEREXTENDED"
    LEVEL_TOO_CLOSE = "LEVEL_TOO_CLOSE"
    POOR_RISK_REWARD = "POOR_RISK_REWARD"
    NEWS_RISK_HIGH = "NEWS_RISK_HIGH"
    NEWS_BIAS_SUPPORTS = "NEWS_BIAS_SUPPORTS"
    NEWS_BIAS_CONFLICTS = "NEWS_BIAS_CONFLICTS"
    SPREAD_WIDE = "SPREAD_WIDE"
    VOLATILITY_EXTREME = "VOLATILITY_EXTREME"
    VOLATILITY_LOW = "VOLATILITY_LOW"
    NO_SETUP = "NO_SETUP"
    OTHER = "OTHER"


class DecisionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Action
    setup: Setup
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[ReasonCode] = Field(min_length=1, max_length=8)
    rationale: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def _consistent(self) -> DecisionOut:
        if self.decision != Action.WAIT and self.setup != Setup.TREND_PULLBACK:
            raise ValueError("BUY/SELL requires setup TREND_PULLBACK")
        return self


DECISION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decision", "setup", "confidence", "reason_codes", "rationale"],
    "properties": {
        "decision": {"type": "string", "enum": [a.value for a in Action]},
        "setup": {"type": "string", "enum": [s.value for s in Setup]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_codes": {
            "type": "array",
            "items": {"type": "string", "enum": [r.value for r in ReasonCode]},
            "minItems": 1,
            "maxItems": 8,
        },
        "rationale": {"type": "string", "description": "At most two short sentences."},
    },
}
