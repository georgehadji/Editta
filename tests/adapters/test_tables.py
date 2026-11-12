"""The CHECK value lists in tables.py (and migration 0010) must match the domain enums: a
new JobState or ArtifactKind value without a migration would be rejected by Postgres."""

import re

from sqlalchemy import CheckConstraint, Table

from editta.adapters.persistence.tables import AnalysisJob, Report
from editta.domain.artifacts import ArtifactKind
from editta.domain.states import JobState


def _allowed_values(table: Table, name: str) -> set[str]:
    constraint = next(
        c for c in table.constraints if isinstance(c, CheckConstraint) and c.name == name
    )
    return set(re.findall(r"'([a-z_]+)'", str(constraint.sqltext)))


def test_job_state_check_lists_every_job_state() -> None:
    assert _allowed_values(AnalysisJob.__table__, "ck_job_state") == {s.value for s in JobState}


def test_report_kind_check_lists_every_artifact_kind() -> None:
    assert _allowed_values(Report.__table__, "ck_report_kind") == {k.value for k in ArtifactKind}
