"""Integrity-light pass: unsupported claims + repetitions. Never AI-detection or plagiarism."""

from editta.domain.models import PassFindings
from editta.passes.base import BaseAnalysisPass


class IntegrityPass(BaseAnalysisPass):
    name = "integrity"
    output_schema = PassFindings

    def instructions(self) -> str:
        return (
            "Light integrity check only: flag unsupported factual claims made without "
            "attribution and notable repetitions across the text. This is not plagiarism "
            "detection or AI-detection — do not attempt either. Return findings with "
            "exact quoted passages, the issue, severity, and a suggestion."
        )
