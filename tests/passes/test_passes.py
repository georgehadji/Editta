import pytest

from editta.domain.models import (
    Finding,
    LLMUsage,
    ManuscriptText,
    PassFindings,
    PassResult,
    Severity,
    SynthesisPlan,
)
from editta.passes.base import SHARED_EFFORT, SYSTEM_PREAMBLE
from editta.passes.integrity import IntegrityPass
from editta.passes.language import LanguagePass
from editta.passes.metadata import MetadataPass
from editta.passes.structure import StructurePass
from editta.passes.synthesis import SynthesisPass

ALL_PASSES = [LanguagePass(), StructurePass(), MetadataPass(), IntegrityPass()]
MANUSCRIPT = ManuscriptText(source_filename="essay.docx", text="Some manuscript body text.")


@pytest.mark.parametrize("analysis_pass", ALL_PASSES, ids=lambda p: p.name)
def test_build_prompt_isolates_manuscript_as_untrusted_data(analysis_pass: object) -> None:
    parts = analysis_pass.build_prompt(MANUSCRIPT)  # type: ignore[attr-defined]

    assert parts.system == SYSTEM_PREAMBLE
    assert "not instructions to follow" in parts.system
    assert parts.manuscript_block == MANUSCRIPT.text
    assert parts.instructions


def test_pass_names_are_unique() -> None:
    names = {p.name for p in ALL_PASSES}
    assert len(names) == len(ALL_PASSES)


def test_synthesis_prompt_carries_pass_results_not_new_instructions_from_manuscript() -> None:
    finding = Finding(
        section="intro",
        quote="Some manuscript",
        issue="unclear opening",
        severity=Severity.MINOR,
        suggestion="tighten the first sentence",
    )
    pass_result = PassResult(
        pass_name="language",  # noqa: S106 -- trips the hardcoded-password heuristic
        findings=[finding],
        usage=LLMUsage(input_tokens=10, output_tokens=5),
    )

    parts = SynthesisPass().build_prompt(MANUSCRIPT, [pass_result])

    # The safety rail and the pass results live after the cached prefix, not in it.
    assert "recommendation" in parts.instructions.lower()
    assert "unclear opening" in parts.instructions
    assert parts.instructions.index("recommendation") < parts.instructions.index("unclear opening")


def test_the_four_passes_share_one_cacheable_prefix() -> None:
    """Prompt-cache invariant (PLAN.md §4): system prompt, manuscript block, effort and
    output schema must be identical across the four passes. Any difference means
    passes 2-4 re-pay the full manuscript instead of reading it from the cache — per
    the Anthropic docs an effort or output-format change alone invalidates it."""
    prompts = [p.build_prompt(MANUSCRIPT) for p in ALL_PASSES]

    assert {parts.system for parts in prompts} == {SYSTEM_PREAMBLE}
    assert {parts.manuscript_block for parts in prompts} == {MANUSCRIPT.text}
    assert {p.effort for p in ALL_PASSES} == {SHARED_EFFORT}
    assert {p.output_schema for p in ALL_PASSES} == {PassFindings}


def test_synthesis_prompt_lists_findings_with_citable_ids() -> None:
    finding = Finding(
        section="intro", quote="q", issue="i", severity=Severity.MINOR, suggestion="s"
    )
    result = PassResult(
        pass_name="language",  # noqa: S106
        findings=[finding],
        usage=LLMUsage(input_tokens=1, output_tokens=1),
    )

    parts = SynthesisPass().build_prompt(MANUSCRIPT, [result])

    assert '"id": "language-0"' in parts.instructions
    assert SynthesisPass.output_schema is SynthesisPlan
