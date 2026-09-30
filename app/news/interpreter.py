"""LLM interpretation of central-bank / macro news into a structured currency bias."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.decision.openrouter import LlmResult, OpenRouterClient

NEWS_PROMPT_VERSION = "news-v1"


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


async def interpret_article(
    client: OpenRouterClient,
    model: str,
    currency: str,
    title: str,
    summary: str | None,
    published: str | None,
) -> tuple[NewsInterpretationOut | None, LlmResult, str | None]:
    """Returns (validated interpretation or None, raw LLM result, validation error)."""
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
