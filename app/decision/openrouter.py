"""OpenRouter chat-completions client with schema-constrained output.

Reference: https://openrouter.ai/docs

Structured output strategy (per model, remembered after the first success):
  1. ``response_format: json_schema`` (strict) with ``provider.require_parameters=true`` so the
     request is only routed to providers that honour the schema.
  2. ``response_format: json_object`` if the model/provider rejects json_schema.
  3. Plain prompt instructions.
Whatever mode is used, the caller validates the parsed JSON with Pydantic and fails closed
(treats anything invalid as WAIT / no bias).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

MODES = ("json_schema", "json_object", "prompt")
# HTTP statuses that indicate "this parameter combination is not supported" rather than an outage.
_UNSUPPORTED_STATUSES = {400, 404, 422}


@dataclass
class LlmResult:
    ok: bool
    model: str
    mode: str | None = None
    parsed: Any = None
    content: str | None = None
    raw_response: dict[str, Any] | None = None
    error: str | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)


class OpenRouterClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://openrouter.ai/api/v1",
        app_url: str = "",
        app_name: str = "",
        timeout: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if app_url:
            headers["HTTP-Referer"] = app_url
        if app_name:
            headers["X-Title"] = app_name
        self.enabled = bool(api_key)
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        self._mode_by_model: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def structured_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int = 600,
        temperature: float = 0.0,
    ) -> LlmResult:
        if not self.enabled:
            return LlmResult(ok=False, model=model, error="OPENROUTER_API_KEY is not configured")

        start_mode = self._mode_by_model.get(model, MODES[0])
        modes = MODES[MODES.index(start_mode) :]
        result = LlmResult(ok=False, model=model)
        t0 = time.monotonic()
        for mode in modes:
            body = self._body(model, messages, schema_name, schema, max_tokens, temperature, mode)
            try:
                resp = await self._client.post("/chat/completions", json=body)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                result.error = f"transport error: {exc!r}"
                result.attempts.append({"mode": mode, "error": result.error})
                break  # outage, not a capability problem: do not downgrade the mode
            data = _json_or_text(resp)
            if resp.status_code != 200 or "error" in data:
                err = data.get("error") if isinstance(data.get("error"), dict) else data
                result.error = f"HTTP {resp.status_code}: {json.dumps(err)[:500]}"
                result.attempts.append({"mode": mode, "status": resp.status_code, "error": err})
                if resp.status_code in _UNSUPPORTED_STATUSES and mode != MODES[-1]:
                    log.info("Model %s rejected %s mode (%s); trying next mode", model, mode, resp.status_code)
                    continue
                break

            result.raw_response = data
            result.mode = mode
            usage = data.get("usage") or {}
            result.prompt_tokens = usage.get("prompt_tokens")
            result.completion_tokens = usage.get("completion_tokens")
            content = _message_content(data)
            result.content = content
            parsed = extract_json(content) if content else None
            if parsed is None:
                result.error = "response did not contain a JSON object"
                result.attempts.append({"mode": mode, "error": result.error})
                break
            result.parsed = parsed
            result.ok = True
            result.error = None
            self._mode_by_model[model] = mode
            break
        result.latency_ms = int((time.monotonic() - t0) * 1000)
        return result

    @staticmethod
    def _body(
        model: str,
        messages: list[dict[str, str]],
        schema_name: str,
        schema: dict[str, Any],
        max_tokens: int,
        temperature: float,
        mode: str,
    ) -> dict[str, Any]:
        msgs = list(messages)
        body: dict[str, Any] = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if mode == "json_schema":
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            }
            body["provider"] = {"require_parameters": True}
        else:
            if mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            msgs = msgs + [
                {
                    "role": "system",
                    "content": "Respond with ONLY a single JSON object (no prose, no code fences) "
                    "that validates against this JSON Schema:\n" + json.dumps(schema),
                }
            ]
        body["messages"] = msgs
        return body


def _json_or_text(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}
    except ValueError:
        return {"error": {"raw": resp.text[:1000]}}


def _message_content(data: dict[str, Any]) -> str | None:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, list):  # some providers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content if isinstance(content, str) else None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    text = text.strip()
    for candidate in (text, *(m.group(1).strip() for m in _FENCE_RE.finditer(text))):
        try:
            value = json.loads(candidate)
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            if isinstance(value, dict):
                return value
        except ValueError:
            return None
    return None
