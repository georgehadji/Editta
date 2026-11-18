"""Fluent DOCX report builder: Report -> styled .docx. No verdict/score rendered anywhere."""

from __future__ import annotations

import io

import docx

from editta.adapters.export.annotated_docx import PASS_LABELS, SEVERITY_LABELS, SUGGESTION_LABEL
from editta.domain.artifacts import ArtifactKind, ExportContext
from editta.domain.models import Finding, Report
from editta.domain.report import assemble_report
from editta.domain.xmltext import strip_xml_illegal as clean


class ReportDocxBuilder:
    """Everything the model wrote goes through `clean`: one control character in a finding
    would otherwise make python-docx raise, and the job would die after paying for the model."""

    def __init__(self, report: Report) -> None:
        self._report = report
        self._document = docx.Document()

    def build(self) -> bytes:
        self._add_title_page()
        self._add_synthesis()
        self._add_pass_details()
        buffer = io.BytesIO()
        self._document.save(buffer)
        return buffer.getvalue()

    def _add_title_page(self) -> ReportDocxBuilder:
        self._document.add_heading("Έκθεση επιμέλειας", level=0)
        self._document.add_paragraph(clean(f"Κείμενο: {self._report.manuscript_filename}"))
        self._document.add_paragraph(f"Ημερομηνία: {self._report.generated_at.isoformat()}")
        return self

    def _add_synthesis(self) -> ReportDocxBuilder:
        self._document.add_heading("Επισκόπηση", level=1)
        self._document.add_paragraph(clean(self._report.synthesis.overview))
        for section in self._report.synthesis.sections:
            self._document.add_heading(clean(section.title), level=2)
            self._document.add_paragraph(clean(section.summary))
            self._add_findings(section.findings)
        return self

    def _add_pass_details(self) -> ReportDocxBuilder:
        self._document.add_heading("Αναλυτικά ευρήματα ανά έλεγχο", level=1)
        for pass_result in self._report.pass_results:
            name = PASS_LABELS.get(pass_result.pass_name, pass_result.pass_name)
            self._document.add_heading(clean(name), level=2)
            self._add_findings(pass_result.findings)
        return self

    def _add_findings(self, findings: list[Finding]) -> None:
        for finding in findings:
            bullet = self._document.add_paragraph(style="List Bullet")
            severity = SEVERITY_LABELS[finding.severity.value]
            bullet.add_run(clean(f"[{severity}] {finding.section}: ")).bold = True
            bullet.add_run(clean(finding.issue))
            self._document.add_paragraph(clean(f"«{finding.quote}»"), style="Intense Quote")
            self._document.add_paragraph(clean(f"{SUGGESTION_LABEL}: {finding.suggestion}"))


class ReportExporter:
    """The editorial report as an ArtifactExporter (domain/ports.py)."""

    kind = ArtifactKind.REPORT

    def build(self, ctx: ExportContext) -> bytes | None:
        report = assemble_report(
            manuscript_filename=ctx.manuscript.source_filename,
            pass_results=list(ctx.pass_results),
            synthesis=ctx.synthesis,
        )
        return ReportDocxBuilder(report).build()
