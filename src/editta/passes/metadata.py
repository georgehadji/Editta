"""Metadata & positioning analysis pass."""

from editta.domain.budget import GEMINI_FLASH, HAIKU
from editta.domain.models import PassFindings
from editta.passes.base import BaseAnalysisPass


class MetadataPass(BaseAnalysisPass):
    name = "metadata"
    model = HAIKU
    fallback_models: tuple[str, ...] = (GEMINI_FLASH,)
    output_schema = PassFindings

    def instructions(self) -> str:
        return (
            "Analyze metadata and positioning: title fit, framing for the intended "
            "audience, genre expectations, and marketability signals visible in the "
            "text itself. Return findings with exact quoted passages, the issue, "
            "severity, and a suggestion."
        )
