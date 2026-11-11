"""One `llm.call` line per model call, numbers only."""

import asyncio
from typing import Any

import pytest
from structlog.testing import capture_logs

from editta.adapters.llm.instrumented import InstrumentedLLMClient
from editta.domain.errors import LLMError
from editta.domain.models import CachePolicy, LLMUsage, PassFindings, PromptParts
from tests.fakes import FakeLLMClient

AUTHOR_TEXT = "the author's own sentence"
PARTS = PromptParts(system="sys", manuscript_block=AUTHOR_TEXT, instructions="do X")


async def _call(client: Any) -> Any:
    return await client.analyze(
        PARTS, PassFindings, cache=CachePolicy.NONE, effort="high", model="m"
    )


@pytest.mark.asyncio
async def test_a_successful_call_logs_model_tokens_and_duration() -> None:
    client = InstrumentedLLMClient(FakeLLMClient(manuscript_text=AUTHOR_TEXT))

    with capture_logs() as logs:
        await _call(client)

    (line,) = [entry for entry in logs if entry["event"] == "llm.call"]
    assert (line["model"], line["served_model"], line["outcome"]) == ("m", "m", "ok")
    assert (line["input_tokens"], line["output_tokens"]) == (100, 50)
    assert isinstance(line["duration_ms"], int)
    assert AUTHOR_TEXT not in str(logs)


@pytest.mark.asyncio
async def test_a_failed_call_logs_its_error_class_and_billed_usage_then_reraises() -> None:
    usage = LLMUsage(input_tokens=7, output_tokens=3, cache_write_tokens=0, cache_read_tokens=0)
    inner = FakeLLMClient(
        manuscript_text=AUTHOR_TEXT, fail_when=lambda parts: LLMError("overloaded", usage=usage)
    )

    with capture_logs() as logs, pytest.raises(LLMError):
        await _call(InstrumentedLLMClient(inner))

    (line,) = [entry for entry in logs if entry["event"] == "llm.call"]
    assert line["outcome"] == "LLMError"
    assert (line["input_tokens"], line["output_tokens"]) == (7, 3)


@pytest.mark.asyncio
async def test_a_call_cut_off_by_a_timeout_is_logged_as_cancelled() -> None:
    class _Hangs(FakeLLMClient):
        async def analyze(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            await asyncio.sleep(60)

    client = InstrumentedLLMClient(_Hangs(manuscript_text=AUTHOR_TEXT))

    with capture_logs() as logs, pytest.raises(TimeoutError):
        await asyncio.wait_for(_call(client), timeout=0.05)

    (line,) = [entry for entry in logs if entry["event"] == "llm.call"]
    assert line["outcome"] == "CancelledError"
