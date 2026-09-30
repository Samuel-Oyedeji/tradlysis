import json

import httpx

from app.decision.openrouter import OpenRouterClient, extract_json, uses_decisions_api
from app.decision.service import DecisionService
from app.news.interpreter import interpret_article

GOOD = {"decision": "BUY", "setup": "TREND_PULLBACK", "confidence": 0.82,
        "reason_codes": ["HTF_BULLISH", "PULLBACK_TO_SUPPORT", "MOMENTUM_CONFIRMED"], "rationale": "Clean pullback."}


def completion(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}}


def client(handler) -> OpenRouterClient:
    return OpenRouterClient("key", "https://openrouter.test/api/v1", transport=httpx.MockTransport(handler))


async def test_json_schema_mode_success():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append(body)
        assert req.headers["Authorization"] == "Bearer key"
        return httpx.Response(200, json=completion(json.dumps(GOOD)))

    out = await DecisionService(client(handler), "openai/gpt-4.1-mini", 600, 0.0).decide({"pair": "EUR_USD"})
    assert out.valid and out.decision == "BUY" and out.confidence == 0.82
    assert out.source == "LLM"
    assert seen[0]["model"] == "openai/gpt-4.1-mini"
    assert seen[0]["response_format"]["type"] == "json_schema"
    assert seen[0]["provider"] == {"require_parameters": True}
    assert out.prompt_tokens == 100


async def test_falls_back_when_schema_unsupported_and_remembers():
    modes = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        mode = body.get("response_format", {}).get("type", "prompt")
        modes.append(mode)
        if mode == "json_schema":
            return httpx.Response(404, json={"error": {"message": "No endpoints found that can handle the requested parameters"}})
        return httpx.Response(200, json=completion("```json\n" + json.dumps(GOOD) + "\n```"))

    c = client(handler)
    svc = DecisionService(c, "m", 600, 0.0)
    assert (await svc.decide({})).decision == "BUY"
    assert modes == ["json_schema", "json_object"]
    await svc.decide({})
    assert modes[-1] == "json_object"  # remembered, no second failing call


async def test_invalid_output_fails_closed_to_wait():
    bad = {**GOOD, "decision": "BUY", "setup": "NONE"}  # BUY without the setup is inconsistent
    svc = DecisionService(client(lambda r: httpx.Response(200, json=completion(json.dumps(bad)))), "m", 600, 0.0)
    out = await svc.decide({})
    assert out.decision == "WAIT" and not out.valid and out.source == "ERROR"

    extra = {**GOOD, "units": 100000}  # model trying to set size -> rejected by schema
    svc = DecisionService(client(lambda r: httpx.Response(200, json=completion(json.dumps(extra)))), "m", 600, 0.0)
    assert (await svc.decide({})).decision == "WAIT"

    svc = DecisionService(client(lambda r: httpx.Response(200, json=completion("I think you should buy"))), "m", 600, 0.0)
    assert (await svc.decide({})).decision == "WAIT"


async def test_outage_is_wait_and_does_not_downgrade():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(503, json={"error": {"message": "overloaded"}})

    out = await DecisionService(client(handler), "m", 600, 0.0).decide({})
    assert out.decision == "WAIT" and out.source == "ERROR"
    assert len(calls) == 1


async def test_missing_key_is_disabled():
    c = OpenRouterClient("")
    assert not c.enabled
    assert not (await c.structured_completion(model="m", messages=[], schema_name="x", schema={})).ok


def test_extract_json():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Sure! ```json\n{"a": 2}\n``` done') == {"a": 2}
    assert extract_json('prefix {"a": 3} suffix') == {"a": 3}
    assert extract_json("[1,2]") is None
    assert extract_json("nothing") is None


# --------------------------------------------------------------------------- Decisions API (Jev)

JEV = "typesafe/jev-1.13"
PLAN_BUY = {"setup_check": {"candidate": True, "trade_plan": {"direction": "BUY"}}}


def jev_answers(choice="BUY", p=0.8, **checks):
    probs = {"BUY": 0.05, "SELL": 0.05, "WAIT": 0.1}
    probs[choice] = p
    out = {"decision": {"type": "choice", "choice": choice, "confidence": p, "probabilities": probs}}
    for key, value in checks.items():
        out[key] = {"type": "noul", "noul": value}
    return out


def decisions_client(answers_or_handler, seen=None):
    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((req.url, json.loads(req.content)))
        if callable(answers_or_handler):
            return answers_or_handler(req)
        return httpx.Response(200, json={"answers": answers_or_handler, "model": JEV,
                                         "usage": {"input_tokens": 500, "output_tokens": 4}})

    return client(handler)


def test_decision_models_are_detected():
    assert uses_decisions_api("typesafe/jev-1.13") and uses_decisions_api("~typesafe/jev-latest")
    assert not uses_decisions_api("openai/gpt-4.1-mini")


async def test_jev_request_and_answer_mapping():
    seen = []
    answers = jev_answers("BUY", 0.74, htf_trend_supports=0.9, pullback_to_level=0.7, momentum_confirms=0.3,
                          room_to_target=0.8, news_risk_high=0.05)
    out = await DecisionService(decisions_client(answers, seen), JEV, 600, 0.0).decide(PLAN_BUY)
    url, body = seen[0]
    assert str(url) == "https://openrouter.test/api/alpha/decisions"
    assert body["model"] == JEV and body["state"] == PLAN_BUY
    assert body["questions"]["decision"]["type"] == "choice"
    assert {k for k, q in body["questions"].items() if q["type"] == "noul"} == {
        "htf_trend_supports", "pullback_to_level", "momentum_confirms", "room_to_target", "news_risk_high"}
    assert out.valid and out.source == "LLM" and out.decision == "BUY" and out.setup == "TREND_PULLBACK"
    assert out.confidence == 0.74
    assert out.reason_codes == ["HTF_BULLISH", "PULLBACK_TO_SUPPORT", "MOMENTUM_WEAK", "ROOM_TO_TARGET"]
    assert out.rationale.startswith("P(BUY)=0.74")
    assert out.prompt_tokens == 500 and out.completion_tokens == 4
    assert out.messages[0]["role"] == "questions"


async def test_jev_sell_and_wait_codes():
    sell = await DecisionService(decisions_client(jev_answers("SELL", 0.7, htf_trend_supports=0.8,
                                                              pullback_to_level=0.9)), JEV, 600, 0.0).decide({})
    assert sell.decision == "SELL" and sell.reason_codes == ["HTF_BEARISH", "PULLBACK_TO_RESISTANCE"]
    wait = await DecisionService(decisions_client(jev_answers("WAIT", 0.6, news_risk_high=0.9)), JEV, 600,
                                 0.0).decide(PLAN_BUY)
    assert wait.valid and wait.decision == "WAIT" and wait.setup == "NONE" and wait.reason_codes == ["NEWS_RISK_HIGH"]
    bare = await DecisionService(decisions_client(jev_answers("WAIT", 0.9)), JEV, 600, 0.0).decide({})
    assert bare.reason_codes == ["OTHER"]


async def test_jev_confidence_falls_back_to_confidence_field():
    answers = {"decision": {"type": "choice", "choice": "BUY", "confidence": 0.66}}
    out = await DecisionService(decisions_client(answers), JEV, 600, 0.0).decide(PLAN_BUY)
    assert out.valid and out.confidence == 0.66


async def test_jev_invalid_answers_fail_closed_to_wait():
    cases = [
        {"decision": {"type": "choice", "choice": "HOLD", "probabilities": {"HOLD": 0.9}}},  # unknown option
        {"decision": {"type": "choice", "choice": "BUY"}},  # no probability at all
        {"decision": {"type": "noul", "noul": 0.9}},  # wrong answer type
        {},  # missing answer
        {"decision": {"type": "choice", "choice": "BUY", "probabilities": {"BUY": 1.7}}},  # out of range
    ]
    for answers in cases:
        out = await DecisionService(decisions_client(answers), JEV, 600, 0.0).decide(PLAN_BUY)
        assert out.decision == "WAIT" and not out.valid and out.source == "ERROR", answers
        assert out.reason_codes == ["SCHEMA_INVALID"]


async def test_jev_http_errors_are_wait():
    body = {"message": "typesafe/jev-1.13 is a decisions model ...", "code": 400}
    out = await DecisionService(decisions_client(lambda r: httpx.Response(400, json=body)), JEV, 600, 0.0).decide({})
    assert out.decision == "WAIT" and out.source == "ERROR" and out.reason_codes == ["LLM_ERROR"]
    assert "HTTP 400" in out.validation_error

    def boom(req):
        raise httpx.ConnectError("down", request=req)

    out = await DecisionService(decisions_client(boom), JEV, 600, 0.0).decide({})
    assert out.decision == "WAIT" and "transport error" in out.validation_error
    assert not (await OpenRouterClient("").decisions(model=JEV, state={}, questions={})).ok


def news_answers(tone, p, theme):
    return {"tone": {"type": "choice", "choice": tone, "probabilities": {tone: p}},
            "theme": {"type": "choice", "choice": theme, "probabilities": {theme: 0.7}}}


async def test_jev_news_interpretation():
    seen = []
    out, result, err = await interpret_article(
        decisions_client(news_answers("HAWKISH", 0.81, "INFLATION_CONCERN"), seen), JEV, "USD",
        "FOMC statement", "Inflation remains elevated...", "2026-09-30T18:00:00Z")
    assert err is None and result.ok
    assert (out.tone, out.currency_bias, out.confidence, out.reason_codes) == ("HAWKISH", "BULLISH", 0.81, ["INFLATION_CONCERN"])
    _, body = seen[0]
    assert body["state"]["currency"] == "USD" and body["state"]["text"].startswith("Inflation")
    assert set(body["questions"]["tone"]["criteria"]) == {"HAWKISH", "DOVISH", "NEUTRAL"}

    out, _, _ = await interpret_article(decisions_client(news_answers("DOVISH", 0.9, "NOT_POLICY_RELEVANT")),
                                        JEV, "EUR", "Board appointment", None, None)
    assert out.tone == "NEUTRAL" and out.currency_bias == "NEUTRAL" and out.confidence == 0.2

    out, _, err = await interpret_article(decisions_client(news_answers("ANGRY", 0.9, "OTHER")), JEV, "EUR", "x", None, None)
    assert out is None and err
