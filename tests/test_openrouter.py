import json

import httpx

from app.decision.openrouter import OpenRouterClient, extract_json
from app.decision.service import DecisionService

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

    out = await DecisionService(client(handler), "typesafe/jev-1.13", 600, 0.0).decide({"pair": "EUR_USD"})
    assert out.valid and out.decision == "BUY" and out.confidence == 0.82
    assert out.source == "LLM"
    assert seen[0]["model"] == "typesafe/jev-1.13"
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
