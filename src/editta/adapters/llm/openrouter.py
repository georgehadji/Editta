"""OpenRouter adapter for the LLMClient port: the same Claude models, routed through
OpenRouter's Chat Completions API (openrouter.ai/docs).

Mapping from the Anthropic request the AnthropicLLMClient sends:
  * cache_control on the manuscript block      -> passed through (Anthropic providers)
  * thinking adaptive + output_config.effort   -> `reasoning: {effort}` (reasoning-tokens guide)
  * structured output schema                   -> `response_format` json_schema, strict
  * prompt-cache hits across parallel passes   -> `session_id` from the prefix, so every
    call with the same prefix sticks to the provider endpoint that holds its cache.

Beyond that request: a pass's fallback models go in `models` (OpenRouter tries them in
order when the first errors), only zero-data-retention endpoints are used (client
manuscripts), and responses are streamed: long non-streamed calls get disconnected.
"""

import asyncio
import contextlib
import hashlib
import json
from typing import Any, Final, TypeVar

import httpx2 as httpx
from anthropic import transform_schema
from pydantic import BaseModel, ValidationError

from editta.adapters.llm.client import (
    MAX_OUTPUT_TOKENS,
    AnthropicLLMClient,
    _output_config,
    build_user_content,
    output_error,
)
from editta.adapters.llm.limits import REQUEST_TIMEOUT_SECONDS
from editta.adapters.llm.resilience import CircuitBreaker, TokenBucket
from editta.domain.budget import (
    GEMINI_ARGON,
    GEMINI_FLASH,
    GPT_SOL,
    GPT_TERRA,
    HAIKU,
    OPUS,
    SONNET,
)
from editta.domain.errors import LLMError, LLMRequestRejected, LLMTimeout
from editta.domain.models import CachePolicy, LLMResult, LLMUsage, PromptParts

T = TypeVar("T", bound=BaseModel)

API_URL: Final = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS: Final = {
    OPUS: "anthropic/claude-opus-5.5",
    SONNET: "anthropic/claude-sonnet-5.5",
    HAIKU: "anthropic/claude-haiku-5.5",
    GEMINI_ARGON: "google/gemini-4-argon",
    GEMINI_FLASH: "google/gemini-3.8-flash",
    GPT_SOL: "openai/gpt-6.1-sol",
    GPT_TERRA: "openai/gpt-6.1-terra",
}
# 402 = out of OpenRouter credits: retrying cannot help either.
_NON_RETRYABLE_STATUSES: Final = frozenset({400, 401, 402, 403, 404, 413, 422})
MAX_ATTEMPTS: Final = 3


def _openrouter_model(model: str) -> str:
    try:
        return OPENROUTER_MODELS[model]
    except KeyError:
        raise LLMRequestRejected(f"no OpenRouter route for model {model!r}") from None


def _strict_schema(node: Any) -> Any:
    """OpenAI strict mode needs every property listed in `required`; Anthropic's
    transform_schema leaves defaulted fields out. Listing them changes nothing for
    validation: the model just always emits them."""
    if isinstance(node, dict):
        if "properties" in node:
            node["required"] = list(node["properties"])
        for value in node.values():
            _strict_schema(value)
    elif isinstance(node, list):
        for value in node:
            _strict_schema(value)
    return node


def build_request(
    parts: PromptParts,
    schema: type[BaseModel],
    *,
    cache: CachePolicy,
    effort: str,
    model: str,
    fallbacks: tuple[str, ...] = (),
) -> dict[str, Any]:
    route = [_openrouter_model(m) for m in (model, *fallbacks)]
    return {
        # `models` replaces `model` when there is anything to fall back to.
        **({"models": route} if fallbacks else {"model": route[0]}),
        "stream": True,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "messages": [
            {"role": "system", "content": parts.system},
            {"role": "user", "content": build_user_content(parts, cache)},
        ],
        "reasoning": {**_output_config(effort), "exclude": True},
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": _strict_schema(transform_schema(schema)),
            },
        },
        # Only endpoints that honour every parameter above (structured output) and keep
        # no copy of the manuscript.
        "provider": {"require_parameters": True, "zdr": True},
        "session_id": hashlib.sha256(
            f"{model}\0{parts.system}\0{parts.manuscript_block}".encode()
        ).hexdigest(),
    }


def served_model(body: dict[str, Any], route: tuple[str, ...]) -> str:
    """Our id for the model that answered, from the response's `model`. Providers may
    append a version suffix; an unrecognised name is charged to the primary."""
    served = str(body.get("model") or "")
    for model in route:
        if served.startswith(OPENROUTER_MODELS[model]):
            return model
    return route[0]


def parse_usage(usage: dict[str, Any]) -> LLMUsage:
    """OpenRouter's prompt_tokens include cached and cache-written tokens; the port's
    input_tokens (like Anthropic's) count only the uncached remainder."""
    details = usage.get("prompt_tokens_details") or {}
    cache_read = int(details.get("cached_tokens") or 0)
    cache_write = int(details.get("cache_write_tokens") or 0)
    return LLMUsage(
        input_tokens=max(int(usage.get("prompt_tokens") or 0) - cache_read - cache_write, 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        cache_write_tokens=cache_write,
        cache_read_tokens=cache_read,
    )


class OpenRouterLLMClient:
    """LLMClient over OpenRouter. Token counts come from Anthropic's free count_tokens
    when `token_counter` is given; otherwise from a conservative byte estimate."""

    def __init__(
        self,
        api_key: str,
        *,
        token_counter: AnthropicLLMClient | None = None,
        max_concurrency: int = 4,
        rate_limit_per_minute: int = 50,
    ) -> None:
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}", "X-Title": "Editta"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        self._token_counter = token_counter
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._breaker = CircuitBreaker()
        self._bucket = TokenBucket(
            capacity=rate_limit_per_minute, refill_per_second=rate_limit_per_minute / 60
        )

    async def count_tokens(self, parts: PromptParts, *, model: str) -> int:
        if self._token_counter is not None:
            return await self._token_counter.count_tokens(parts, model=model)
        # shortcut: OpenRouter has no token-count endpoint. UTF-8 bytes / 2 over-counts
        # polytonic Greek by ~15% and English ~2x (safe for the budget guardrail); set
        # EDITTA_ANTHROPIC_API_KEY too for exact, free counts.
        text = parts.system + parts.manuscript_block + parts.instructions
        return len(text.encode()) // 2

    async def analyze(
        self,
        parts: PromptParts,
        schema: type[T],
        *,
        cache: CachePolicy,
        effort: str,
        model: str,
        fallbacks: tuple[str, ...] = (),
    ) -> LLMResult[T]:
        request = build_request(
            parts, schema, cache=cache, effort=effort, model=model, fallbacks=fallbacks
        )
        self._breaker.before_call()
        async with self._semaphore:
            try:
                body = await self._post(request)
            except LLMRequestRejected:
                raise
            except LLMError:
                self._breaker.record_failure()
                raise
        # The provider answered, so a bad answer is not a provider-health failure.
        self._breaker.record_success()
        usage = parse_usage(body.get("usage") or {})
        return LLMResult(
            output=_parse_output(body, schema, usage),
            usage=usage,
            model=served_model(body, (model, *fallbacks)),
        )

    async def _post(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST with retry on 408/409/429/5xx, network errors and errors reported inside
        a 200 (mid-stream), honouring retry-after."""
        for attempt in range(MAX_ATTEMPTS):
            await self._bucket.acquire()
            delay = 2.0**attempt
            try:
                async with self._http.stream("POST", API_URL, json=request) as response:
                    if response.status_code in _NON_RETRYABLE_STATUSES:
                        await response.aread()
                        # Status only: a refusal body can quote the input (moderation
                        # flags carry a snippet), and this message is kept in job.error.
                        raise LLMRequestRejected(
                            f"OpenRouter rejected the request ({response.status_code})"
                        )
                    if response.is_success:
                        body = await _read_body(response)
                        if not body.get("error"):
                            return body
                        # Code only, for the same reason as the rejection above.
                        code = (
                            body["error"].get("code") if isinstance(body["error"], dict) else None
                        )
                        error = LLMError(f"OpenRouter provider error (code {code})")
                    else:
                        error = LLMError(f"OpenRouter call failed ({response.status_code})")
                        with contextlib.suppress(ValueError):  # HTTP-date: keep the backoff
                            delay = float(response.headers.get("retry-after") or delay)
            except httpx.TimeoutException:
                error = LLMTimeout("OpenRouter call timed out")
            except httpx.HTTPError as exc:
                error = LLMError(f"OpenRouter call failed: {exc}")
            except json.JSONDecodeError:
                # Not str(exc): a decode error quotes the text it choked on.
                error = LLMError("OpenRouter sent an unreadable response")
            if attempt + 1 < MAX_ATTEMPTS:
                await asyncio.sleep(delay)
        raise error


async def _read_body(response: httpx.Response) -> dict[str, Any]:
    """The response as one non-streamed body: SSE chunks are folded into a single
    choice; a plain JSON body (an error sent before streaming began) is returned as is."""
    if not response.headers.get("content-type", "").startswith("text/event-stream"):
        await response.aread()
        plain: dict[str, Any] = response.json()
        return plain
    body: dict[str, Any] = {}
    content: list[str] = []
    finish_reason = None
    async for line in response.aiter_lines():
        # Blank lines separate events; ":" lines are keep-alive comments.
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            break
        chunk = json.loads(data)
        if chunk.get("error"):
            return {"error": chunk["error"]}
        for key in ("model", "usage"):
            if chunk.get(key):
                body[key] = chunk[key]
        for choice in chunk.get("choices") or []:
            content.append((choice.get("delta") or {}).get("content") or "")
            finish_reason = choice.get("finish_reason") or finish_reason
    body["choices"] = [{"message": {"content": "".join(content)}, "finish_reason": finish_reason}]
    return body


def _parse_output[T: BaseModel](body: dict[str, Any], schema: type[T], usage: LLMUsage) -> T:
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    try:
        return schema.model_validate(json.loads(content or ""))
    except (json.JSONDecodeError, ValidationError):
        # `from None`: both exceptions quote the invalid input, i.e. the manuscript.
        raise output_error(choice.get("finish_reason"), usage) from None
