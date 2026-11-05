import io
from uuid import uuid4

import docx
import pytest

from editta.domain.errors import LLMError
from editta.domain.models import CachePolicy, ClaimedJob, JobOutcomeKind, PromptParts
from editta.domain.states import JobState, retry_delay_seconds
from editta.services.jobs import JobRunner, _pass_checkpoint_key
from tests.fakes import FailureRule, FakeLLMClient
from tests.services.fakes import FakeBlobStore, FakeJobUnitOfWork, _PassResultRecord

MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."
STRUCTURE_MARKER_PREFIX = "Analyze structure"


def _docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class Harness:
    def __init__(self, *, llm: FakeLLMClient | None = None, cost_ceiling_usd: float = 6.0) -> None:
        self.tenant_id, self.job_id, self.manuscript_id = uuid4(), uuid4(), uuid4()
        self.uow = FakeJobUnitOfWork()
        self.blobs = FakeBlobStore()
        self.llm = llm or FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)
        self.runner = JobRunner(
            uow=self.uow,
            blobs=self.blobs,  # type: ignore[arg-type]
            llm=self.llm,
            cost_ceiling_usd=cost_ceiling_usd,
        )
        self.uow.manuscripts.seed(
            self.tenant_id, self.manuscript_id, blob_key="raw/essay", title="essay.docx"
        )
        self.uow.jobs.seed(self.tenant_id, self.job_id, self.manuscript_id)
        self.blobs.seed("raw/essay", _docx_bytes(MANUSCRIPT_TEXT))

    def claimed(self, attempt: int = 0) -> ClaimedJob:
        return ClaimedJob(
            job_id=self.job_id,
            tenant_id=self.tenant_id,
            manuscript_id=self.manuscript_id,
            attempt=attempt,
        )

    @property
    def job(self) -> object:
        return self.uow.jobs._records[(self.tenant_id, self.job_id)]

    def ledger(self, reason: str) -> list[int]:
        return [e["delta"] for e in self.uow.ledger.entries if e["reason"] == reason]

    async def checkpointed(self) -> set[str]:
        return {r.pass_name for r in await self.uow.pass_results.list_for_job(self.job_id)}


def _fail_pass(marker: str, error: BaseException) -> FailureRule:
    def rule(parts: PromptParts) -> BaseException | None:
        return error if parts.instructions.startswith(marker) else None

    return rule


@pytest.mark.asyncio
async def test_happy_path_completes_writes_report_and_reserves_one_credit() -> None:
    h = Harness()

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.COMPLETED
    assert [s for s, _ in h.uow.jobs.state_history] == [
        JobState.PARSING,
        JobState.ANALYZING,
        JobState.SYNTHESIZING,
        JobState.EXPORTING,
        JobState.COMPLETED,
    ]
    assert len(await h.checkpointed()) == 4
    assert len(h.uow.reports.created) == 1
    assert h.ledger("job") == [-1]
    assert h.ledger("refund") == []
    assert h.job.cost_input_tokens == 4 * 100  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_each_step_commits_in_its_own_transaction() -> None:
    h = Harness()

    await h.runner.run(h.claimed())

    # reserve, parsing, analyzing, 4 checkpoints, 4 checkpoint loads, synthesizing,
    # exporting, report+completed — never one long transaction around LLM calls.
    assert h.uow.commits >= 10
    assert h.uow.rollbacks == 0


@pytest.mark.asyncio
async def test_resume_runs_only_missing_passes_and_reprimes_the_cache() -> None:
    h = Harness()
    for pass_name in ("language", "structure"):
        key = _pass_checkpoint_key(h.job_id, pass_name)
        h.blobs.seed(key, b"[]")
        usage = {"input_tokens": 1, "output_tokens": 1}
        h.uow.pass_results.seed(_PassResultRecord(h.job_id, pass_name, key, "fake-model", usage))

    outcome = await h.runner.run(h.claimed(attempt=1))

    assert outcome.kind is JobOutcomeKind.COMPLETED
    # 2 missing passes + synthesis; the first remaining pass re-writes the cache.
    assert len(h.llm.calls) == 3
    assert h.llm.calls[0][1] is CachePolicy.WRITE


@pytest.mark.asyncio
async def test_projected_budget_over_ceiling_dies_before_any_billable_call() -> None:
    h = Harness(cost_ceiling_usd=0.01)

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    assert "exceeds ceiling" in (outcome.error or "")
    assert h.llm.calls == []  # the guardrail now exists on the worker path too
    assert h.uow.jobs.state_history[-1][0] is JobState.DEAD
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_too_long_manuscript_dies_with_token_message_and_refund() -> None:
    class HugeTokenLLM(FakeLLMClient):
        async def count_tokens(self, parts: PromptParts) -> int:
            return 800_000

    h = Harness(llm=HugeTokenLLM(manuscript_text=MANUSCRIPT_TEXT))

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    assert "800,000 tokens" in (outcome.error or "")
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_retryable_failure_keeps_sibling_checkpoints_and_backs_off() -> None:
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass(STRUCTURE_MARKER_PREFIX, LLMError("overloaded")),
    )
    h = Harness(llm=llm)

    outcome = await h.runner.run(h.claimed(attempt=0))

    assert outcome.kind is JobOutcomeKind.RETRY
    assert outcome.retry_after_seconds == retry_delay_seconds(1)
    assert h.job.state is JobState.PENDING  # type: ignore[attr-defined]
    assert h.job.attempt == 1  # type: ignore[attr-defined]
    # The core regression: completed passes are committed, not rolled back with the
    # failure — so the retry won't pay for them again.
    assert await h.checkpointed() == {"language", "metadata", "integrity"}
    assert h.ledger("job") == [-1]
    assert h.ledger("refund") == []  # not refunded: the job is still going to run


@pytest.mark.asyncio
async def test_retry_after_failure_reruns_only_the_failed_pass() -> None:
    failing = {"active": True}

    def rule(parts: PromptParts) -> BaseException | None:
        if failing["active"] and parts.instructions.startswith(STRUCTURE_MARKER_PREFIX):
            return LLMError("overloaded")
        return None

    llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT, fail_when=rule)
    h = Harness(llm=llm)
    await h.runner.run(h.claimed(attempt=0))
    calls_before_retry = len(llm.calls)
    failing["active"] = False

    outcome = await h.runner.run(h.claimed(attempt=1))

    assert outcome.kind is JobOutcomeKind.COMPLETED
    assert len(llm.calls) - calls_before_retry == 2  # structure + synthesis only
    assert h.ledger("job") == [-1]  # one reservation across both attempts


@pytest.mark.asyncio
async def test_retryable_failure_on_last_attempt_goes_dead_and_refunds() -> None:
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass("Analyze language", LLMError("overloaded")),
    )
    h = Harness(llm=llm)

    outcome = await h.runner.run(h.claimed(attempt=2))

    assert outcome.kind is JobOutcomeKind.DEAD
    assert h.job.attempt == 3  # type: ignore[attr-defined]
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_unexpected_exception_is_recorded_without_leaking_its_message() -> None:
    leak_probe = "verbatim manuscript sentence that must never reach the error column"
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass("Analyze language", KeyError(leak_probe)),
    )
    h = Harness(llm=llm)

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.RETRY  # bugs get the benefit of the doubt
    assert outcome.error == "internal error (KeyError)"
    assert leak_probe not in (h.job.error or "")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_unparseable_manuscript_dies_immediately() -> None:
    h = Harness()
    h.blobs.seed("raw/essay", b"PK\x03\x04 not really a zip")

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    assert h.job.attempt == 1  # type: ignore[attr-defined]
    assert h.llm.calls == []
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_missing_manuscript_does_not_mint_a_credit() -> None:
    h = Harness()
    h.uow.manuscripts._records.clear()

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    # Reservation committed in its own transaction, so the refund nets to zero.
    assert sum(h.ledger("job") + h.ledger("refund")) == 0
