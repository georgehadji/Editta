"""Job lifecycle: checkpointed pipeline execution, credit reserve/refund, retry policy.

Transaction design (the reason this module looks the way it does)
-----------------------------------------------------------------
Each step commits in its *own* short Unit of Work, and no LLM call ever runs inside an
open transaction. Running a whole job in one transaction (the previous design) meant:
  * a failure rolled back the FAILED state, the refund *and* every pass checkpoint,
    leaving the job claimable with nothing recorded — re-run and re-billed, forever;
  * the UI saw no progress, and each job pinned a pooled DB connection for minutes.

Now, per step: transition state -> commit; checkpoint each pass the moment it
finishes -> commit; on failure, record the policy's decision (retry with backoff, or
DEAD + refund) atomically -> commit. A retried job resumes from its checkpoints.

Credits: reserved once per job (idempotency key), refunded only when the job dies.
A retry reuses the original reservation; a completed job keeps it.
"""

import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID

import structlog

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.adapters.parsing import parse_manuscript
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
)
from editta.domain.errors import EdittaError
from editta.domain.models import (
    ClaimedJob,
    Finding,
    JobOutcome,
    LLMUsage,
    ManuscriptText,
    PassResult,
    SynthesisReport,
)
from editta.domain.ports import BlobStore, LLMClient
from editta.domain.report import assemble_report
from editta.domain.states import JobState, decide_after_failure
from editta.services.analysis import ANALYSIS_PASSES, preflight, run_passes, run_synthesis

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class JobRepositories:
    """Repositories bound to one transaction."""

    jobs: AnalysisJobRepository
    manuscripts: ManuscriptRepository
    pass_results: PassResultRepository
    reports: ReportRepository
    ledger: CreditLedgerRepository


# Opens one tenant-scoped transaction; commits on clean exit, rolls back on error.
JobUnitOfWork = Callable[[], AbstractAsyncContextManager[JobRepositories]]


def _pass_checkpoint_key(job_id: UUID, pass_name: str) -> str:
    return f"jobs/{job_id}/passes/{pass_name}.json"


def _report_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/report.docx"


def _error_message(exc: BaseException) -> str:
    # Domain errors carry deliberate, content-free messages. Anything else is a bug:
    # record only the type, since arbitrary exception text could quote the manuscript
    # (PLAN.md §5: no manuscript text in errors).
    if isinstance(exc, EdittaError):
        return str(exc)
    return f"internal error ({type(exc).__name__})"


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


class JobRunner:
    """One `run()` per claimed job. All I/O is injected — no globals."""

    def __init__(
        self,
        *,
        uow: JobUnitOfWork,
        blobs: BlobStore,
        llm: LLMClient,
        cost_ceiling_usd: float,
        job_cost_credits: int = 1,
    ) -> None:
        self._uow = uow
        self._blobs = blobs
        self._llm = llm
        self._cost_ceiling_usd = cost_ceiling_usd
        self._job_cost_credits = job_cost_credits

    async def run(self, job: ClaimedJob) -> JobOutcome:
        try:
            await self._execute(job)
        except Exception as exc:  # classified, recorded and returned — never swallowed
            return await self._handle_failure(job, exc)
        return JobOutcome.completed()

    # --- happy path -------------------------------------------------------------

    async def _execute(self, job: ClaimedJob) -> None:
        # Own transaction: the reservation must survive a failure in any later step,
        # because the failure path refunds against it.
        async with self._uow() as repos:
            await self._reserve_credit_once(repos, job)

        async with self._uow() as repos:
            await repos.jobs.update_state(job.tenant_id, job.job_id, state=JobState.PARSING)
            record = await repos.manuscripts.get_by_id(job.tenant_id, job.manuscript_id)
            if record is None:
                raise EdittaError(f"manuscript {job.manuscript_id} not found")
            blob_key, title = record.blob_key, record.title

        data = b"".join([chunk async for chunk in self._blobs.get(blob_key)])
        manuscript = parse_manuscript(data, title)

        async with self._uow() as repos:
            await repos.jobs.update_state(job.tenant_id, job.job_id, state=JobState.ANALYZING)
            done = {r.pass_name for r in await repos.pass_results.list_for_job(job.job_id)}

        missing = [p for p in ANALYSIS_PASSES if p.name not in done]
        await preflight(
            self._llm,
            manuscript,
            analysis_passes_remaining=len(missing),
            cost_ceiling_usd=self._cost_ceiling_usd,
        )

        async def checkpoint(result: PassResult) -> None:
            await self._checkpoint(job, result)

        await run_passes(missing, manuscript, self._llm, on_pass_completed=checkpoint)
        pass_results = [await self._load_checkpoint(job.job_id, p.name) for p in ANALYSIS_PASSES]

        async with self._uow() as repos:
            await repos.jobs.update_state(job.tenant_id, job.job_id, state=JobState.SYNTHESIZING)
        synthesis = await run_synthesis(manuscript, pass_results, self._llm)

        async with self._uow() as repos:
            await repos.jobs.update_state(job.tenant_id, job.job_id, state=JobState.EXPORTING)
        report_key = await self._export_report(job.job_id, manuscript, pass_results, synthesis)

        # Report row + COMPLETED commit together: a report never exists for a job that
        # isn't complete, and a complete job always has its report.
        async with self._uow() as repos:
            await repos.reports.create(job_id=job.job_id, blob_key=report_key)
            await repos.jobs.update_state(job.tenant_id, job.job_id, state=JobState.COMPLETED)

    async def _reserve_credit_once(self, repos: JobRepositories, job: ClaimedJob) -> None:
        # A retry finds the first attempt's reservation and appends nothing.
        await repos.ledger.append_once(
            tenant_id=job.tenant_id,
            delta=-self._job_cost_credits,
            reason="job",
            idempotency_key=f"job:{job.job_id}:reserve",
            job_id=job.job_id,
        )

    async def _checkpoint(self, job: ClaimedJob, result: PassResult) -> None:
        blob_key = _pass_checkpoint_key(job.job_id, result.pass_name)
        payload = json.dumps(
            [f.model_dump(mode="json") for f in result.findings], ensure_ascii=False
        ).encode("utf-8")
        # Blob first, then the row that points at it: a crash in between leaves an
        # orphan blob (harmless, overwritten on retry), never a row without its blob.
        await self._blobs.put(blob_key, _single_chunk(payload))
        async with self._uow() as repos:
            await repos.pass_results.upsert(
                job_id=job.job_id,
                pass_name=result.pass_name,
                blob_key=blob_key,
                model=result.model,
                usage=result.usage.model_dump(mode="json"),
            )
            await repos.jobs.record_usage(
                job.tenant_id,
                job.job_id,
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cache_read_tokens=result.usage.cache_read_tokens,
            )

    async def _load_checkpoint(self, job_id: UUID, pass_name: str) -> PassResult:
        async with self._uow() as repos:
            records = await repos.pass_results.list_for_job(job_id)
            record = next(r for r in records if r.pass_name == pass_name)
            blob_key, usage, model = record.blob_key, record.usage, record.model
        raw = b"".join([chunk async for chunk in self._blobs.get(blob_key)])
        findings = [Finding.model_validate(f) for f in json.loads(raw)]
        return PassResult(
            pass_name=pass_name,
            findings=findings,
            usage=LLMUsage.model_validate(usage),
            model=model,
        )

    async def _export_report(
        self,
        job_id: UUID,
        manuscript: ManuscriptText,
        pass_results: list[PassResult],
        synthesis: SynthesisReport,
    ) -> str:
        report = assemble_report(
            manuscript_filename=manuscript.source_filename,
            pass_results=pass_results,
            synthesis=synthesis,
        )
        blob_key = _report_key(job_id)
        await self._blobs.put(blob_key, _single_chunk(ReportDocxBuilder(report).build()))
        return blob_key

    # --- failure path -----------------------------------------------------------

    async def _handle_failure(self, job: ClaimedJob, exc: Exception) -> JobOutcome:
        retryable = exc.retryable if isinstance(exc, EdittaError) else True
        decision = decide_after_failure(previous_attempts=job.attempt, retryable=retryable)
        error = _error_message(exc)
        log = logger.bind(job_id=str(job.job_id), tenant_id=str(job.tenant_id))
        if isinstance(exc, EdittaError):
            log.warning("job.attempt_failed", error=error, attempts=decision.attempts_made)
        else:
            log.exception("job.attempt_crashed", attempts=decision.attempts_made)

        # The decision, the error and (if terminal) the refund commit together.
        async with self._uow() as repos:
            await repos.jobs.record_failure(
                job.tenant_id,
                job.job_id,
                state=decision.next_state,
                error=error,
                attempts_made=decision.attempts_made,
            )
            # Refund only what was actually reserved — a job that died before its
            # reservation committed must not mint a credit.
            if decision.is_terminal and await repos.ledger.has_entry(f"job:{job.job_id}:reserve"):
                await repos.ledger.append_once(
                    tenant_id=job.tenant_id,
                    delta=self._job_cost_credits,
                    reason="refund",
                    idempotency_key=f"job:{job.job_id}:refund",
                    job_id=job.job_id,
                )

        if decision.retry_after_seconds is None:
            return JobOutcome.dead(error=error)
        return JobOutcome.retry(error=error, after_seconds=decision.retry_after_seconds)
