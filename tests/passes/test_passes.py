import pytest

from editta.domain.models import Finding, LLMUsage, ManuscriptText, PassResult, Severity
from editta.passes.base import SYSTEM_PREAMBLE
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

    assert "no recommendation" not in parts.system.lower()  # instruction phrased positively
    assert "recommendation" in parts.system.lower()
    assert "unclear opening" in parts.instructions
