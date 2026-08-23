"""An OpenAI-compatible chat-completions client behind the app's Anthropic
client interface (Providers Tier 2 — docs/PROVIDERS-FEASIBILITY.md).

Every AI feature module calls ``await client.messages.create(...)`` and reads
``resp.content`` text blocks plus ``resp.usage`` token fields — the de facto
interface the test fakes pin. This module emulates that shape over the OpenAI
chat-completions wire format via httpx (already a dependency; no OpenAI SDK,
so the lazy-import rule for optional AI packages stays moot), which buys
OpenAI and every serious local runtime — Ollama, LM Studio, llama.cpp server,
vLLM — in one adapter. Nothing here imports ``anthropic``.

Translation notes, in one place:

- ``system`` blocks are flattened to a single leading ``{"role": "system"}``
  message; ``cache_control`` keys are dropped here (prompt caching is
  Anthropic wire semantics), so no call site changes for the compat path.
- ``thinking`` is accepted and dropped. aicfg.thinking_kwargs already returns
  ``{}`` for non-Anthropic ids, so it never actually arrives; accepting it
  anyway means a future caller can't 400 the endpoint by association.
- ``output_config`` (json_schema) becomes ``response_format`` with
  ``additionalProperties: false`` injected into every object node — the
  OpenAI dialect requires it. ``strict`` is deliberately NOT set: strict mode
  demands ``required`` list every property, and the scoring schema is built
  dynamically from the user's criteria doc. Runtimes honor non-strict
  json_schema, and every schema-forced module validates and retries anyway.
- A runtime that 400s the schema-carrying request gets ONE free retry with
  the schema inlined into the system text instead (llama.cpp-era fallback);
  the module-level validators remain the real output contract.
- ``max_tokens`` is sent as ``max_tokens``. Known limitation: OpenAI's newest
  hosted models want ``max_completion_tokens``; every local runtime (and
  OpenAI's mainstream tier) accepts ``max_tokens``. Revisit on demand.

Errors are typed to mirror the Anthropic SDK's names because consumers
duck-type them: scoring's ``_is_rate_limit`` matches the class name
``RateLimitError`` or a 429 on ``exc.response`` — both work here.
"""

import asyncio
import copy
import json
import random
from types import SimpleNamespace

import httpx

# The request budget for a single interactive call; matches the 300s proxy
# budget the compose client comment cites. Connects fail much faster so an
# unreachable endpoint errors promptly instead of hanging the full budget.
DEFAULT_TIMEOUT = 300.0
CONNECT_TIMEOUT = 10.0
PROBE_TIMEOUT = 10.0

_BACKOFF_BASE = 0.5
_BACKOFF_CAP = 30.0


class OpenAICompatError(Exception):
    """Base for every error this adapter raises deliberately."""


class APIConnectionError(OpenAICompatError):
    """The endpoint could not be reached (DNS, refused, timeout)."""


class APIStatusError(OpenAICompatError):
    """A non-success HTTP status from the endpoint."""

    def __init__(self, message: str, *, response: httpx.Response):
        super().__init__(message)
        self.response = response
        self.status_code = response.status_code


class AuthenticationError(APIStatusError):
    """401/403 — the configured key was rejected."""


class RateLimitError(APIStatusError):
    """429. The class NAME is load-bearing: scoring._is_rate_limit duck-types
    ``exc.__class__.__name__ == "RateLimitError"`` (and the attached
    ``.response.status_code == 429`` also matches)."""


def _flatten_content(content) -> str:
    """A message's text, whether it arrived as a string or as Anthropic-style
    text blocks. Anything else is a caller bug — fail loudly, not lossily."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            else:
                raise TypeError(f"unsupported content block: {block!r}")
        return "".join(parts)
    raise TypeError(f"unsupported message content: {type(content).__name__}")


def _translate_schema(schema: dict) -> dict:
    """The app's JSON schema in the OpenAI dialect: a deep copy with
    ``additionalProperties: false`` on every object node that lacks it. The
    schemas are structural-keywords-only by test, so nothing else needs
    rewriting; the source dict is never mutated."""
    out = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node.setdefault("additionalProperties", False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(out)
    return out


def _schema_of(output_config) -> dict | None:
    """The JSON schema inside an Anthropic-style ``output_config``, or None."""
    if not output_config:
        return None
    fmt = output_config.get("format") or {}
    if fmt.get("type") == "json_schema":
        return fmt.get("schema")
    return None


def _map_response(data: dict):
    """The chat-completions response in the shape every consumer reads:
    ``resp.content`` text blocks and Anthropic-named ``resp.usage`` fields.
    Absent usage maps to None — consumers already guard with getattr."""
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        raise OpenAICompatError("endpoint response carried no choices")
    text = message.get("content") or ""
    if not isinstance(text, str):
        text = _flatten_content(text)
    usage = None
    raw = data.get("usage")
    if isinstance(raw, dict):
        details = raw.get("prompt_tokens_details") or {}
        cached = details.get("cached_tokens") if isinstance(details, dict) else 0
        usage = SimpleNamespace(
            input_tokens=raw.get("prompt_tokens", 0) or 0,
            output_tokens=raw.get("completion_tokens", 0) or 0,
            cache_read_input_tokens=cached or 0,
            cache_creation_input_tokens=0,
        )
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=usage,
        model=data.get("model"),
    )


def _status_error(response: httpx.Response) -> APIStatusError:
    """The right typed error for a non-success response. The body excerpt aids
    server-side logs; endpoint handlers never surface it to the user raw."""
    excerpt = response.text[:200].replace("\n", " ") if response.text else ""
    msg = f"endpoint returned {response.status_code}: {excerpt}".rstrip(": ")
    if response.status_code in (401, 403):
        return AuthenticationError(msg, response=response)
    if response.status_code == 429:
        return RateLimitError(msg, response=response)
    return APIStatusError(msg, response=response)


def _retry_delay(attempt: int, retry_after: str | None) -> float:
    """Exponential backoff with jitter, honoring a numeric Retry-After."""
    if retry_after:
        try:
            return min(float(retry_after), _BACKOFF_CAP)
        except ValueError:
            pass
    base = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_CAP)
    return base * (0.5 + random.random() / 2)


class _Messages:
    def __init__(self, client: "OpenAICompatClient"):
        self._client = client

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        messages: list,
        system=None,
        temperature: float | None = None,
        thinking=None,  # accepted and dropped — Anthropic wire semantics
        output_config=None,
    ):
        return await self._client._create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            system=system,
            temperature=temperature,
            output_config=output_config,
        )


class OpenAICompatClient:
    """``client.messages.create(...)`` against ``{base_url}/chat/completions``.

    One httpx request per call, no pooling: the app builds clients per request
    and never closes them (the AsyncAnthropic lifecycle), so holding sockets
    would leak. ``transport`` is the offline-test seam (httpx.MockTransport).
    ``max_retries`` mirrors the SDK's constructor knob the four construction
    sites already size deliberately (6/4/2/0).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        max_retries: int = 2,
        timeout: float = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or None
        self._max_retries = max_retries
        self._timeout = timeout
        self._transport = transport
        self.messages = _Messages(self)

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _build_payload(
        self, *, model, max_tokens, messages, system, temperature, schema, inline_schema
    ) -> dict:
        system_text = _flatten_content(system) if system else ""
        if schema is not None and inline_schema:
            note = (
                "Reply with ONLY a raw JSON object matching this JSON Schema, "
                "no prose and no code fences:\n" + json.dumps(schema)
            )
            system_text = f"{system_text}\n\n{note}" if system_text else note
        chat: list[dict] = []
        if system_text:
            chat.append({"role": "system", "content": system_text})
        for m in messages:
            chat.append({"role": m["role"], "content": _flatten_content(m["content"])})
        payload: dict = {"model": model, "messages": chat, "max_tokens": max_tokens}
        if temperature is not None:
            payload["temperature"] = temperature
        if schema is not None and not inline_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "output", "schema": _translate_schema(schema)},
            }
        return payload

    async def _post(self, payload: dict) -> httpx.Response:
        timeout = httpx.Timeout(self._timeout, connect=CONNECT_TIMEOUT)
        async with httpx.AsyncClient(transport=self._transport, timeout=timeout) as http:
            return await http.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
            )

    async def _create(self, *, model, max_tokens, messages, system, temperature, output_config):
        schema = _schema_of(output_config)
        inline_schema = False
        attempt = 0
        while True:
            payload = self._build_payload(
                model=model,
                max_tokens=max_tokens,
                messages=messages,
                system=system,
                temperature=temperature,
                schema=schema,
                inline_schema=inline_schema,
            )
            try:
                resp = await self._post(payload)
            except httpx.TransportError as exc:
                if attempt >= self._max_retries:
                    raise APIConnectionError(f"could not reach endpoint: {exc}") from exc
                await asyncio.sleep(_retry_delay(attempt, None))
                attempt += 1
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    raise OpenAICompatError("endpoint returned 200 with a non-JSON body")
                return _map_response(data)
            # A 400 on a schema-carrying request: assume weak/absent
            # response_format support and fall back ONCE to the inline-schema
            # prompt. Does not count against the retry budget; a second 400
            # is a real error.
            if resp.status_code == 400 and schema is not None and not inline_schema:
                inline_schema = True
                continue
            error = _status_error(resp)
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if isinstance(error, AuthenticationError) or not retryable or attempt >= self._max_retries:
                raise error
            await asyncio.sleep(_retry_delay(attempt, resp.headers.get("retry-after")))
            attempt += 1


async def probe(
    base_url: str,
    api_key: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """One zero-token liveness check: GET ``{base_url}/models`` — served by
    OpenAI, Ollama, LM Studio, llama.cpp server, and vLLM — distinguishing a
    rejected key (AuthenticationError) from an unreachable endpoint
    (APIConnectionError). No retries: it is a probe, fail fast. The model-id
    list rides back best-effort for the Settings datalist; a 200 whose body we
    can't parse is still ok."""
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(PROBE_TIMEOUT, connect=PROBE_TIMEOUT)
    try:
        async with httpx.AsyncClient(transport=transport, timeout=timeout) as http:
            resp = await http.get(f"{base_url.rstrip('/')}/models", headers=headers)
    except httpx.TransportError as exc:
        raise APIConnectionError(f"could not reach endpoint: {exc}") from exc
    if resp.status_code != 200:
        raise _status_error(resp)
    models: list[str] = []
    try:
        data = resp.json()
        for entry in data.get("data", []):
            model_id = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(model_id, str):
                models.append(model_id)
    except (ValueError, AttributeError):
        pass
    return {"ok": True, "models": models}
