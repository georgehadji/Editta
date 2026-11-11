"""In-memory fakes for services/jobs.py and API route contract tests — exercise
JobRunner and FastAPI routes without Postgres.

Duck-typed against the repository/BlobStore method signatures the real code calls;
real Postgres-backed behavior (RLS, SKIP LOCKED, JSONB) needs testcontainers (see
tests/adapters/test_persistence_integration.py — unverified without Docker).
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

from editta.domain.artifacts import ArtifactKind
from editta.domain.errors import LeaseLost
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
        self.status = "uploaded"


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

    async def titles(self, tenant_id: UUID, manuscript_ids: list[UUID]) -> dict[UUID, str]:
        return {
            m: self._records[(tenant_id, m)].title
            for m in manuscript_ids
            if (tenant_id, m) in self._records
        }

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
            r
            for r in self._records.values()
            if r.retention_until and r.retention_until < before and r.status != "purged"
        ]

    async def mark_purged(self, tenant_id: UUID, manuscript_id: UUID) -> None:
        record = self._records[(tenant_id, manuscript_id)]
        record.status, record.blob_key = "purged", ""


class _JobRecord:
    def __init__(self, tenant_id: UUID, manuscript_id: UUID) -> None:
        self.id = uuid4()
        self.tenant_id = tenant_id
        self.manuscript_id = manuscript_id
        self.state = JobState.PENDING
        self.error: str | None = None
        self.failure_kind: str | None = None
        self.attempt = 0
        self.lease_owner: str | None = None
        self.cost_input_tokens = 0
        self.cost_output_tokens = 0
        self.cache_read_tokens = 0


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

    async def list_ids_for_manuscript(self, tenant_id: UUID, manuscript_id: UUID) -> list[UUID]:
        return [
            r.id
            for (t, _), r in self._records.items()
            if t == tenant_id and r.manuscript_id == manuscript_id
        ]

    async def list_for_tenant(self, tenant_id: UUID, *, limit: int = 50) -> list[_JobRecord]:
        return [r for (t, _), r in self._records.items() if t == tenant_id][:limit]

    def seed(
        self, tenant_id: UUID, job_id: UUID, manuscript_id: UUID, *, lease_owner: str | None
    ) -> _JobRecord:
        record = _JobRecord(tenant_id, manuscript_id)
        record.id = job_id
        record.lease_owner = lease_owner
        self._records[(tenant_id, job_id)] = record
        return record

    async def set_state_if_owned(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        lease_owner: str,
        state: str,
        error: str | None = None,
        failure_kind: str | None = None,
    ) -> None:
        record = self._records.get((tenant_id, job_id))
        if record is None or record.lease_owner != lease_owner:
            raise LeaseLost(f"worker {lease_owner} no longer holds job {job_id}")
        self.state_history.append((JobState(state), error))
        record.state, record.error = JobState(state), error
        record.failure_kind = failure_kind

    async def record_usage(
        self,
        tenant_id: UUID,
        job_id: UUID,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
    ) -> None:
        record = self._records.get((tenant_id, job_id))
        if record is not None:
            record.cost_input_tokens += input_tokens
            record.cost_output_tokens += output_tokens
            record.cache_read_tokens += cache_read_tokens


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

    async def delete_for_jobs(self, job_ids: list[UUID]) -> None:
        self._rows = [r for r in self._rows if r.job_id not in job_ids]

    async def list_for_job(self, job_id: UUID) -> list[_PassResultRecord]:
        return [r for r in self._rows if r.job_id == job_id]


class _ReportRecord:
    def __init__(
        self, job_id: UUID, blob_key: str, kind: ArtifactKind = ArtifactKind.REPORT
    ) -> None:
        self.id = uuid4()
        self.job_id = job_id
        self.kind = kind
        self.blob_key = blob_key


class FakeReportRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self._records: dict[tuple[UUID, ArtifactKind], _ReportRecord] = {}

    async def create(self, *, job_id: UUID, kind: ArtifactKind, blob_key: str) -> None:
        # Mirrors the real repo's upsert-on-(job_id, kind): one row per file of a job.
        self._records[(job_id, kind)] = _ReportRecord(job_id, blob_key, kind)
        self.created.append({"job_id": job_id, "kind": kind, "blob_key": blob_key})

    async def get(self, job_id: UUID, kind: ArtifactKind) -> _ReportRecord | None:
        return self._records.get((job_id, kind))

    async def kinds_by_job(self, job_ids: list[UUID]) -> dict[UUID, set[str]]:
        found: dict[UUID, set[str]] = {}
        for job_id, kind in self._records:
            if job_id in job_ids:
                found.setdefault(job_id, set()).add(kind.value)
        return found

    async def delete_for_jobs(self, job_ids: list[UUID]) -> None:
        for key in [k for k in self._records if k[0] in job_ids]:
            del self._records[key]


class FakeLedgerRepo:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._seen_keys: set[str] = set()
        self._deltas: dict[str, int] = {}

    async def append_once(
        self,
        *,
        tenant_id: UUID,
        delta: int,
        reason: str,
        idempotency_key: str,
        job_id: UUID | None = None,
        stripe_ref: str | None = None,
    ) -> bool:
        if idempotency_key in self._seen_keys:
            return False
        self._seen_keys.add(idempotency_key)
        self._deltas[idempotency_key] = delta
        self.entries.append({"tenant_id": tenant_id, "delta": delta, "reason": reason})
        return True

    def seed_reservation(self, tenant_id: UUID, job_id: UUID, credits: int) -> None:
        """What submit_manuscript leaves behind: the credits already spent on the job."""
        key = f"job:{job_id}:reserve"
        self._seen_keys.add(key)
        self._deltas[key] = -credits
        self.entries.append({"tenant_id": tenant_id, "delta": -credits, "reason": "job"})

    async def entry_delta(self, idempotency_key: str) -> int | None:
        return self._deltas.get(idempotency_key)

    async def balance(self, tenant_id: UUID) -> int:
        return sum(e["delta"] for e in self.entries if e["tenant_id"] == tenant_id)


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
        self.download_names: list[str | None] = []

    def seed(self, key: str, data: bytes) -> None:
        self._data[key] = data

    async def put(self, key: str, data: AsyncIterator[bytes]) -> None:
        self._data[key] = b"".join([chunk async for chunk in data])

    async def get(self, key: str) -> AsyncIterator[bytes]:
        yield self._data[key]

    async def delete_prefix(self, prefix: str) -> None:
        for key in [k for k in self._data if k.startswith(prefix)]:
            del self._data[key]

    async def presigned_get_url(
        self, key: str, *, expires_in: int = 3600, download_name: str | None = None
    ) -> str:
        self.download_names.append(download_name)
        return f"fake://{key}"


class FakeTenantRepo:
    def __init__(self) -> None:
        self.locked: list[UUID] = []
        self.known: set[UUID] | None = None  # None = every tenant id exists

    async def create(self, *, name: str) -> Any:
        tenant = type("Tenant", (), {"id": uuid4(), "name": name})()
        if self.known is not None:
            self.known.add(tenant.id)
        return tenant

    async def lock(self, tenant_id: UUID) -> bool:
        self.locked.append(tenant_id)
        return self.known is None or tenant_id in self.known


class FakeUserRepo:
    """One repo for all tenants; every method is keyed by tenant_id like the real one."""

    def __init__(self) -> None:
        self._by_email: dict[tuple[UUID, str], Any] = {}

    async def get_by_email(self, tenant_id: UUID, email: str) -> Any:
        return self._by_email.get((tenant_id, email))

    async def get_by_id(self, tenant_id: UUID, user_id: UUID) -> Any:
        return next(
            (u for (t, _), u in self._by_email.items() if t == tenant_id and u.id == user_id),
            None,
        )

    async def create(self, *, tenant_id: UUID, email: str, role: str = "editor") -> Any:
        attrs = {
            "id": uuid4(),
            "tenant_id": tenant_id,
            "email": email,
            "role": role,
            "last_login_at": None,
        }
        user = type("User", (), attrs)()
        self._by_email[(tenant_id, email)] = user
        return user

    async def consume_login(
        self, tenant_id: UUID, user_id: UUID, *, issued_at: Any, now: Any
    ) -> bool:
        user = await self.get_by_id(tenant_id, user_id)
        if user is None or (user.last_login_at is not None and user.last_login_at >= issued_at):
            return False
        user.last_login_at = now
        return True

    def remove(self, tenant_id: UUID, email: str) -> None:
        del self._by_email[(tenant_id, email)]


class RecordingEmailSender:
    """Captures sign-in emails so tests read the link from the 'inbox', like a user."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_magic_link(self, *, to_email: str, link_url: str) -> None:
        self.sent.append((to_email, link_url))

    def last_token_for(self, email: str) -> str:
        link = next(url for to, url in reversed(self.sent) if to == email)
        return link.split("token=", 1)[1]


def make_account_service(
    users: FakeUserRepo, *, secret: str, tenants: FakeTenantRepo | None = None
) -> Any:
    from contextlib import asynccontextmanager

    from editta.services.accounts import AccountService

    @asynccontextmanager
    async def user_store(tenant_id: UUID) -> AsyncIterator[Any]:
        yield users

    @asynccontextmanager
    async def tenant_store() -> AsyncIterator[Any]:
        yield tenants or FakeTenantRepo()

    return AccountService(
        users=user_store,  # type: ignore[arg-type]
        tenants=tenant_store,  # type: ignore[arg-type]
        session_secret=secret,
    )


class FakeJobUnitOfWork:
    """Transactional fake for services.jobs.JobUnitOfWork.

    Like a real Postgres transaction: writes made inside a unit that raises are rolled
    back (repository state is snapshotted on entry and restored on error). This is what
    makes "checkpoints survive a failed attempt" testable without a database.
    """

    def __init__(self) -> None:
        from editta.services.jobs import JobRepositories

        self.jobs = FakeAnalysisJobRepo()
        self.manuscripts = FakeManuscriptRepo()
        self.pass_results = FakePassResultRepo()
        self.reports = FakeReportRepo()
        self.ledger = FakeLedgerRepo()
        self._repos = JobRepositories(
            jobs=self.jobs,  # type: ignore[arg-type]
            manuscripts=self.manuscripts,  # type: ignore[arg-type]
            pass_results=self.pass_results,  # type: ignore[arg-type]
            reports=self.reports,  # type: ignore[arg-type]
            ledger=self.ledger,  # type: ignore[arg-type]
        )
        self.commits = 0
        self.rollbacks = 0

    def __call__(self) -> Any:
        import copy
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def unit() -> AsyncIterator[Any]:
            repos = (self.jobs, self.manuscripts, self.pass_results, self.reports, self.ledger)
            snapshot = [copy.deepcopy(r.__dict__) for r in repos]
            try:
                yield self._repos
            except BaseException:
                for repo, state in zip(repos, snapshot, strict=True):
                    repo.__dict__.clear()
                    repo.__dict__.update(state)
                self.rollbacks += 1
                raise
            self.commits += 1

        return unit()
