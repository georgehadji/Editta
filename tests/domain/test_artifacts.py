from uuid import uuid4

import pytest

from editta.adapters.storage.s3 import attachment_disposition
from editta.domain.artifacts import (
    DOCX_CONTENT_TYPE,
    ArtifactKind,
    artifact_blob_key,
    artifact_content_type,
    artifact_filename,
)


def test_blob_key_is_built_from_the_job_and_the_kind_only() -> None:
    job_id = uuid4()

    assert artifact_blob_key(job_id, ArtifactKind.REPORT) == f"jobs/{job_id}/report.docx"
    assert artifact_content_type(ArtifactKind.REPORT) == DOCX_CONTENT_TYPE


def test_the_commented_manuscript_has_its_own_key_and_download_name() -> None:
    job_id = uuid4()
    kind = ArtifactKind.ANNOTATED_MANUSCRIPT

    assert artifact_blob_key(job_id, kind) == f"jobs/{job_id}/annotated_manuscript.docx"
    assert artifact_content_type(kind) == DOCX_CONTENT_TYPE
    assert artifact_filename("Το βιβλίο.docx", kind) == "Το βιβλίο-editta-comments.docx"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("essay.docx", "essay-editta-report.docx"),
        ("ESSAY.PDF", "ESSAY-editta-report.docx"),
        ("notes v1.2", "notes v1.2-editta-report.docx"),  # only a source extension is dropped
        ("Το βιβλίο μου.docx", "Το βιβλίο μου-editta-report.docx"),
        ("", "manuscript-editta-report.docx"),
        (".docx", "manuscript-editta-report.docx"),
    ],
)
def test_download_name_is_the_authors_name_plus_a_suffix(title: str, expected: str) -> None:
    assert artifact_filename(title, ArtifactKind.REPORT) == expected


def test_attachment_disposition_forces_a_download_and_carries_a_greek_name() -> None:
    header = attachment_disposition("Το βιβλίο-editta-report.docx")

    assert header.startswith("attachment;")
    assert 'filename="__ ______-editta-report.docx"' in header  # ASCII fallback
    assert "filename*=UTF-8''%CE%A4%CE%BF%20%CE%B2" in header


def test_attachment_disposition_cannot_be_broken_out_of_by_the_file_name() -> None:
    header = attachment_disposition('a"; filename=evil.html; x=".docx')

    fallback = header.split("; filename*=")[0]
    assert fallback.count('"') == 2  # only the two delimiters survive
    assert ";" not in fallback.removeprefix("attachment; ")
