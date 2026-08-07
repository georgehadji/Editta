"""Job lifecycle: checkpointed pipeline execution, credit reserve/refund.

Each analysis pass's findings are checkpointed to blob storage + a PassResult row
the moment the pass completes. A retried job (state FAILED -> PENDING, PLAN.md §3)
resumes from whatever passes already have a checkpoint, so completed passes are
never re-run and never re-billed in tokens.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import cast
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.adapters.parsing.docx import parse_docx
from editta.adapters.persistence.repositories import (
    AnalysisJobRepository,
    CreditLedgerRepository,
    ManuscriptRepository,
    PassResultRepository,
    ReportRepository,
)
from editta.domain.budget import check_token_ceiling
from editta.domain.errors import EdittaError
from editta.domain.models import (
    CachePolicy,
    Finding,
    LLMUsage,
    ManuscriptText,
    PassFindings,
    PassResult,
    SynthesisReport,
)
from editta.domain.ports import BlobStore, LLMClient
from editta.domain.quotes import is_quote_anchored
from editta.domain.report import assemble_report
from editta.domain.states import JobState
from editta.passes.base import AnalysisPass
from editta.passes.integrity import IntegrityPass
from editta.passes.language import LanguagePass
from editta.passes.metadata import MetadataPass
from editta.passes.structure import StructurePass
from editta.passes.synthesis import SynthesisPass

ANALYSIS_PASSES = (LanguagePass(), StructurePass(), MetadataPass(), IntegrityPass())


def _pass_checkpoint_key(job_id: UUID, pass_name: str) -> str:
    return f"jobs/{job_id}/passes/{pass_name}.json"


def _report_key(job_id: UUID) -> str:
    return f"jobs/{job_id}/report.docx"


async def _single_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


class JobRunner:
    """One call to `run()` per claimed job. All I/O is injected — no globals."""

    def __init__(
        self,
        *,
        jobs: AnalysisJobRepository,
        manuscripts: ManuscriptRepository,
        pass_results: PassResultRepository,
        reports: ReportRepository,
        ledger: CreditLedgerRepository,
        blobs: BlobStore,
        llm: LLMClient,
        job_cost_credits: int = 1,
    ) -> None:
        self._jobs = jobs
        self._manuscripts = manuscripts
        self._pass_results = pass_results
        self._reports = reports
        self._ledger = ledger
        self._blobs = blobs
        self._llm = llm
        self._job_cost_credits = job_cost_credits

    async def run(self, *, tenant_id: UUID, job_id: UUID, manuscript_id: UUID) -> None:
        await self._reserve_credit_once(tenant_id, job_id)
        try:
            await self._jobs.update_state(tenant_id, job_id, state=JobState.PARSING)
            manuscript = await self._load_manuscript(tenant_id, manuscript_id)

            await self._jobs.update_state(tenant_id, job_id, state=JobState.ANALYZING)
            pass_results = await self._run_missing_passes(job_id, manuscript)

            await self._jobs.update_state(tenant_id, job_id, state=JobState.SYNTHESIZING)
            synthesis = await self._run_synthesis(manuscript, pass_results)

            await self._jobs.update_state(tenant_id, job_id, state=JobState.EXPORTING)
            await self._export_report(job_id, manuscript.source_filename, pass_results, synthesis)

            await self._jobs.update_state(tenant_id, job_id, state=JobState.COMPLETED)
        except EdittaError as exc:
            await self._jobs.update_state(tenant_id, job_id, state=JobState.FAILED, error=str(exc))
            await self._refund_credit(tenant_id, job_id)
            raise

    async def _reserve_credit_once(self, tenant_id: UUID, job_id: UUID) -> None:
        # Already reserved by a prior attempt at this job — retries don't re-charge.
        with suppress(IntegrityError):
            await self._ledger.append(
                tenant_id=tenant_id,
                delta=-self._job_cost_credits,
                reason="job",
                idempotency_key=f"job:{job_id}:reserve",
                job_id=job_id,
            )

    async def _refund_credit(self, tenant_id: UUID, job_id: UUID) -> None:
        with suppress(IntegrityError):
            await self._ledger.append(
                tenant_id=tenant_id,
                delta=self._job_cost_credits,
                reason="refund",
                idempotency_key=f"job:{job_id}:refund",
                job_id=job_id,
            )

    async def _load_manuscript(self, tenant_id: UUID, manuscript_id: UUID) -> ManuscriptText:
        record = await self._manuscripts.get_by_id(tenant_id, manuscript_id)
        if record is None:
            raise EdittaError(f"manuscript {manuscript_id} not found for tenant {tenant_id}")
        data = b"".join([chunk async for chunk in self._blobs.get(record.blob_key)])
        return parse_docx(data, record.title)

    async def _run_missing_passes(
        self, job_id: UUID, manuscript: ManuscriptText
    ) -> list[PassResult]:
        checkpointed = {r.pass_name for r in await self._pass_results.list_for_job(job_id)}
        missing = [p for p in ANALYSIS_PASSES if p.name not in checkpointed]

        if missing:
            preflight_tokens = await self._llm.count_tokens(missing[0].build_prompt(manuscript))
            check_token_ceiling(preflight_tokens)

            # ponytail: cache WRITE only fires when pass 1 itself is missing. On a cold
            # resume (pass 1 already checkpointed, cache long expired) the remaining
            # passes still request READ and just pay full price with no cache benefit —
            # correct, not free. Upgrade: track cache-primed-at and re-WRITE past ~5min.
            first, *rest = missing
            first_cache = CachePolicy.WRITE if first is ANALYSIS_PASSES[0] else CachePolicy.READ
            await self._run_and_checkpoint(job_id, first, manuscript, cache=first_cache)
            if rest:
                await asyncio.gather(
                    *(
                        self._run_and_checkpoint(job_id, p, manuscript, cache=CachePolicy.READ)
                        for p in rest
                    )
                )

        return [await self._load_checkpoint(job_id, p.name) for p in ANALYSIS_PASSES]

    async def _run_and_checkpoint(
        self,
        job_id: UUID,
        analysis_pass: AnalysisPass,
        manuscript: ManuscriptText,
        *,
        cache: CachePolicy,
    ) -> None:
        prompt = analysis_pass.build_prompt(manuscript)
        result = await self._llm.analyze(
            prompt, analysis_pass.output_schema, cache=cache, effort=analysis_pass.effort
        )
        findings = cast(PassFindings, result.output).findings
        anchored = [f for f in findings if is_quote_anchored(f.quote, manuscript.text)]

        blob_key = _pass_checkpoint_key(job_id, analysis_pass.name)
        payload = json.dumps([f.model_dump(mode="json") for f in anchored]).encode("utf-8")
        await self._blobs.put(blob_key, _single_chunk(payload))
        await self._pass_results.upsert(
            job_id=job_id,
            pass_name=analysis_pass.name,
            blob_key=blob_key,
            model=result.model,
            usage=result.usage.model_dump(mode="json"),
        )

    async def _load_checkpoint(self, job_id: UUID, pass_name: str) -> PassResult:
        records = await self._pass_results.list_for_job(job_id)
        record = next(r for r in records if r.pass_name == pass_name)
        raw = b"".join([chunk async for chunk in self._blobs.get(record.blob_key)])
        findings = [Finding.model_validate(f) for f in json.loads(raw)]
        return PassResult(
            pass_name=pass_name, findings=findings, usage=LLMUsage.model_validate(record.usage)
        )

    async def _run_synthesis(
        self, manuscript: ManuscriptText, pass_results: list[PassResult]
    ) -> SynthesisReport:
        synthesis_pass = SynthesisPass()
        prompt = synthesis_pass.build_prompt(manuscript, pass_results)
        result = await self._llm.analyze(
            prompt,
            synthesis_pass.output_schema,
            cache=CachePolicy.READ,
            effort=synthesis_pass.effort,
        )
        return result.output

    async def _export_report(
        self,
        job_id: UUID,
        filename: str,
        pass_results: list[PassResult],
        synthesis: SynthesisReport,
    ) -> None:
        report = assemble_report(
            manuscript_filename=filename, pass_results=pass_results, synthesis=synthesis
        )
        docx_bytes = ReportDocxBuilder(report).build()
        blob_key = _report_key(job_id)
        await self._blobs.put(blob_key, _single_chunk(docx_bytes))
        await self._reports.create(job_id=job_id, blob_key=blob_key)
