"""Versioned prompts for the decision layer. Bump PROMPT_VERSION on any wording change so
results can be grouped by prompt in the experiment analysis.

Two forms of the same task:
  * decision models (Jev) on OpenRouter's Decisions API: the snapshot is the ``state`` and the task
    is a set of typed questions (:func:`build_decision_questions`);
  * chat models: a system prompt plus the snapshot as JSON (:func:`build_messages`).
"""

from __future__ import annotations

import json
from typing import Any

PROMPT_VERSION = "decision-v3"

SYSTEM_PROMPT = """You are the setup-confirmation layer of a systematic, rules-based FX research \
system trading EUR/USD CFDs on a Capital.com demo account. You receive one structured market snapshot. Your only job \
is to judge whether the predefined TREND_PULLBACK setup is genuinely present right now.

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


def build_messages(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Market snapshot (JSON):\n" + json.dumps(snapshot, separators=(",", ":"), default=str),
        },
    ]


# --------------------------------------------------------------------------- Decisions API (Jev)

DECISION_INSTRUCTIONS = """The state is one structured market snapshot from a systematic, rules-based \
research system trading EUR/USD CFDs on a Capital.com demo account. Judge whether the predefined \
TREND_PULLBACK setup is genuinely present right now.

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

# Yes/no checks recorded as reason codes: key -> (question, codes if yes (long, short, no plan), code if no).
DECISION_CHECKS: dict[str, tuple[str, tuple[str | None, str | None, str | None], str | None]] = {
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


def build_decision_questions() -> dict[str, dict[str, Any]]:
    """Typed questions for the Decisions API; the snapshot itself is sent as the ``state``."""
    questions: dict[str, dict[str, Any]] = {
        "decision": {"type": "choice", "instructions": DECISION_INSTRUCTIONS, "criteria": DECISION_CRITERIA},
    }
    for key, (question, _yes, _no) in DECISION_CHECKS.items():
        questions[key] = {"type": "noul", "instructions": question}
    return questions
