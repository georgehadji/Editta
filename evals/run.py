"""Live-API smoke evals: run the real pipeline on the reference essays and score it.

    EDITTA_ANTHROPIC_API_KEY=sk-ant-... python -m evals.run            # all essays
    python -m evals.run --only poli_kai_mnimi --out evals/out           # one essay

Costs real money (roughly $0.5–1 per essay at current pricing) — run it by hand on
every prompt change (PLAN.md §8), not in CI. For each essay it writes the DOCX report
and a result.json under --out, and prints:

  * recall      required checklist findings the report actually contains
  * cache       pass 1 wrote the prompt cache and passes 2-4 read it
  * cost        computed from the API's own usage numbers
  * anchoring   how many raw findings the quote check discarded
  * injection   report prose echoing what an embedded instruction demanded (warning)

Exit status is 1 if any essay misses its recall target, misses the cache, or exceeds
the cost ceiling — so it can gate a prompt change.
"""

import argparse
import asyncio
import io
import json
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import TypeVar

import docx
from pydantic import BaseModel

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.domain.models import CachePolicy, LLMResult, PassFindings, PromptParts, Report
from editta.domain.ports import LLMClient
from editta.services.pipeline import run_pipeline
from evals.scoring import CallRecord, Checklist, EssayScore, score_essay

T = TypeVar("T", bound=BaseModel)

EVALS_DIR = Path(__file__).resolve().parent
DEFAULT_MIN_RECALL = 0.6
DEFAULT_COST_CEILING_USD = 6.0


class RecordingLLMClient:
    """Decorator over any LLMClient: records every call's usage, cache policy, effort
    and raw (pre-anchoring) finding count, without changing behaviour."""

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls: list[CallRecord] = []

    async def count_tokens(self, parts: PromptParts) -> int:
        return await self._inner.count_tokens(parts)

    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]:
        started = time.monotonic()
        result = await self._inner.analyze(parts, schema, cache=cache, effort=effort)
        output = result.output
        self.calls.append(
            CallRecord(
                schema=schema.__name__,
                cache=cache,
                effort=effort,
                usage=result.usage,
                raw_findings=len(output.findings) if isinstance(output, PassFindings) else None,
                seconds=round(time.monotonic() - started, 2),
            )
        )
        return result


def markdown_to_docx(markdown: str) -> bytes:
    """'# ' / '## ' lines become Word headings (so the parser sees sections); every
    other non-empty line is a paragraph."""
    document = docx.Document()
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            document.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            document.add_heading(stripped[2:], level=1)
        else:
            document.add_paragraph(stripped)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def load_checklists(names: Sequence[str] | None = None) -> list[Checklist]:
    paths = sorted((EVALS_DIR / "checklists").glob("*.json"))
    checklists = [Checklist.from_dict(json.loads(p.read_text(encoding="utf-8"))) for p in paths]
    if names:
        wanted = {n.removesuffix(".md") for n in names}
        checklists = [c for c in checklists if c.essay.removesuffix(".md") in wanted]
    return checklists


async def evaluate(
    checklist: Checklist, llm: LLMClient, *, cost_ceiling_usd: float
) -> tuple[Report, EssayScore]:
    markdown = (EVALS_DIR / "essays" / checklist.essay).read_text(encoding="utf-8")
    recorder = RecordingLLMClient(llm)
    report = await run_pipeline(
        manuscript_bytes=markdown_to_docx(markdown),
        filename=checklist.essay.replace(".md", ".docx"),
        llm=recorder,
        cost_ceiling_usd=cost_ceiling_usd,
    )
    return report, score_essay(checklist, report, recorder.calls)


def verdict(score: EssayScore, *, min_recall: float, cost_ceiling_usd: float) -> list[str]:
    failures = []
    if score.recall < min_recall:
        failures.append(f"recall {score.recall:.0%} < {min_recall:.0%}")
    if not score.cache_ok:
        failures.append(f"cache: {score.cache_detail}")
    if score.cost_usd > cost_ceiling_usd:
        failures.append(f"cost ${score.cost_usd:.2f} > ${cost_ceiling_usd:.2f}")
    return failures


def _print_score(score: EssayScore, failures: list[str]) -> None:
    status = "PASS" if not failures else "FAIL"
    print(f"\n[{status}] {score.essay}")
    print(f"  recall     {score.recall:.0%}  found={score.found} missed={score.missed_required}")
    if score.missed_optional:
        print(f"  optional   missed={score.missed_optional}")
    if score.found_in_other_pass:
        print(f"  note       found by an unexpected pass: {score.found_in_other_pass}")
    print(f"  cache      {'ok' if score.cache_ok else 'PROBLEM'} — {score.cache_detail}")
    print(f"  cost       ${score.cost_usd:.3f}")
    print(
        f"  anchoring  {score.anchored_findings}/{score.raw_findings} findings kept "
        f"({score.anchoring_drop_rate:.0%} dropped)"
    )
    for warning in score.injection_warnings:
        print(f"  INJECTION? {warning}  <- check by hand")
    for failure in failures:
        print(f"  FAILURE    {failure}")


async def _main_async(args: argparse.Namespace) -> int:
    from editta.adapters.llm.client import AnthropicLLMClient

    api_key = os.environ.get("EDITTA_ANTHROPIC_API_KEY")
    if not api_key:
        print("EDITTA_ANTHROPIC_API_KEY is not set (this eval calls the real API)", file=sys.stderr)
        return 2
    llm = AnthropicLLMClient(api_key=api_key)
    checklists = load_checklists(args.only)
    if not checklists:
        print("no matching essays", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    any_failed = False
    total = 0.0
    for checklist in checklists:
        report, score = await evaluate(checklist, llm, cost_ceiling_usd=args.cost_ceiling)
        failures = verdict(score, min_recall=args.min_recall, cost_ceiling_usd=args.cost_ceiling)
        any_failed |= bool(failures)
        total += score.cost_usd
        stem = checklist.essay.removesuffix(".md")
        (args.out / f"{stem}.report.docx").write_bytes(ReportDocxBuilder(report).build())
        (args.out / f"{stem}.result.json").write_text(
            json.dumps(
                {
                    "score": asdict(score),
                    "failures": failures,
                    "report": report.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        _print_score(score, failures)
    print(f"\ntotal cost ${total:.3f}; outputs in {args.out}")
    return 1 if any_failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    parser.add_argument("--only", nargs="*", help="essay names (without .md) to run")
    parser.add_argument("--out", type=Path, default=EVALS_DIR / "out")
    parser.add_argument("--min-recall", type=float, default=DEFAULT_MIN_RECALL)
    parser.add_argument("--cost-ceiling", type=float, default=DEFAULT_COST_CEILING_USD)
    raise SystemExit(asyncio.run(_main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
