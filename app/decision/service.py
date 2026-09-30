"""Decision Layer: asks the model via OpenRouter whether the setup is present and validates the answer.

With a decision model (Jev) the snapshot is sent to the Decisions API as the ``state`` with typed
questions: one BUY/SELL/WAIT choice (its probability is the confidence) and yes/no checks that are
recorded as reason codes. Chat models get the system prompt and a JSON-schema response instead.
Whatever the path, anything invalid becomes WAIT.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.db.enums import DecisionSource
from app.decision.openrouter import OpenRouterClient, uses_decisions_api
from app.decision.prompts import DECISION_CHECKS, PROMPT_VERSION, build_decision_questions, build_messages
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
        if uses_decisions_api(self.model):
            return await self._decide_with_decisions_api(snapshot)
        return await self._decide_with_chat(snapshot)

    async def _decide_with_decisions_api(self, snapshot: dict[str, Any]) -> DecisionOutcome:
        questions = build_decision_questions()
        result = await self.client.decisions(model=self.model, state=snapshot, questions=questions)
        common = dict(
            raw_response={"api": "decisions", "response": result.raw_response, "error": result.error},
            latency_ms=result.latency_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            # Recorded with the request so every decision can be replayed: what was asked, about what.
            messages=[{"role": "questions", "content": json.dumps(questions)}],
        )
        if not result.ok:
            return _error_wait("LLM_ERROR", result.error, common)
        try:
            out = DecisionOut.model_validate(parse_decision_answers(result.answers, snapshot))
        except (ValidationError, ValueError) as exc:
            return _error_wait("SCHEMA_INVALID", str(exc)[:1000], common)
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

    async def _decide_with_chat(self, snapshot: dict[str, Any]) -> DecisionOutcome:
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


def _error_wait(code: str, error: str | None, common: dict[str, Any]) -> DecisionOutcome:
    return DecisionOutcome(
        source=DecisionSource.ERROR,
        decision=Action.WAIT,
        setup=None,
        confidence=None,
        reason_codes=[code],
        valid=False,
        validation_error=error,
        **common,
    )


def parse_decision_answers(answers: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Map Decisions API answers onto the decision schema (validated afterwards by ``DecisionOut``)."""
    answer = answers.get("decision")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise ValueError("no choice answer for 'decision'")
    choice = str(answer.get("choice", "")).upper()
    probabilities = {str(k).upper(): v for k, v in (answer.get("probabilities") or {}).items()}
    confidence = probabilities.get(choice, answer.get("confidence"))
    if confidence is None:
        raise ValueError("decision answer carries no probability or confidence")

    plan = (snapshot.get("setup_check") or {}).get("trade_plan") or {}
    direction = choice if choice in (Action.BUY, Action.SELL) else plan.get("direction")
    slot = 0 if direction == Action.BUY else 1 if direction == Action.SELL else 2
    codes: list[str] = []
    for key, (_question, yes_codes, no_code) in DECISION_CHECKS.items():
        check = answers.get(key)
        if not isinstance(check, dict) or check.get("noul") is None:
            continue
        code = yes_codes[slot] if float(check["noul"]) >= 0.5 else no_code
        if code and code not in codes:
            codes.append(code)

    ranked = sorted(probabilities.items(), key=lambda kv: -float(kv[1]))
    rationale = " · ".join(f"P({k})={float(v):.2f}" for k, v in ranked) or f"{choice} ({float(confidence):.2f})"
    return {
        "decision": choice,
        "setup": "TREND_PULLBACK" if choice in (Action.BUY, Action.SELL) else "NONE",
        "confidence": float(confidence),
        "reason_codes": codes or ["OTHER"],
        "rationale": rationale[:400],
    }
