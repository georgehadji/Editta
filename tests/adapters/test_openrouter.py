import json
from typing import Any

import httpx2 as httpx
import pytest

from editta.adapters.llm import build_llm_client
from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.llm.openrouter import (
    OpenRouterLLMClient,
    build_request,
    parse_usage,
    served_model,
)
from editta.domain.budget import GEMINI_ARGON, GPT_SOL, HAIKU, OPUS, SONNET
from editta.domain.errors import LLMError, LLMRequestRejected
from editta.domain.models import CachePolicy, Finding, PassFindings, PromptParts, Severity

PARTS = PromptParts(system="sys", manuscript_block="manuscript body", instructions="do X")
FINDINGS = PassFindings(
    findings=[
        Finding(
            section="intro", quote="manuscript", issue="i", severity=Severity.MINOR, suggestion="s"
        )
    ]
)


def _client(handler: Any) -> OpenRouterLLMClient:
    client = OpenRouterLLMClient("or-key")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def _sse(*chunks: dict[str, Any]) -> httpx.Response:
    events = [": OPENROUTER PROCESSING", *(f"data: {json.dumps(c)}" for c in chunks)]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content="\n\n".join([*events, "data: [DONE]"]).encode(),
    )


def _ok(content: str, model: str = "anthropic/claude-opus-5.5") -> httpx.Response:
    """A streamed answer split over two deltas, then OpenRouter's usage chunk."""
    half = len(content) // 2
    last = {"delta": {"content": content[half:]}, "finish_reason": "stop"}
    return _sse(
        {"model": model, "choices": [{"delta": {"content": content[:half]}}]},
        {"model": model, "choices": [last]},
        {
            "model": model,
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_request_maps_model_effort_cache_and_schema() -> None:
    request = build_request(PARTS, PassFindings, cache=CachePolicy.WRITE, effort="high", model=OPUS)

    assert request["model"] == "anthropic/claude-opus-5.5"
    assert "models" not in request
    assert request["stream"] is True
    assert request["reasoning"] == {"effort": "high", "exclude": True}
    assert request["messages"][0] == {"role": "system", "content": "sys"}
    assert request["messages"][1]["content"][0]["cache_control"] == {"type": "ephemeral"}
    schema = request["response_format"]["json_schema"]
    assert schema["strict"] is True
    assert schema["schema"]["additionalProperties"] is False
    assert request["provider"] == {"require_parameters": True, "zdr": True}


def test_schema_lists_every_property_as_required_for_openai_strict_mode() -> None:
    request = build_request(PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=OPUS)
    schema = request["response_format"]["json_schema"]["schema"]

    def check(node: Any) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                assert node["required"] == list(node["properties"])
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(schema)
    assert "findings" in schema["required"]  # defaulted, so transform_schema omits it


def test_fallbacks_go_in_models_after_the_primary() -> None:
    request = build_request(
        PARTS,
        PassFindings,
        cache=CachePolicy.NONE,
        effort="high",
        model=OPUS,
        fallbacks=(GEMINI_ARGON,),
    )
    assert request["models"] == ["anthropic/claude-opus-5.5", "google/gemini-4-argon"]
    assert "model" not in request


def test_served_model_maps_back_to_our_id() -> None:
    route = (OPUS, GPT_SOL)
    assert served_model({"model": "openai/gpt-6.1-sol-20261001"}, route) == GPT_SOL
    assert served_model({"model": "anthropic/claude-opus-5.5"}, route) == OPUS
    assert served_model({}, route) == OPUS  # unknown: charged to the primary


def test_same_prefix_shares_a_session_so_the_cache_stays_on_one_provider() -> None:
    other = PromptParts(system="sys", manuscript_block="manuscript body", instructions="do Y")

    def session(parts: PromptParts, model: str) -> str:
        request = build_request(
            parts, PassFindings, cache=CachePolicy.READ, effort="high", model=model
        )
        return str(request["session_id"])

    assert session(PARTS, OPUS) == session(other, OPUS)
    assert session(PARTS, OPUS) != session(PARTS, SONNET)  # caches are per model


@pytest.mark.parametrize("model", [OPUS, SONNET, HAIKU])
def test_every_routed_model_has_an_openrouter_id(model: str) -> None:
    assert build_request(PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=model)


def test_unknown_model_is_a_non_retryable_rejection() -> None:
    with pytest.raises(LLMRequestRejected):
        build_request(PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model="gpt-x")


def test_usage_splits_cached_tokens_out_of_prompt_tokens() -> None:
    usage = parse_usage(
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 50,
            "prompt_tokens_details": {"cached_tokens": 700, "cache_write_tokens": 200},
        }
    )
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (
        100,
        700,
        200,
    )
    assert usage.output_tokens == 50


@pytest.mark.asyncio
async def test_analyze_parses_structured_output() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _ok(FINDINGS.model_dump_json())

    result = await _client(handler).analyze(
        PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=HAIKU
    )

    assert result.output == FINDINGS
    assert result.model == HAIKU
    assert sent[0]["model"] == "anthropic/claude-haiku-5.5"
    assert result.usage.output_tokens == 5


@pytest.mark.asyncio
async def test_result_names_the_fallback_that_served() -> None:
    client = _client(lambda request: _ok(FINDINGS.model_dump_json(), "google/gemini-4-argon"))

    result = await client.analyze(
        PARTS,
        PassFindings,
        cache=CachePolicy.NONE,
        effort="high",
        model=OPUS,
        fallbacks=(GEMINI_ARGON,),
    )

    assert result.model == GEMINI_ARGON


@pytest.mark.asyncio
async def test_unparseable_output_is_retryable() -> None:
    client = _client(lambda request: _ok("not json"))

    with pytest.raises(LLMError) as exc_info:
        await client.analyze(PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=OPUS)
    assert exc_info.value.retryable


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 402, 404])
async def test_request_rejections_are_not_retried(status: int) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(status)
        return httpx.Response(status, json={"error": {"message": "no"}})

    with pytest.raises(LLMRequestRejected):
        await _client(handler).analyze(
            PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=OPUS
        )
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_transient_failures_are_retried_then_succeed() -> None:
    responses = iter(
        [
            httpx.Response(429, headers={"retry-after": "0"}),
            # Mid-stream failure: 200, then an error chunk.
            _sse({"error": {"code": 502, "message": "upstream"}, "choices": [{"delta": {}}]}),
            _ok(FINDINGS.model_dump_json()),
        ]
    )
    client = _client(lambda request: next(responses))

    result = await client.analyze(
        PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model=OPUS
    )

    assert result.output == FINDINGS


@pytest.mark.asyncio
async def test_byte_estimate_over_counts_greek_when_no_anthropic_key() -> None:
    greek = PromptParts(system="", manuscript_block="Ἡ ψυχὴ τοῦ Σωκράτους " * 100, instructions="")
    estimate = await OpenRouterLLMClient("or-key").count_tokens(greek, model=OPUS)
    # Measured: polytonic Greek runs ~0.9 tokens per character; the estimate must not be lower.
    assert estimate >= 0.9 * len(greek.manuscript_block)


def test_factory_prefers_openrouter_and_keeps_anthropic_for_counting() -> None:
    both = build_llm_client(anthropic_api_key="a", openrouter_api_key="o")
    assert isinstance(both, OpenRouterLLMClient)
    assert isinstance(both._token_counter, AnthropicLLMClient)
    anthropic_only = build_llm_client(anthropic_api_key="a", openrouter_api_key=None)
    assert isinstance(anthropic_only, AnthropicLLMClient)
    with pytest.raises(ValueError):
        build_llm_client(anthropic_api_key=None, openrouter_api_key=None)
