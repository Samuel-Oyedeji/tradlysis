"""Versioned prompts for the decision layer, one set per strategy. Bump a strategy's version on any
wording change so results can be grouped by prompt in the experiment analysis.

Two forms of the same task:
  * decision models (Jev) on OpenRouter's Decisions API: the snapshot is the ``state`` and the task
    is a set of typed questions (:meth:`DecisionPrompt.questions`);
  * chat models: a system prompt plus the snapshot as JSON (:meth:`DecisionPrompt.messages`).

The prompts name no pair: the snapshot's ``pair`` field says which market it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Yes/no checks recorded as reason codes: key -> (question, codes if yes (long, short, no plan), code if no).
Checks = dict[str, tuple[str, tuple[str | None, str | None, str | None], str | None]]


@dataclass(frozen=True)
class DecisionPrompt:
    setup: str  # the setup name the model confirms (decision schema ``Setup``)
    version: str
    system_prompt: str
    instructions: str
    criteria: dict[str, str]
    checks: Checks

    def messages(self, snapshot: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": "Market snapshot (JSON):\n" + json.dumps(snapshot, separators=(",", ":"), default=str),
            },
        ]

    def questions(self) -> dict[str, dict[str, Any]]:
        """Typed questions for the Decisions API; the snapshot itself is sent as the ``state``."""
        questions: dict[str, dict[str, Any]] = {
            "decision": {"type": "choice", "instructions": self.instructions, "criteria": self.criteria},
        }
        for key, (question, _yes, _no) in self.checks.items():
            questions[key] = {"type": "noul", "instructions": question}
        return questions


# =========================================================================== trend pullback

PROMPT_VERSION = "decision-v4"  # trend pullback

SYSTEM_PROMPT = """You are the setup-confirmation layer of a systematic, rules-based FX research \
system trading FX CFDs on a Capital.com demo account. You receive one structured market snapshot of one \
currency pair (its "pair" field). Your only job is to judge whether the predefined TREND_PULLBACK setup is genuinely present right now.

The TREND_PULLBACK setup (long; short is the mirror image):
1. The 4h trend establishes direction (bullish for longs).
2. The 1h trend generally agrees (not opposing).
3. On 15m, price has pulled back toward a predefined support area (resistance for shorts).
4. Momentum is beginning to confirm continuation in the trend direction.
5. No high-impact news event is imminent.
6. The proposed trade has an acceptable risk/reward.

The snapshot's "market_regime" is a deterministic classification of the overall market \
(strong trends suit this setup; ranges, breakouts, compression and event risk usually do not). \
The snapshot's "setup_check" contains the deterministic rule evaluation and, when present, a \
fixed trade plan (entry, stop-loss, take-profit). You cannot change the plan, the position \
size, or any risk limit. A separate deterministic risk engine makes the final decision and may \
reject your answer.

Rules:
- Answer BUY only for a long TREND_PULLBACK that matches setup_check.trade_plan.direction=BUY; \
SELL only for a short that matches direction=SELL. Otherwise answer WAIT.
- Answer WAIT whenever the evidence is mixed, the market looks range-bound, news risk is high, \
or you are unsure. WAIT is always an acceptable answer.
- Use setup TREND_PULLBACK when you judge the setup present, otherwise NONE.
- confidence (0-1) is how clearly the snapshot shows the complete setup.
- reason_codes must come from the allowed list and explain the decision.
- Do not invent data that is not in the snapshot."""


# --------------------------------------------------------------------------- Decisions API (Jev)

DECISION_INSTRUCTIONS = """The state is one structured market snapshot from a systematic, rules-based \
research system trading FX CFDs on a Capital.com demo account; "pair" names the currency pair. Judge \
whether the predefined TREND_PULLBACK setup is genuinely present right now.

The TREND_PULLBACK setup (long; short is the mirror image):
1. The 4h trend establishes direction (bullish for longs).
2. The 1h trend generally agrees (not opposing).
3. On 15m, price has pulled back toward a predefined support area (resistance for shorts).
4. Momentum is beginning to confirm continuation in the trend direction.
5. No high-impact news event is imminent.
6. The proposed trade has an acceptable risk/reward.

"market_regime" is a deterministic classification of the overall market (strong trends suit this \
setup; ranges, breakouts, compression and event risk usually do not). "setup_check" holds the \
deterministic rule evaluation and, when present, a fixed trade plan (entry, stop-loss, take-profit). \
The plan, the position size and every risk limit are fixed; a deterministic risk engine makes the \
final decision. Use only data present in the snapshot."""

DECISION_CRITERIA = {
    "BUY": "Confirm a long TREND_PULLBACK: setup_check.trade_plan.direction is BUY and the snapshot "
    "clearly shows the complete bullish setup.",
    "SELL": "Confirm a short TREND_PULLBACK: setup_check.trade_plan.direction is SELL and the snapshot "
    "clearly shows the complete bearish setup.",
    "WAIT": "No complete setup, no trade plan or a plan in the other direction, mixed evidence, a "
    "range-bound market, high news risk, or any doubt. WAIT is always acceptable.",
}

DECISION_CHECKS: Checks = {
    "htf_trend_supports": (
        "Do the 4h and 1h trends support the direction of setup_check.trade_plan (without a plan: is "
        "there a clear 4h trend)?",
        ("HTF_BULLISH", "HTF_BEARISH", None),
        "HTF_UNCLEAR",
    ),
    "pullback_to_level": (
        "Has 15m price pulled back into a predefined support area (resistance for shorts) in the trend "
        "direction?",
        ("PULLBACK_TO_SUPPORT", "PULLBACK_TO_RESISTANCE", None),
        "NO_PULLBACK",
    ),
    "momentum_confirms": (
        "Is 15m momentum beginning to confirm continuation in the trend direction?",
        ("MOMENTUM_CONFIRMED", "MOMENTUM_CONFIRMED", "MOMENTUM_CONFIRMED"),
        "MOMENTUM_WEAK",
    ),
    "room_to_target": (
        "Is there enough room to the target for an acceptable risk/reward, with no opposing level "
        "too close?",
        ("ROOM_TO_TARGET", "ROOM_TO_TARGET", "ROOM_TO_TARGET"),
        "LEVEL_TOO_CLOSE",
    ),
    "news_risk_high": (
        "Is news or event risk elevated right now (an imminent high-impact event or strongly conflicting "
        "central-bank bias)?",
        ("NEWS_RISK_HIGH", "NEWS_RISK_HIGH", "NEWS_RISK_HIGH"),
        None,
    ),
}


TREND_PULLBACK = DecisionPrompt(
    setup="TREND_PULLBACK",
    version=PROMPT_VERSION,
    system_prompt=SYSTEM_PROMPT,
    instructions=DECISION_INSTRUCTIONS,
    criteria=DECISION_CRITERIA,
    checks=DECISION_CHECKS,
)


def build_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    return TREND_PULLBACK.messages(snapshot)


def build_decision_questions() -> dict[str, dict[str, Any]]:
    return TREND_PULLBACK.questions()


# =========================================================================== range breakout

BREAKOUT_PROMPT_VERSION = "breakout-v1"

_BREAKOUT_SETUP = """The RANGE_BREAKOUT setup (long; short is the mirror image):
1. Before the break, price moved sideways in a clear range on the 1h chart, tested at both edges.
2. The range was neither tiny (noise) nor wide (a trend leg), measured in 1h ATR.
3. The latest 15m candle closed decisively above the range high: a strong body, closing near its high.
4. The break is fresh: no earlier 15m close above the range in the last few candles (no chasing).
5. The 4h trend does not point against the break.
6. No high-impact news event is imminent, and there is room for the measured move."""

BREAKOUT_SYSTEM_PROMPT = f"""You are the setup-confirmation layer of a systematic, rules-based FX research \
system trading FX CFDs on a Capital.com demo account. You receive one structured market snapshot of one \
currency pair (its "pair" field). Your only job is to judge whether the predefined RANGE_BREAKOUT setup is \
genuinely present right now.

{_BREAKOUT_SETUP}

The snapshot's "market_regime" is a deterministic classification of the overall market (breakouts \
and compression suit this setup; strong trends and event risk usually do not). The snapshot's \
"setup_check" contains the deterministic rule evaluation, the range ("context": range high/low, \
height, edge touches) and, when present, a fixed trade plan (entry, stop-loss inside the range, \
take-profit at the measured move). You cannot change the plan, the position size, or any risk limit. \
A separate deterministic risk engine makes the final decision and may reject your answer.

Rules:
- Answer BUY only for an upside RANGE_BREAKOUT that matches setup_check.trade_plan.direction=BUY; \
SELL only for a downside break that matches direction=SELL. Otherwise answer WAIT.
- Answer WAIT when the break looks weak or likely to fail back into the range, the range is unclear, \
news risk is high, or you are unsure. WAIT is always an acceptable answer.
- Use setup RANGE_BREAKOUT when you judge the setup present, otherwise NONE.
- confidence (0-1) is how clearly the snapshot shows the complete setup.
- reason_codes must come from the allowed list and explain the decision.
- Do not invent data that is not in the snapshot."""

BREAKOUT_INSTRUCTIONS = f"""The state is one structured market snapshot from a systematic, rules-based \
research system trading FX CFDs on a Capital.com demo account; "pair" names the currency pair. Judge \
whether the predefined RANGE_BREAKOUT setup is genuinely present right now.

{_BREAKOUT_SETUP}

"market_regime" is a deterministic classification of the overall market (breakouts and compression \
suit this setup; strong trends and event risk usually do not). "setup_check" holds the deterministic \
rule evaluation, the range ("context") and, when present, a fixed trade plan (entry, stop-loss inside \
the range, take-profit at the measured move). The plan, the position size and every risk limit are \
fixed; a deterministic risk engine makes the final decision. Use only data present in the snapshot."""

BREAKOUT_CRITERIA = {
    "BUY": "Confirm an upside RANGE_BREAKOUT: setup_check.trade_plan.direction is BUY and the snapshot "
    "clearly shows a clean range and a decisive, fresh break above it.",
    "SELL": "Confirm a downside RANGE_BREAKOUT: setup_check.trade_plan.direction is SELL and the snapshot "
    "clearly shows a clean range and a decisive, fresh break below it.",
    "WAIT": "No complete setup, no trade plan or a plan in the other direction, a weak or late break, an "
    "unclear range, high news risk, or any doubt. WAIT is always acceptable.",
}

BREAKOUT_CHECKS: Checks = {
    "range_clear": (
        "Before the break, was price in a clear, well-tested sideways range on the 1h chart?",
        ("RANGE_DEFINED", "RANGE_DEFINED", "RANGE_DEFINED"),
        "RANGE_UNCLEAR",
    ),
    "breakout_decisive": (
        "Did the latest 15m candle close decisively beyond the range (strong body, close near its extreme), "
        "in the direction of setup_check.trade_plan?",
        ("BREAKOUT_CONFIRMED", "BREAKOUT_CONFIRMED", None),
        "BREAKOUT_WEAK",
    ),
    "htf_supports": (
        "Do the 4h and 1h trends support, or at least not oppose, the breakout direction?",
        ("HTF_BULLISH", "HTF_BEARISH", None),
        "HTF_OPPOSES",
    ),
    "room_to_target": (
        "Is there enough room to the measured-move target, with no strong opposing level too close?",
        ("ROOM_TO_TARGET", "ROOM_TO_TARGET", "ROOM_TO_TARGET"),
        "LEVEL_TOO_CLOSE",
    ),
    "news_risk_high": DECISION_CHECKS["news_risk_high"],
}

RANGE_BREAKOUT = DecisionPrompt(
    setup="RANGE_BREAKOUT",
    version=BREAKOUT_PROMPT_VERSION,
    system_prompt=BREAKOUT_SYSTEM_PROMPT,
    instructions=BREAKOUT_INSTRUCTIONS,
    criteria=BREAKOUT_CRITERIA,
    checks=BREAKOUT_CHECKS,
)
