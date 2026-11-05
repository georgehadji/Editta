import anthropic
import httpx2 as httpx
import pytest

from editta.adapters.llm.client import _is_rejection, _output_config, build_user_content
from editta.domain.errors import LLMRequestRejected
from editta.domain.models import CachePolicy, PromptParts

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
