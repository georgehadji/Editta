"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("plan", sa.String, nullable=False, server_default="trial"),
        sa.Column("credit_balance_cache", sa.Integer, nullable=False, server_default="0"),
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("email", sa.String, nullable=False),
        sa.Column("role", sa.String, nullable=False, server_default="editor"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "email"),
        sa.CheckConstraint("role in ('admin','editor')", name="ck_user_role"),
    )
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"])

    op.create_table(
        "manuscripts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("blob_key", sa.String, nullable=False),
        sa.Column("sha256", sa.String, nullable=False),
        sa.Column("token_count", sa.Integer, nullable=True),
        sa.Column("status", sa.String, nullable=False, server_default="uploaded"),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_manuscripts_tenant_id", "manuscripts", ["tenant_id"])

    op.create_table(
        "analysis_jobs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("manuscript_id", _UUID, sa.ForeignKey("manuscripts.id"), nullable=False),
        sa.Column("state", sa.String, nullable=False, server_default="pending"),
        sa.Column("error", sa.String, nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_input_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_output_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lease_owner", sa.String, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("attempt >= 0", name="ck_job_attempt_nonneg"),
    )
    op.create_index("ix_analysis_jobs_tenant_id", "analysis_jobs", ["tenant_id"])
    op.create_index("ix_analysis_jobs_manuscript_id", "analysis_jobs", ["manuscript_id"])
    op.create_index("ix_analysis_jobs_state", "analysis_jobs", ["state"])

    op.create_table(
        "pass_results",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("job_id", _UUID, sa.ForeignKey("analysis_jobs.id"), nullable=False),
        sa.Column("pass_name", sa.String, nullable=False),
        sa.Column("blob_key", sa.String, nullable=False),
        sa.Column("model", sa.String, nullable=False),
        sa.Column("usage", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("job_id", "pass_name"),
    )
    op.create_index("ix_pass_results_job_id", "pass_results", ["job_id"])

    op.create_table(
        "reports",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("job_id", _UUID, sa.ForeignKey("analysis_jobs.id"), nullable=False, unique=True),
        sa.Column("blob_key", sa.String, nullable=False),
        sa.Column("summary_blob_key", sa.String, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_reports_job_id", "reports", ["job_id"])

    op.create_table(
        "credit_ledger",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("delta", sa.Integer, nullable=False),
        sa.Column("reason", sa.String, nullable=False),
        sa.Column("job_id", _UUID, sa.ForeignKey("analysis_jobs.id"), nullable=True),
        sa.Column("stripe_ref", sa.String, nullable=True),
        sa.Column("idempotency_key", sa.String, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("idempotency_key"),
        sa.CheckConstraint("reason in ('purchase','job','refund')", name="ck_ledger_reason"),
    )
    op.create_index("ix_credit_ledger_tenant_id", "credit_ledger", ["tenant_id"])

    op.create_table(
        "audit_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("tenant_id", _UUID, sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("actor", sa.String, nullable=False),
        sa.Column("action", sa.String, nullable=False),
        sa.Column("subject", sa.String, nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_audit_events_tenant_id", "audit_events", ["tenant_id"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("credit_ledger")
    op.drop_table("reports")
    op.drop_table("pass_results")
    op.drop_table("analysis_jobs")
    op.drop_table("manuscripts")
    op.drop_table("users")
    op.drop_table("tenants")
