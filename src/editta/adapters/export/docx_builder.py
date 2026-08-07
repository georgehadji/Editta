"""Fluent DOCX report builder: Report -> styled .docx. No verdict/score rendered anywhere."""

from __future__ import annotations

import io

import docx

from editta.domain.models import Finding, Report


class ReportDocxBuilder:
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
        self._document.add_heading("Editorial Report", level=0)
        self._document.add_paragraph(f"Manuscript: {self._report.manuscript_filename}")
        self._document.add_paragraph(f"Generated: {self._report.generated_at.isoformat()}")
        return self

    def _add_synthesis(self) -> ReportDocxBuilder:
        self._document.add_heading("Overview", level=1)
        self._document.add_paragraph(self._report.synthesis.overview)
        for section in self._report.synthesis.sections:
            self._document.add_heading(section.title, level=2)
            self._document.add_paragraph(section.summary)
            self._add_findings(section.findings)
        return self

    def _add_pass_details(self) -> ReportDocxBuilder:
        self._document.add_heading("Detailed Findings by Pass", level=1)
        for pass_result in self._report.pass_results:
            self._document.add_heading(pass_result.pass_name.title(), level=2)
            self._add_findings(pass_result.findings)
        return self

    def _add_findings(self, findings: list[Finding]) -> None:
        for finding in findings:
            bullet = self._document.add_paragraph(style="List Bullet")
            bullet.add_run(f"[{finding.severity.value}] {finding.section}: ").bold = True
            bullet.add_run(finding.issue)
            self._document.add_paragraph(f'"{finding.quote}"', style="Intense Quote")
            self._document.add_paragraph(f"Suggestion: {finding.suggestion}")
