from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2 as httpx
import pytest

from editta.adapters.llm.client import (
    AnthropicLLMClient,
    _is_rejection,
    _output_config,
    build_user_content,
)
from editta.domain.errors import (
    LLMOutputInvalid,
    LLMOutputTruncated,
    LLMRequestRejected,
    LLMTimeout,
)
from editta.domain.models import CachePolicy, Finding, PassFindings, PromptParts, Severity

PARTS = PromptParts(system="sys", manuscript_block="manuscript body", instructions="do X")


def test_cache_control_attached_when_caching_requested() -> None:
    for policy in (CachePolicy.WRITE, CachePolicy.READ):
        content = build_user_content(PARTS, policy)
        assert content[0]["cache_control"] == {"type": "ephemeral"}
        assert content[1] == {"type": "text", "text": "do X"}


def test_cache_control_omitted_when_not_requested() -> None:
    content = build_user_content(PARTS, CachePolicy.NONE)
    assert "cache_control" not in content[0]


def _status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    error_type = {400: anthropic.BadRequestError, 529: anthropic.InternalServerError}.get(
        status, anthropic.APIStatusError
    )
    return error_type(message=f"status {status}", response=response, body=None)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_request_level_rejections_are_not_retried(status: int) -> None:
    assert _is_rejection(_status_error(status))


@pytest.mark.parametrize("status", [408, 409, 429, 500, 529])
def test_transient_failures_stay_retryable(status: int) -> None:
    assert not _is_rejection(_status_error(status))


def test_unknown_effort_is_a_non_retryable_rejection() -> None:
    with pytest.raises(LLMRequestRejected):
        _output_config("extreme")
    assert _output_config("high") == {"effort": "high"}


GREEK = "«ἐνδυναμώ"
FINDINGS = PassFindings(
    findings=[Finding(section="s", quote="q", issue="i", severity=Severity.MINOR, suggestion="x")]
)


def _client_answering(text: str, stop_reason: str) -> AnthropicLLMClient:
    """A client whose stream yields one message with this text."""
    message = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=64_000,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=7,
        ),
    )

    class _Stream:
        async def __aenter__(self) -> "_Stream":
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

        async def get_final_message(self) -> SimpleNamespace:
            return message

    client = AnthropicLLMClient("key")
    client._client.messages.stream = lambda **kwargs: _Stream()  # type: ignore[method-assign,assignment]
    return client


async def _analyze(client: AnthropicLLMClient) -> Any:
    return await client.analyze(
        PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model="claude-opus-5-5"
    )


@pytest.mark.asyncio
async def test_valid_answer_is_parsed_with_usage() -> None:
    result = await _analyze(_client_answering(FINDINGS.model_dump_json(), "end_turn"))

    assert result.output == FINDINGS
    assert (result.usage.output_tokens, result.usage.cache_read_tokens) == (64_000, 7)


@pytest.mark.asyncio
async def test_cut_off_answer_is_not_retryable_and_leaks_nothing() -> None:
    cut = '{"findings":[{"section":"' + GREEK  # the model quoted the manuscript, then ran out
    client = _client_answering(cut, "max_tokens")

    with pytest.raises(LLMOutputTruncated) as caught:
        await _analyze(client)

    error = caught.value
    assert not error.retryable
    assert GREEK not in str(error)
    assert error.__cause__ is None and error.__suppress_context__
    assert error.usage is not None and error.usage.output_tokens == 64_000
    assert client._breaker._consecutive_failures == 0  # the provider is healthy


@pytest.mark.asyncio
async def test_a_request_that_times_out_is_retryable_and_counts_against_the_provider() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    class _TimesOut:
        async def __aenter__(self) -> None:
            raise anthropic.APITimeoutError(request=request)

        async def __aexit__(self, *exc: object) -> None:
            return None

    client = AnthropicLLMClient("key")
    client._client.messages.stream = lambda **kwargs: _TimesOut()  # type: ignore[method-assign,assignment]

    with pytest.raises(LLMTimeout) as caught:
        await _analyze(client)

    assert caught.value.retryable
    assert client._breaker._consecutive_failures == 1


@pytest.mark.asyncio
async def test_wrong_shape_answer_is_retryable_and_leaks_nothing() -> None:
    with pytest.raises(LLMOutputInvalid) as caught:
        await _analyze(_client_answering(f'{{"findings": "{GREEK}"}}', "end_turn"))

    assert caught.value.retryable
    assert GREEK not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
