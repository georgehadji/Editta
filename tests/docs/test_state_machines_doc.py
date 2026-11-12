"""docs/STATE_MACHINES.md must name every state the code has: a new JobState, JobPhase
or ArtifactKind value without a doc line fails here (docs/STATE_MACHINES_PLAN.md, item 8)."""

from enum import StrEnum
from pathlib import Path

import pytest

from editta.domain.artifacts import ArtifactKind
from editta.domain.job_view import JobPhase
from editta.domain.states import JobState

DOC = Path(__file__).parents[2] / "docs" / "STATE_MACHINES.md"


@pytest.mark.parametrize("enum", [JobState, JobPhase, ArtifactKind])
def test_every_state_value_is_in_the_catalog(enum: type[StrEnum]) -> None:
    text = DOC.read_text(encoding="utf-8")
    missing = [m.value for m in enum if f"`{m.value}`" not in text]
    assert not missing, f"add {missing} ({enum.__name__}) to {DOC.name}"
