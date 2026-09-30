"""Decision Layer: sends the snapshot to the model via OpenRouter and validates the answer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.db.enums import DecisionSource
from app.decision.openrouter import OpenRouterClient
from app.decision.prompts import PROMPT_VERSION, build_messages
from app.decision.schema import DECISION_SCHEMA, Action, DecisionOut


@dataclass
class DecisionOutcome:
    source: str
    decision: str
    setup: str | None
    confidence: float | None
    reason_codes: list[str]
    valid: bool
    validation_error: str | None = None
    raw_response: Any = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    rationale: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)

    @property
    def is_trade(self) -> bool:
        return self.decision in (Action.BUY, Action.SELL)


def prefilter_wait(reason_codes: list[str]) -> DecisionOutcome:
    """WAIT recorded without calling the model (deterministic prefilter)."""
    return DecisionOutcome(
        source=DecisionSource.PREFILTER,
        decision=Action.WAIT,
        setup="NONE",
        confidence=None,
        reason_codes=reason_codes or ["NO_SETUP"],
        valid=True,
    )


class DecisionService:
    def __init__(self, client: OpenRouterClient, model: str, max_tokens: int, temperature: float) -> None:
        self.client = client
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.prompt_version = PROMPT_VERSION

    async def decide(self, snapshot: dict[str, Any]) -> DecisionOutcome:
        messages = build_messages(snapshot)
        result = await self.client.structured_completion(
            model=self.model,
            messages=messages,
            schema_name="trade_decision",
            schema=DECISION_SCHEMA,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        raw = {
            "mode": result.mode,
            "content": result.content,
            "response": result.raw_response,
            "attempts": result.attempts,
        }
        common = dict(
            raw_response=raw,
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            messages=messages,
        )
        if not result.ok:
            return DecisionOutcome(
                source=DecisionSource.ERROR,
                decision=Action.WAIT,
                setup=None,
                confidence=None,
                reason_codes=["LLM_ERROR"],
                valid=False,
                validation_error=result.error,
                **common,
            )
        try:
            out = DecisionOut.model_validate(result.parsed)
        except ValidationError as exc:
            return DecisionOutcome(
                source=DecisionSource.ERROR,
                decision=Action.WAIT,
                setup=None,
                confidence=None,
                reason_codes=["SCHEMA_INVALID"],
                valid=False,
                validation_error=str(exc)[:1000],
                **common,
            )
        return DecisionOutcome(
            source=DecisionSource.LLM,
            decision=str(out.decision),
            setup=str(out.setup),
            confidence=out.confidence,
            reason_codes=[str(r) for r in out.reason_codes],
            valid=True,
            rationale=out.rationale,
            **common,
        )
