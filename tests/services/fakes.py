"""In-memory fakes for services/jobs.py and API route contract tests — exercise
JobRunner and FastAPI routes without Postgres.

Duck-typed against the repository/BlobStore method signatures the real code calls;
real Postgres-backed behavior (RLS, SKIP LOCKED, JSONB) needs testcontainers (see
tests/adapters/test_persistence_integration.py — unverified without Docker).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from editta.domain.states import JobState


class _ManuscriptRecord:
    def __init__(
        self, tenant_id: UUID, blob_key: str, title: str, retention_until: Any = None
    ) -> None:
        self.id = uuid4()
        self.tenant_id = tenant_id
        self.blob_key = blob_key
        self.title = title
        self.retention_until = retention_until


class FakeManuscriptRepo:
    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID], _ManuscriptRecord] = {}

    def seed(
        self,
        tenant_id: UUID,
        manuscript_id: UUID,
        *,
        blob_key: str,
        title: str,
        retention_until: Any = None,
    ) -> None:
        record = _ManuscriptRecord(tenant_id, blob_key, title, retention_until)
        record.id = manuscript_id
        self._records[(tenant_id, manuscript_id)] = record

    async def create(
        self,
        *,
        tenant_id: UUID,
        title: str,
        blob_key: str,
        sha256: str,
        retention_until: Any = None,
    ) -> _ManuscriptRecord:
        record = _ManuscriptRecord(tenant_id, blob_key, title, retention_until)
        self._records[(tenant_id, record.id)] = record
        return record

    async def get_by_id(self, tenant_id: UUID, manuscript_id: UUID) -> _ManuscriptRecord | None:
        return self._records.get((tenant_id, manuscript_id))

    async def list_expired(self, before: Any) -> list[_ManuscriptRecord]:
        return [
            r for r in self._records.values() if r.retention_until and r.retention_until < before
        ]

    async def delete(self, manuscript: _ManuscriptRecord) -> None:
        key = next(k for k, v in self._records.items() if v is manuscript)
        del self._records[key]


class _JobRecord:
    def __init__(self, tenant_id: UUID, manuscript_id: UUID) -> None:
        self.id = uuid4()
        self.tenant_id = tenant_id
        self.manuscript_id = manuscript_id
        self.state = JobState.PENDING
        self.error: str | None = None


class FakeAnalysisJobRepo:
    def __init__(self) -> None:
        self._records: dict[tuple[UUID, UUID], _JobRecord] = {}
        self.state_history: list[tuple[JobState, str | None]] = []

    async def create(self, *, tenant_id: UUID, manuscript_id: UUID) -> _JobRecord:
        record = _JobRecord(tenant_id, manuscript_id)
        self._records[(tenant_id, record.id)] = record
        return record

    async def get_by_id(self, tenant_id: UUID, job_id: UUID) -> _JobRecord | None:
        return self._records.get((tenant_id, job_id))

    async def update_state(
        self, tenant_id: UUID, job_id: UUID, *, state: JobState, error: str | None = None
    ) -> None:
        self.state_history.append((state, error))
        record = self._records.get((tenant_id, job_id))
        if record is not None:
            record.state, record.error = state, error


class _PassResultRecord:
    def __init__(
        self, job_id: UUID, pass_name: str, blob_key: str, model: str, usage: dict[str, Any]
    ) -> None:
        self.job_id = job_id
        self.pass_name = pass_name
        self.blob_key = blob_key
        self.model = model
        self.usage = usage


class FakePassResultRepo:
    def __init__(self) -> None:
        self._rows: list[_PassResultRecord] = []

    def seed(self, row: _PassResultRecord) -> None:
        self._rows.append(row)

    async def upsert(
        self, *, job_id: UUID, pass_name: str, blob_key: str, model: str, usage: dict[str, Any]
    ) -> _PassResultRecord:
        existing = next(
            (r for r in self._rows if r.job_id == job_id and r.pass_name == pass_name), None
        )
        if existing is not None:
            existing.blob_key, existing.model, existing.usage = blob_key, model, usage
            return existing
        row = _PassResultRecord(job_id, pass_name, blob_key, model, usage)
        self._rows.append(row)
        return row

    async def list_for_job(self, job_id: UUID) -> list[_PassResultRecord]:
        return [r for r in self._rows if r.job_id == job_id]


class _ReportRecord:
    def __init__(self, job_id: UUID, blob_key: str, summary_blob_key: str | None) -> None:
        self.id = uuid4()
        self.job_id = job_id
        self.blob_key = blob_key
        self.summary_blob_key = summary_blob_key


class FakeReportRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._records: dict[UUID, _ReportRecord] = {}

    async def create(
        self, *, job_id: UUID, blob_key: str, summary_blob_key: str | None = None
    ) -> _ReportRecord:
        record = _ReportRecord(job_id, blob_key, summary_blob_key)
        self._records[job_id] = record
        self.created.append({"job_id": job_id, "blob_key": blob_key})
        return record

    async def get_by_job_id(self, job_id: UUID) -> _ReportRecord | None:
        return self._records.get(job_id)


class FakeLedgerRepo:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._seen_keys: set[str] = set()

    async def append(
        self,
        *,
        tenant_id: UUID,
        delta: int,
        reason: str,
        idempotency_key: str,
        job_id: UUID | None = None,
        stripe_ref: str | None = None,
    ) -> None:
        if idempotency_key in self._seen_keys:
            raise IntegrityError("insert", {}, Exception("duplicate idempotency_key"))
        self._seen_keys.add(idempotency_key)
        self.entries.append({"tenant_id": tenant_id, "delta": delta, "reason": reason})


class FakeAuditRepo:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def append(self, *, tenant_id: UUID, actor: str, action: str, subject: str) -> None:
        self.entries.append(
            {"tenant_id": tenant_id, "actor": actor, "action": action, "subject": subject}
        )


class FakeBlobStore:
    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}

    def seed(self, key: str, data: bytes) -> None:
        self._data[key] = data

    async def put(self, key: str, data: AsyncIterator[bytes]) -> None:
        self._data[key] = b"".join([chunk async for chunk in data])

    async def get(self, key: str) -> AsyncIterator[bytes]:
        yield self._data[key]

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            del self._data[key]

    async def presigned_get_url(self, key: str, *, expires_in: int = 3600) -> str:
        return f"fake://{key}"


class FakeTenantRepo:
    async def create(self, *, name: str) -> Any:
        return type("Tenant", (), {"id": uuid4(), "name": name})()


class FakeUserRepo:
    def __init__(self) -> None:
        self._by_email: dict[tuple[UUID, str], Any] = {}

    async def get_by_email(self, tenant_id: UUID, email: str) -> Any:
        return self._by_email.get((tenant_id, email))

    async def create(self, *, tenant_id: UUID, email: str, role: str = "editor") -> Any:
        attrs = {"id": uuid4(), "tenant_id": tenant_id, "email": email, "role": role}
        user = type("User", (), attrs)()
        self._by_email[(tenant_id, email)] = user
        return user
