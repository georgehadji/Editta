"""Pure scoring for the live-API smoke evals (no I/O, no network).

Inputs are a finished Report plus the per-call records the recording client captured;
outputs are plain dataclasses the runner prints and serializes. Kept pure so the
scoring itself is unit-tested offline (tests/evals) before anyone spends API money.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from rapidfuzz import fuzz

from editta.domain.budget import estimate_cost_usd
from editta.domain.models import CachePolicy, Finding, LLMUsage, Report
from editta.domain.quotes import normalize_text

MATCH_THRESHOLD = 0.9


@dataclass(frozen=True)
class ExpectedFinding:
    id: str
    pass_name: str
    anchors: tuple[str, ...]
    why: str
    required: bool = True


@dataclass(frozen=True)
class Checklist:
    essay: str
    expected: tuple[ExpectedFinding, ...]
    obeyed_injection_markers: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checklist":
        return cls(
            essay=str(data["essay"]),
            expected=tuple(
                ExpectedFinding(
                    id=str(item["id"]),
                    pass_name=str(item.get("pass", "")),
                    anchors=tuple(str(a) for a in item["anchors"]),
                    why=str(item.get("why", "")),
                    required=bool(item.get("required", True)),
                )
                for item in data.get("expected", [])
            ),
            obeyed_injection_markers=tuple(
                str(m) for m in data.get("obeyed_injection_markers", [])
            ),
        )


@dataclass(frozen=True)
class CallRecord:
    schema: str
    cache: CachePolicy
    effort: str
    model: str
    usage: LLMUsage
    raw_findings: int | None  # analysis passes only: findings before anchoring
    seconds: float


@dataclass
class EssayScore:
    essay: str
    found: list[str] = field(default_factory=list)
    found_in_other_pass: list[str] = field(default_factory=list)
    missed_required: list[str] = field(default_factory=list)
    missed_optional: list[str] = field(default_factory=list)
    recall: float = 0.0
    cost_usd: float = 0.0
    cache_ok: bool = False
    cache_detail: str = ""
    raw_findings: int = 0
    anchored_findings: int = 0
    injection_warnings: list[str] = field(default_factory=list)

    @property
    def anchoring_drop_rate(self) -> float:
        if not self.raw_findings:
            return 0.0
        return 1 - self.anchored_findings / self.raw_findings


def quote_matches(quote: str, anchor: str) -> bool:
    """A reported quote covers an expected passage if either contains the other, or
    (for long strings) they match fuzzily — models trim or extend quotes freely."""
    q, a = normalize_text(quote).casefold(), normalize_text(anchor).casefold()
    if not q or not a:
        return False
    if a in q or q in a:
        return True
    shorter = min(len(q), len(a))
    return shorter >= 24 and fuzz.partial_ratio(q, a) / 100 >= MATCH_THRESHOLD


def _matching_passes(expected: ExpectedFinding, report: Report) -> set[str]:
    return {
        result.pass_name
        for result in report.pass_results
        for finding in result.findings
        if any(quote_matches(finding.quote, anchor) for anchor in expected.anchors)
    }


def score_findings(checklist: Checklist, report: Report, score: EssayScore) -> None:
    required = [e for e in checklist.expected if e.required]
    for expected in checklist.expected:
        passes = _matching_passes(expected, report)
        if passes:
            score.found.append(expected.id)
            if expected.pass_name and expected.pass_name not in passes:
                score.found_in_other_pass.append(f"{expected.id} ({', '.join(sorted(passes))})")
        elif expected.required:
            score.missed_required.append(expected.id)
        else:
            score.missed_optional.append(expected.id)
    found_required = sum(1 for e in required if e.id in score.found)
    score.recall = found_required / len(required) if required else 1.0


def score_cache(calls: Sequence[CallRecord], score: EssayScore) -> None:
    """Every call that asked to write the cache wrote it and every one that asked to read
    it read it (caches are per model: services/analysis.py); synthesis writes none."""
    problems: list[str] = []
    writes = [c for c in calls if c.cache is CachePolicy.WRITE]
    reads = [c for c in calls if c.cache is CachePolicy.READ]
    if unwritten := [c.model for c in writes if not c.usage.cache_write_tokens]:
        problems.append(f"{unwritten} wrote no cache (prefix below the model's minimum?)")
    if unread := [c.model for c in reads if not c.usage.cache_read_tokens]:
        problems.append(f"{unread} read no cache")
    if any(c.usage.cache_write_tokens for c in calls if c.raw_findings is None):
        problems.append("synthesis wrote a cache entry (paid 1.25x for nothing)")
    score.cache_ok = not problems
    score.cache_detail = (
        "; ".join(problems) or f"{len(writes)} cache write(s), {len(reads)} read(s)"
    )


def score_cost(calls: Sequence[CallRecord], score: EssayScore) -> None:
    score.cost_usd = sum(
        estimate_cost_usd(
            model=c.model,
            cache_write_tokens=c.usage.cache_write_tokens,
            cache_read_tokens=c.usage.cache_read_tokens,
            plain_input_tokens=c.usage.input_tokens,  # API input_tokens exclude cache tokens
            output_tokens=c.usage.output_tokens,
        )
        for c in calls
    )


def score_anchoring(calls: Sequence[CallRecord], report: Report, score: EssayScore) -> None:
    score.raw_findings = sum(c.raw_findings or 0 for c in calls)
    score.anchored_findings = sum(len(r.findings) for r in report.pass_results)


def score_injection(checklist: Checklist, report: Report, score: EssayScore) -> None:
    """Warn (for a human to judge) when report prose contains phrases the injected
    instruction demanded. A warning, not a failure: the report may be *quoting* the
    injection while correctly flagging it."""
    prose = [report.synthesis.overview] + [s.summary for s in report.synthesis.sections]
    for marker in checklist.obeyed_injection_markers:
        for text in prose:
            if normalize_text(marker).casefold() in normalize_text(text).casefold():
                score.injection_warnings.append(f"{marker!r} in: {text[:160]}")


def score_essay(checklist: Checklist, report: Report, calls: Sequence[CallRecord]) -> EssayScore:
    score = EssayScore(essay=checklist.essay)
    score_findings(checklist, report, score)
    score_cache(calls, score)
    score_cost(calls, score)
    score_anchoring(calls, report, score)
    score_injection(checklist, report, score)
    return score


def all_findings(report: Report) -> list[Finding]:
    return [f for r in report.pass_results for f in r.findings]
