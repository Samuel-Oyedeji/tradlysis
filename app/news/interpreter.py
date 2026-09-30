"""LLM interpretation of central-bank / macro news into a structured currency bias.

With a decision model (Jev) the article is the ``state`` and two choice questions are asked: the
policy tone (HAWKISH / DOVISH / NEUTRAL, whose probability is the confidence) and the main theme
(recorded as the reason code). Chat models get a system prompt and a JSON-schema response.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.decision.openrouter import DecisionsResult, LlmResult, OpenRouterClient, uses_decisions_api

NEWS_PROMPT_VERSION = "news-v2"


class Tone(StrEnum):
    HAWKISH = "HAWKISH"
    DOVISH = "DOVISH"
    NEUTRAL = "NEUTRAL"


class Bias(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class NewsReason(StrEnum):
    RATE_HIKE_SIGNAL = "RATE_HIKE_SIGNAL"
    RATE_CUT_SIGNAL = "RATE_CUT_SIGNAL"
    RATES_ON_HOLD = "RATES_ON_HOLD"
    INFLATION_CONCERN = "INFLATION_CONCERN"
    INFLATION_EASING = "INFLATION_EASING"
    GROWTH_STRONG = "GROWTH_STRONG"
    GROWTH_WEAK = "GROWTH_WEAK"
    LABOR_STRONG = "LABOR_STRONG"
    LABOR_WEAK = "LABOR_WEAK"
    BALANCE_SHEET_TIGHTENING = "BALANCE_SHEET_TIGHTENING"
    BALANCE_SHEET_EASING = "BALANCE_SHEET_EASING"
    FINANCIAL_STABILITY = "FINANCIAL_STABILITY"
    MORE_HAWKISH_THAN_EXPECTED = "MORE_HAWKISH_THAN_EXPECTED"
    MORE_DOVISH_THAN_EXPECTED = "MORE_DOVISH_THAN_EXPECTED"
    AS_EXPECTED = "AS_EXPECTED"
    NOT_POLICY_RELEVANT = "NOT_POLICY_RELEVANT"
    OTHER = "OTHER"


class NewsInterpretationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: str = Field(min_length=3, max_length=3)
    tone: Tone
    currency_bias: Bias
    confidence: float = Field(ge=0.0, le=1.0)
    reason_codes: list[NewsReason] = Field(min_length=1, max_length=6)
    summary: str = Field(max_length=400)


NEWS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["currency", "tone", "currency_bias", "confidence", "reason_codes", "summary"],
    "properties": {
        "currency": {"type": "string", "description": "ISO currency code affected, e.g. USD"},
        "tone": {"type": "string", "enum": [t.value for t in Tone]},
        "currency_bias": {"type": "string", "enum": [b.value for b in Bias]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason_codes": {
            "type": "array",
            "items": {"type": "string", "enum": [r.value for r in NewsReason]},
            "minItems": 1,
            "maxItems": 6,
        },
        "summary": {"type": "string", "description": "One or two sentences, max 400 characters"},
    },
}

SYSTEM_PROMPT = """You are a macro analyst classifying central-bank and economic news for an FX \
research system. You interpret language only; you never give trading instructions.
Classify the item's monetary-policy tone (HAWKISH / DOVISH / NEUTRAL) and the implied \
directional bias for the stated currency (BULLISH / BEARISH / NEUTRAL). Compare against what \
markets most likely expected when the text makes that possible. If the item is not relevant \
to monetary policy or the currency, use NEUTRAL with low confidence and NOT_POLICY_RELEVANT. \
Confidence reflects how clearly the text supports the classification (0-1)."""


def build_messages(currency: str, title: str, summary: str | None, published: str | None) -> list[dict[str, str]]:
    body = f"Currency: {currency}\nPublished: {published or 'unknown'}\nTitle: {title}\n"
    if summary:
        body += f"Text: {summary[:3000]}\n"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": body},
    ]


TONE_INSTRUCTIONS = """The state is one central-bank or economic news item for the stated currency, \
used by an FX research system. Interpret the language only. Classify the monetary-policy tone of \
the item for the stated currency's central bank, comparing against what markets most likely \
expected when the text makes that possible."""

TONE_CRITERIA = {
    "HAWKISH": "Points to tighter policy: rate hikes or rates held higher for longer, inflation concern, "
    "balance-sheet reduction. Supportive of the currency.",
    "DOVISH": "Points to looser policy: rate cuts, easing inflation, growth or labour weakness, balance-sheet "
    "easing. Negative for the currency.",
    "NEUTRAL": "Balanced or as expected, or not about monetary policy or the currency.",
}

THEME_INSTRUCTIONS = "Which theme best describes the policy content of this news item for the stated currency?"

THEME_CRITERIA = {
    "RATE_HIKE_SIGNAL": "Signals or delivers a rate increase.",
    "RATE_CUT_SIGNAL": "Signals or delivers a rate cut.",
    "RATES_ON_HOLD": "Rates unchanged with no clear signal of the next move.",
    "INFLATION_CONCERN": "Inflation too high or rising is the main concern.",
    "INFLATION_EASING": "Inflation falling or under control is the main message.",
    "GROWTH_STRONG": "Economic growth is strong.",
    "GROWTH_WEAK": "Economic growth is weak or slowing.",
    "LABOR_STRONG": "The labour market is strong.",
    "LABOR_WEAK": "The labour market is weakening.",
    "BALANCE_SHEET_TIGHTENING": "Balance-sheet reduction or quantitative tightening.",
    "BALANCE_SHEET_EASING": "Asset purchases or quantitative easing.",
    "FINANCIAL_STABILITY": "Financial stability or banking-sector concerns.",
    "NOT_POLICY_RELEVANT": "Not relevant to monetary policy or the currency (administrative, personnel, events).",
    "OTHER": "Policy-relevant but none of the above.",
}

TONE_BIAS = {Tone.HAWKISH: Bias.BULLISH, Tone.DOVISH: Bias.BEARISH, Tone.NEUTRAL: Bias.NEUTRAL}
IRRELEVANT_MAX_CONFIDENCE = 0.2


def build_news_questions() -> dict[str, dict]:
    return {
        "tone": {"type": "choice", "instructions": TONE_INSTRUCTIONS, "criteria": TONE_CRITERIA},
        "theme": {"type": "choice", "instructions": THEME_INSTRUCTIONS, "criteria": THEME_CRITERIA},
    }


def parse_news_answers(answers: dict, currency: str) -> dict:
    """Map Decisions API answers onto the interpretation schema (validated afterwards)."""
    tone_answer = answers.get("tone") or {}
    theme_answer = answers.get("theme") or {}
    tone = Tone(str(tone_answer.get("choice", "")).upper())
    theme = NewsReason(str(theme_answer.get("choice", "")).upper())
    probabilities = {str(k).upper(): v for k, v in (tone_answer.get("probabilities") or {}).items()}
    confidence = probabilities.get(tone, tone_answer.get("confidence"))
    if confidence is None:
        raise ValueError("tone answer carries no probability or confidence")
    confidence = float(confidence)
    if theme == NewsReason.NOT_POLICY_RELEVANT:
        tone, confidence = Tone.NEUTRAL, min(confidence, IRRELEVANT_MAX_CONFIDENCE)
    return {
        "currency": currency.upper(),
        "tone": tone,
        "currency_bias": TONE_BIAS[tone],
        "confidence": confidence,
        "reason_codes": [theme],
        "summary": f"{tone} for {currency.upper()} (p={confidence:.2f}); theme {theme}",
    }


async def interpret_article(
    client: OpenRouterClient,
    model: str,
    currency: str,
    title: str,
    summary: str | None,
    published: str | None,
) -> tuple[NewsInterpretationOut | None, LlmResult | DecisionsResult, str | None]:
    """Returns (validated interpretation or None, raw model result, validation error)."""
    if uses_decisions_api(model):
        state = {"currency": currency, "published": published or "unknown", "title": title}
        if summary:
            state["text"] = summary[:3000]
        decided = await client.decisions(model=model, state=state, questions=build_news_questions())
        if not decided.ok:
            return None, decided, decided.error
        try:
            return NewsInterpretationOut.model_validate(parse_news_answers(decided.answers, currency)), decided, None
        except (ValidationError, ValueError) as exc:
            return None, decided, str(exc)[:1000]

    result = await client.structured_completion(
        model=model,
        messages=build_messages(currency, title, summary, published),
        schema_name="news_interpretation",
        schema=NEWS_SCHEMA,
        max_tokens=400,
    )
    if not result.ok:
        return None, result, result.error
    try:
        return NewsInterpretationOut.model_validate(result.parsed), result, None
    except ValidationError as exc:
        return None, result, str(exc)[:1000]
