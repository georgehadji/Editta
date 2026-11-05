"""Structure & argumentation analysis pass."""

from editta.domain.models import PassFindings
from editta.passes.base import BaseAnalysisPass


class StructurePass(BaseAnalysisPass):
    name = "structure"
    output_schema = PassFindings

    def instructions(self) -> str:
        return (
            "Analyze structure and argumentation: logical flow between sections, "
            "argument coherence, unsupported transitions, and pacing. Return findings "
            "with exact quoted passages, the structural issue, severity, and a suggestion."
        )
