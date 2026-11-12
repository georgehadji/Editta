"""SQLAlchemy table definitions matching PLAN.md §3 data model."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # Every timestamp is TIMESTAMPTZ, matching migration 0001. Without this the ORM
    # metadata declared naive TIMESTAMP, so create_all-built schemas (the integration
    # tests) rejected the tz-aware datetimes the application writes.
    type_annotation_map = {datetime: DateTime(timezone=True)}


class UUIDPKMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Tenant(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "tenants"

    name: Mapped[str]


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email"),
        CheckConstraint("role in ('admin','editor')", name="ck_user_role"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    email: Mapped[str]
    role: Mapped[str] = mapped_column(default="editor")
    # When a sign-in link was last used. A link issued before this moment is spent
    # (services/accounts.py: links are single-use).
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)


class Manuscript(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "manuscripts"
    __table_args__ = (
        CheckConstraint("status IN ('uploaded','purged')", name="ck_manuscript_status"),
        # One fact, two columns: retention empties blob_key when it purges.
        CheckConstraint("(status = 'purged') = (blob_key = '')", name="ck_manuscript_purged_blob"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    title: Mapped[str]
    blob_key: Mapped[str]
    status: Mapped[str] = mapped_column(default="uploaded")
    retention_until: Mapped[datetime]


class AnalysisJob(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "analysis_jobs"
    # Value lists match migration 0010 and the domain enums (tests/adapters/test_tables.py).
    __table_args__ = (
        CheckConstraint("attempt >= 0", name="ck_job_attempt_nonneg"),
        CheckConstraint(
            "state IN ('pending','parsing','analyzing','synthesizing','exporting',"
            "'completed','dead')",
            name="ck_job_state",
        ),
        CheckConstraint(
            "failure_kind IS NULL OR (failure_kind = 'customer' AND state = 'dead')",
            name="ck_job_failure_kind",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    manuscript_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("manuscripts.id"), index=True)
    state: Mapped[str] = mapped_column(default="pending", index=True)
    error: Mapped[str | None] = mapped_column(default=None)
    # Set on a dead job: "customer" when the input was at fault (domain/states.py).
    failure_kind: Mapped[str | None] = mapped_column(default=None)
    attempt: Mapped[int] = mapped_column(default=0)
    cost_input_tokens: Mapped[int] = mapped_column(default=0)
    cost_output_tokens: Mapped[int] = mapped_column(default=0)
    cache_read_tokens: Mapped[int] = mapped_column(default=0)
    # Lease + heartbeat: a claimed job whose lease expires is reclaimable by another worker.
    lease_owner: Mapped[str | None] = mapped_column(default=None)
    lease_expires_at: Mapped[datetime | None] = mapped_column(default=None)


class PassResult(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "pass_results"
    __table_args__ = (UniqueConstraint("job_id", "pass_name"),)

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_jobs.id"), index=True)
    pass_name: Mapped[str]
    blob_key: Mapped[str]
    model: Mapped[str]
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Report(UUIDPKMixin, TimestampMixin, Base):
    """One row per output file of a job (domain/artifacts.py). Named `reports` since the
    report was the only file; authorized through its job, so it has no tenant_id."""

    __tablename__ = "reports"
    __table_args__ = (
        UniqueConstraint("job_id", "kind", name="uq_reports_job_id_kind"),
        CheckConstraint("kind IN ('report','annotated_manuscript')", name="ck_report_kind"),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("analysis_jobs.id"), index=True)
    kind: Mapped[str] = mapped_column(server_default="report")
    blob_key: Mapped[str]


class CreditLedger(UUIDPKMixin, Base):
    """Append-only: balance is derived as SUM(delta), never updated in place."""

    __tablename__ = "credit_ledger"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        CheckConstraint("reason in ('purchase','job','refund')", name="ck_ledger_reason"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    delta: Mapped[int]
    reason: Mapped[str]
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("analysis_jobs.id"), default=None)
    stripe_ref: Mapped[str | None] = mapped_column(default=None)
    idempotency_key: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AuditEvent(UUIDPKMixin, Base):
    """Append-only audit trail: uploads, downloads, deletions, credit changes."""

    __tablename__ = "audit_events"

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor: Mapped[str]
    action: Mapped[str]
    subject: Mapped[str]
    at: Mapped[datetime] = mapped_column(server_default=func.now())
