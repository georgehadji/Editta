"""The files a job produces. Pure data and naming rules -- no I/O.

Storage keys and download names come from the enum, never from request input: the
download route validates the kind against `ArtifactKind`, so a caller cannot steer a key.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from editta.domain.models import ManuscriptText, PassResult, SynthesisReport

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_SOURCE_EXTENSION = re.compile(r"\.(docx|pdf)$", re.IGNORECASE)


class ArtifactKind(StrEnum):
    REPORT = "report"


@dataclass(frozen=True, slots=True)
class _ArtifactSpec:
    extension: str
    content_type: str
    filename_suffix: str  # appended to the manuscript's own name in the download


_SPECS: Mapping[ArtifactKind, _ArtifactSpec] = MappingProxyType(
    {ArtifactKind.REPORT: _ArtifactSpec("docx", DOCX_CONTENT_TYPE, "-editta-report")}
)


def artifact_content_type(kind: ArtifactKind) -> str:
    return _SPECS[kind].content_type


def artifact_blob_key(job_id: UUID, kind: ArtifactKind) -> str:
    """Everything under `jobs/{job_id}/` is purged by retention (services/retention.py)."""
    return f"jobs/{job_id}/{kind.value}.{_SPECS[kind].extension}"


def artifact_filename(manuscript_title: str, kind: ArtifactKind) -> str:
    """Name the author sees in the download: their own file name plus a suffix, so the
    file lands next to the original. `manuscript_title` is already sanitized at upload."""
    spec = _SPECS[kind]
    stem = _SOURCE_EXTENSION.sub("", manuscript_title) or "manuscript"
    return f"{stem}{spec.filename_suffix}.{spec.extension}"


@dataclass(frozen=True, slots=True)
class ExportContext:
    """Everything an exporter may read. Frozen: an exporter cannot change the inputs the
    next one sees."""

    manuscript: ManuscriptText
    pass_results: tuple[PassResult, ...]
    synthesis: SynthesisReport
