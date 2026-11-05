"""Language & style analysis pass."""

from editta.domain.models import PassFindings
from editta.passes.base import BaseAnalysisPass


class LanguagePass(BaseAnalysisPass):
    name = "language"
    output_schema = PassFindings

    def instructions(self) -> str:
        return (
            "Analyze language and style: clarity, register consistency, sentence-level "
            "flow, word choice, and grammar. For each issue worth an editor's attention, "
            "return a finding with the exact quoted passage, the issue, a severity "
            "(minor/moderate/major), and a concrete suggestion."
        )
