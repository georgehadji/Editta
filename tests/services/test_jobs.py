import io
import time
from collections.abc import AsyncIterator
from uuid import uuid4

import docx
import pytest

from editta.domain.artifacts import ArtifactKind, ExportContext
from editta.domain.errors import LLMError, LLMOutputInvalid, LLMOutputTruncated, ParseError
from editta.domain.models import CachePolicy, ClaimedJob, JobOutcomeKind, LLMUsage, PromptParts
from editta.domain.states import JobState, retry_delay_seconds
from editta.services.jobs import DEFAULT_EXPORTERS, JobRunner, _pass_checkpoint_key
from tests.fakes import FailureRule, FakeLLMClient
from tests.services.fakes import FakeBlobStore, FakeJobUnitOfWork, _PassResultRecord

MANUSCRIPT_TEXT = "This is a Greek essay about language and structure and much more content here."
STRUCTURE_MARKER_PREFIX = "Analyze structure"
WORKER = "worker-1"


def _docx_bytes(text: str) -> bytes:
    document = docx.Document()
    document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class Harness:
    def __init__(
        self,
        *,
        llm: FakeLLMClient | None = None,
        cost_ceiling_usd: float = 6.0,
        reserved: int | None = 1,
        exporters: tuple[object, ...] = DEFAULT_EXPORTERS,
    ) -> None:
        self.tenant_id, self.job_id, self.manuscript_id = uuid4(), uuid4(), uuid4()
        self.uow = FakeJobUnitOfWork()
        self.blobs = FakeBlobStore()
        self.llm = llm or FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT)
        self.runner = JobRunner(
            uow=self.uow,
            blobs=self.blobs,  # type: ignore[arg-type]
            llm=self.llm,
            cost_ceiling_usd=cost_ceiling_usd,
            exporters=exporters,  # type: ignore[arg-type]
        )
        self.uow.manuscripts.seed(
            self.tenant_id, self.manuscript_id, blob_key="raw/essay", title="essay.docx"
        )
        self.uow.jobs.seed(self.tenant_id, self.job_id, self.manuscript_id, lease_owner=WORKER)
        self.blobs.seed("raw/essay", _docx_bytes(MANUSCRIPT_TEXT))
        if reserved is not None:  # the upload has already taken the credits
            self.uow.ledger.seed_reservation(self.tenant_id, self.job_id, reserved)

    def claimed(self, attempt: int = 1) -> ClaimedJob:
        return ClaimedJob(
            job_id=self.job_id,
            tenant_id=self.tenant_id,
            manuscript_id=self.manuscript_id,
            attempt=attempt,
            lease_owner=WORKER,
        )

    def steal_lease(self) -> None:
        self.job.lease_owner = "another-worker"  # type: ignore[attr-defined]

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
async def test_happy_path_completes_and_writes_report() -> None:
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
    assert await h.checkpointed() == {
        "language",
        "structure",
        "metadata",
        "integrity",
        "synthesis",
    }
    assert len(h.uow.reports.created) == 1
    assert h.ledger("job") == [-1]
    assert h.ledger("refund") == []
    assert h.job.cost_input_tokens == 4 * 100  # type: ignore[attr-defined]


class _Exporter:
    kind = ArtifactKind.REPORT

    def __init__(self, result: bytes | None, *, delay: float = 0.0) -> None:
        self._result, self._delay = result, delay

    def build(self, ctx: ExportContext) -> bytes | None:
        time.sleep(self._delay)
        return self._result


@pytest.mark.asyncio
async def test_an_exporter_that_does_not_apply_leaves_no_blob_and_no_row() -> None:
    h = Harness(exporters=(_Exporter(None),))

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.COMPLETED
    assert h.uow.reports.created == []
    assert not any(key.endswith("report.docx") for key in h.blobs._data)


@pytest.mark.asyncio
async def test_each_artifact_is_stored_under_a_key_built_from_its_kind() -> None:
    h = Harness(exporters=(_Exporter(b"file"),))

    await h.runner.run(h.claimed())

    assert h.blobs._data[f"jobs/{h.job_id}/report.docx"] == b"file"
    assert [(r["kind"], r["blob_key"]) for r in h.uow.reports.created] == [
        (ArtifactKind.REPORT, f"jobs/{h.job_id}/report.docx")
    ]


@pytest.mark.asyncio
async def test_an_exporter_that_never_finishes_ends_the_job_and_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("editta.services.jobs.EXPORT_TIMEOUT_SECONDS", 0.05)
    h = Harness(exporters=(_Exporter(b"file", delay=0.5),))

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    assert "report export took too long" in (outcome.error or "")
    assert h.uow.reports.created == []
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_runner_never_touches_the_ledger_for_a_successful_job() -> None:
    h = Harness(reserved=None)  # even a job with no reservation: the runner adds none

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.COMPLETED
    assert h.uow.ledger.entries == []


@pytest.mark.asyncio
async def test_a_manuscript_that_never_finishes_parsing_ends_the_job_and_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile file must not hold the worker (and its lease) forever."""

    def never_in_time(data: bytes, title: str) -> None:
        time.sleep(0.5)

    monkeypatch.setattr("editta.services.jobs.parse_manuscript", never_in_time)
    monkeypatch.setattr("editta.services.jobs.PARSE_TIMEOUT_SECONDS", 0.05)
    h = Harness()

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    assert outcome.error == "manuscript took too long to parse"
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_each_step_commits_in_its_own_transaction() -> None:
    h = Harness()

    await h.runner.run(h.claimed())

    # parsing, analyzing, 4 checkpoints, 4 checkpoint loads, synthesizing,
    # exporting, report+completed — never one long transaction around LLM calls.
    assert h.uow.commits >= 10
    assert h.uow.rollbacks == 0


@pytest.mark.asyncio
async def test_resume_runs_only_missing_passes_and_reprimes_the_cache() -> None:
    h = Harness()
    # The two Opus passes are missing: they share a cache, which the resume re-writes.
    for pass_name in ("metadata", "integrity"):
        key = _pass_checkpoint_key(h.job_id, pass_name)
        h.blobs.seed(key, b"[]")
        usage = {"input_tokens": 1, "output_tokens": 1}
        h.uow.pass_results.seed(_PassResultRecord(h.job_id, pass_name, key, "fake-model", usage))

    outcome = await h.runner.run(h.claimed(attempt=2))

    assert outcome.kind is JobOutcomeKind.COMPLETED
    # 2 missing passes + synthesis; the first remaining pass re-writes the cache.
    assert len(h.llm.calls) == 3
    assert h.llm.calls[0][1] is CachePolicy.WRITE
    assert h.llm.calls[1][1] is CachePolicy.READ


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
        async def count_tokens(self, parts: PromptParts, *, model: str) -> int:
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

    outcome = await h.runner.run(h.claimed(attempt=1))

    assert outcome.kind is JobOutcomeKind.RETRY
    assert outcome.retry_after_seconds == retry_delay_seconds(1)
    assert h.job.state is JobState.PENDING  # type: ignore[attr-defined]
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
    await h.runner.run(h.claimed(attempt=1))
    calls_before_retry = len(llm.calls)
    failing["active"] = False

    outcome = await h.runner.run(h.claimed(attempt=2))

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

    outcome = await h.runner.run(h.claimed(attempt=3))

    assert outcome.kind is JobOutcomeKind.DEAD
    assert h.ledger("refund") == [1]


_CUT_OFF = LLMUsage(input_tokens=700, output_tokens=64_000, cache_read_tokens=300)


@pytest.mark.asyncio
async def test_cut_off_answer_is_not_retried_is_refunded_and_its_spend_is_recorded() -> None:
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass("Analyze language", LLMOutputTruncated("cut off", usage=_CUT_OFF)),
    )
    h = Harness(llm=llm)

    outcome = await h.runner.run(h.claimed(attempt=1))

    assert outcome.kind is JobOutcomeKind.DEAD  # first attempt: a retry would be pointless
    assert h.ledger("refund") == [1]
    assert h.job.cost_output_tokens >= 64_000  # type: ignore[attr-defined]
    assert h.job.cache_read_tokens >= 300  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_invalid_answer_is_retried_and_its_spend_is_recorded() -> None:
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass("Analyze language", LLMOutputInvalid("bad", usage=_CUT_OFF)),
    )
    h = Harness(llm=llm)

    outcome = await h.runner.run(h.claimed(attempt=1))

    assert outcome.kind is JobOutcomeKind.RETRY
    assert h.job.cost_output_tokens >= 64_000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_every_failed_call_is_billed_even_though_one_error_propagates() -> None:
    # Language (Opus) and metadata (Haiku) run as parallel groups; both fail.
    def rule(parts: PromptParts) -> BaseException | None:
        if parts.instructions.startswith(("Analyze language", "Analyze metadata")):
            return LLMOutputInvalid("bad", usage=LLMUsage(input_tokens=1, output_tokens=1_000))
        return None

    h = Harness(llm=FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT, fail_when=rule))

    await h.runner.run(h.claimed(attempt=1))

    # Two failed calls at 1 000 tokens each, not just the one error that propagates.
    assert h.job.cost_output_tokens >= 2_000  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_failure_without_usage_records_nothing_extra() -> None:
    llm = FakeLLMClient(
        manuscript_text=MANUSCRIPT_TEXT,
        fail_when=_fail_pass("Analyze language", LLMError("overloaded")),
    )
    h = Harness(llm=llm)

    await h.runner.run(h.claimed(attempt=1))

    assert h.job.cost_output_tokens <= 3 * 50  # type: ignore[attr-defined]  # siblings only


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
    assert h.llm.calls == []
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_missing_manuscript_does_not_mint_a_credit() -> None:
    h = Harness()
    h.uow.manuscripts._records.clear()

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.DEAD
    # The upload's reservation is refunded, so the net is zero.
    assert sum(h.ledger("job") + h.ledger("refund")) == 0


@pytest.mark.asyncio
async def test_attempt_beyond_the_limit_is_dead_lettered_without_running() -> None:
    """Attempts that crashed without recording anything still count (the claim
    increments them), so a crash-looping job ends instead of re-running forever."""
    h = Harness()

    outcome = await h.runner.run(h.claimed(attempt=4))

    assert outcome.kind is JobOutcomeKind.DEAD
    assert "3 attempts" in (outcome.error or "")
    assert h.llm.calls == [] and h.llm.token_counts == 0
    assert h.ledger("refund") == [1]


@pytest.mark.asyncio
async def test_exhausted_job_without_reservation_gets_no_refund() -> None:
    h = Harness(reserved=None)

    outcome = await h.runner.run(h.claimed(attempt=4))

    assert outcome.kind is JobOutcomeKind.DEAD
    assert h.ledger("refund") == []


@pytest.mark.asyncio
async def test_lost_lease_abandons_without_touching_the_new_owners_job() -> None:
    h = Harness()

    def steal_during_structure(parts: PromptParts) -> BaseException | None:
        if parts.instructions.startswith(STRUCTURE_MARKER_PREFIX):
            h.steal_lease()  # e.g. we stalled past lease expiry and were reclaimed
        return None

    h.llm = FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT, fail_when=steal_during_structure)
    h.runner._llm = h.llm

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.ABANDONED
    assert JobState.COMPLETED not in [s for s, _ in h.uow.jobs.state_history]
    assert h.uow.reports.created == []
    assert h.ledger("refund") == []


@pytest.mark.asyncio
async def test_lost_lease_during_failure_handling_neither_kills_nor_refunds() -> None:
    def steal_then_fail(parts: PromptParts) -> BaseException | None:
        h.steal_lease()
        return ParseError("deterministic failure")

    h = Harness(llm=FakeLLMClient(manuscript_text=MANUSCRIPT_TEXT, fail_when=steal_then_fail))

    outcome = await h.runner.run(h.claimed())

    assert outcome.kind is JobOutcomeKind.ABANDONED
    assert JobState.DEAD not in [s for s, _ in h.uow.jobs.state_history]
    assert h.ledger("refund") == []


@pytest.mark.asyncio
async def test_synthesis_is_checkpointed_so_an_export_failure_does_not_rebill_it() -> None:
    class FlakyReportStore(FakeBlobStore):
        fail_report_once = True

        async def put(self, key: str, data: AsyncIterator[bytes]) -> None:
            if key.endswith("report.docx") and self.fail_report_once:
                self.fail_report_once = False
                raise OSError("object store unavailable")
            await super().put(key, data)

    h = Harness()
    h.blobs = FlakyReportStore()
    h.blobs.seed("raw/essay", _docx_bytes(MANUSCRIPT_TEXT))
    h.runner._blobs = h.blobs

    first = await h.runner.run(h.claimed(attempt=1))
    calls_after_first = len(h.llm.calls)
    second = await h.runner.run(h.claimed(attempt=2))

    assert first.kind is JobOutcomeKind.RETRY
    assert second.kind is JobOutcomeKind.COMPLETED
    assert len(h.llm.calls) == calls_after_first  # neither passes nor synthesis re-run


@pytest.mark.asyncio
async def test_refund_returns_the_reserved_amount_not_todays_price() -> None:
    h = Harness(reserved=2)
    h.blobs.seed("raw/essay", b"PK\x03\x04 not a zip")  # dies deterministically

    outcome = await h.runner.run(h.claimed())  # runner configured at 1 credit/job

    assert outcome.kind is JobOutcomeKind.DEAD
    assert h.ledger("refund") == [2]
