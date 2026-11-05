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

Credits: reserved at upload (services/uploads.py), refunded only when the job dies.
The runner's own reservation is an idempotent fallback for jobs queued before upload-
time reservation existed; for every other job it finds the key and appends nothing.

Fencing: every job-state write is conditional on this run's `lease_owner` still
holding the job (`set_state_if_owned`). If the lease was lost — the worker stalled
past expiry and another worker claimed the job — the write raises LeaseLost, the
step's transaction rolls back, and this run abandons the job without recording or
refunding anything; the new owner's outcome is the one that counts.
"""

import asyncio
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
from editta.domain.credits import refund_key, reservation_key
from editta.domain.errors import EdittaError, LeaseLost
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
from editta.domain.states import JobState, attempts_exhausted, decide_after_failure
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


SYNTHESIS_CHECKPOINT = "synthesis"


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
        if attempts_exhausted(job.attempt):
            # Earlier attempts ended without recording anything (crash, OOM, lost
            # lease) — the claim counted them anyway. Stop instead of looping forever.
            return await self._dead_letter(
                job, f"gave up after {job.attempt - 1} attempts that did not finish"
            )
        try:
            await self._execute(job)
        except LeaseLost as exc:
            return self._abandon(job, exc)
        except Exception as exc:  # classified, recorded and returned — never swallowed
            try:
                return await self._handle_failure(job, exc)
            except LeaseLost as lost:
                return self._abandon(job, lost)
        return JobOutcome.completed()

    # --- happy path -------------------------------------------------------------

    async def _set_state(self, repos: JobRepositories, job: ClaimedJob, state: JobState) -> None:
        await repos.jobs.set_state_if_owned(
            job.tenant_id, job.job_id, lease_owner=job.lease_owner, state=state
        )

    async def _execute(self, job: ClaimedJob) -> None:
        # Own transaction: the reservation must survive a failure in any later step,
        # because the failure path refunds against it.
        async with self._uow() as repos:
            await self._reserve_credit_once(repos, job)

        async with self._uow() as repos:
            await self._set_state(repos, job, JobState.PARSING)
            record = await repos.manuscripts.get_by_id(job.tenant_id, job.manuscript_id)
            if record is None:
                raise EdittaError(f"manuscript {job.manuscript_id} not found")
            if not record.blob_key:  # purged by retention (services/retention.py)
                raise EdittaError("manuscript content was deleted by the retention policy")
            blob_key, title = record.blob_key, record.title

        data = b"".join([chunk async for chunk in self._blobs.get(blob_key)])
        # CPU-bound and attacker-influenced (a crafted PDF can take minutes): off the
        # event loop, so heartbeats keep the lease alive while it runs.
        manuscript = await asyncio.to_thread(parse_manuscript, data, title)

        async with self._uow() as repos:
            await self._set_state(repos, job, JobState.ANALYZING)
            done = {r.pass_name for r in await repos.pass_results.list_for_job(job.job_id)}

        missing = [p for p in ANALYSIS_PASSES if p.name not in done]
        synthesis_done = SYNTHESIS_CHECKPOINT in done
        if missing or not synthesis_done:
            await preflight(
                self._llm,
                manuscript,
                analysis_passes=missing,
                include_synthesis=not synthesis_done,
                cost_ceiling_usd=self._cost_ceiling_usd,
            )

        async def checkpoint(result: PassResult) -> None:
            await self._checkpoint(job, result)

        await run_passes(missing, manuscript, self._llm, on_pass_completed=checkpoint)
        pass_results = [await self._load_checkpoint(job.job_id, p.name) for p in ANALYSIS_PASSES]

        async with self._uow() as repos:
            await self._set_state(repos, job, JobState.SYNTHESIZING)
        if synthesis_done:
            synthesis = await self._load_synthesis(job.job_id)
        else:
            synthesis = await run_synthesis(manuscript, pass_results, self._llm)
            await self._checkpoint_synthesis(job, synthesis)

        async with self._uow() as repos:
            await self._set_state(repos, job, JobState.EXPORTING)
        report_key = await self._export_report(job.job_id, manuscript, pass_results, synthesis)

        # Report row + COMPLETED commit together: a report never exists for a job that
        # isn't complete, and a complete job always has its report. Fenced: if the lease
        # was lost, the transaction rolls back and the report row is not created.
        async with self._uow() as repos:
            await repos.reports.create(job_id=job.job_id, blob_key=report_key)
            await self._set_state(repos, job, JobState.COMPLETED)

    async def _reserve_credit_once(self, repos: JobRepositories, job: ClaimedJob) -> None:
        # A retry finds the first attempt's reservation and appends nothing.
        await repos.ledger.append_once(
            tenant_id=job.tenant_id,
            delta=-self._job_cost_credits,
            reason="job",
            idempotency_key=reservation_key(job.job_id),
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

    async def _checkpoint_synthesis(self, job: ClaimedJob, synthesis: SynthesisReport) -> None:
        """Synthesis is the most expensive call (uncached manuscript); checkpointing it
        means an export or commit failure never pays for it twice."""
        blob_key = _pass_checkpoint_key(job.job_id, SYNTHESIS_CHECKPOINT)
        payload = synthesis.model_dump_json().encode("utf-8")
        await self._blobs.put(blob_key, _single_chunk(payload))
        async with self._uow() as repos:
            await repos.pass_results.upsert(
                job_id=job.job_id,
                pass_name=SYNTHESIS_CHECKPOINT,
                blob_key=blob_key,
                model="",
                usage={},
            )

    async def _load_synthesis(self, job_id: UUID) -> SynthesisReport:
        blob_key = _pass_checkpoint_key(job_id, SYNTHESIS_CHECKPOINT)
        raw = b"".join([chunk async for chunk in self._blobs.get(blob_key)])
        return SynthesisReport.model_validate_json(raw)

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
        docx_bytes = await asyncio.to_thread(ReportDocxBuilder(report).build)
        await self._blobs.put(blob_key, _single_chunk(docx_bytes))
        return blob_key

    # --- failure path -----------------------------------------------------------

    async def _handle_failure(self, job: ClaimedJob, exc: Exception) -> JobOutcome:
        retryable = exc.retryable if isinstance(exc, EdittaError) else True
        decision = decide_after_failure(attempts_made=job.attempt, retryable=retryable)
        error = _error_message(exc)
        log = logger.bind(job_id=str(job.job_id), tenant_id=str(job.tenant_id))
        if isinstance(exc, EdittaError):
            log.warning("job.attempt_failed", error=error, attempt=job.attempt)
        else:
            log.exception("job.attempt_crashed", attempt=job.attempt)

        if decision.retry_after_seconds is None:  # terminal: retrying cannot help
            return await self._dead_letter(job, error)
        async with self._uow() as repos:
            await repos.jobs.set_state_if_owned(
                job.tenant_id,
                job.job_id,
                lease_owner=job.lease_owner,
                state=decision.next_state,
                error=error,
            )
        return JobOutcome.retry(error=error, after_seconds=decision.retry_after_seconds)

    async def _dead_letter(self, job: ClaimedJob, error: str) -> JobOutcome:
        # DEAD and the refund commit together, fenced: a worker that lost the job can
        # neither kill it nor refund it.
        async with self._uow() as repos:
            await repos.jobs.set_state_if_owned(
                job.tenant_id,
                job.job_id,
                lease_owner=job.lease_owner,
                state=JobState.DEAD,
                error=error,
            )
            # Refund exactly what was reserved: nothing if the job died before its
            # reservation committed (no minted credit), and the reserved amount — not
            # today's price — if the price changed since the upload.
            reserved = await repos.ledger.entry_delta(reservation_key(job.job_id))
            if reserved is not None and reserved < 0:
                await repos.ledger.append_once(
                    tenant_id=job.tenant_id,
                    delta=-reserved,
                    reason="refund",
                    idempotency_key=refund_key(job.job_id),
                    job_id=job.job_id,
                )
        return JobOutcome.dead(error=error)

    def _abandon(self, job: ClaimedJob, exc: LeaseLost) -> JobOutcome:
        logger.warning("job.lease_lost", job_id=str(job.job_id), worker=job.lease_owner)
        return JobOutcome.abandoned(error=str(exc))
