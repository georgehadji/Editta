"""worker role with BYPASSRLS

The worker claims jobs across every tenant by design (adapters/queue/postgres.py) —
it can't set app.current_tenant_id before it knows which tenant a job belongs to.
A role bound by the RLS policies from 0002 would see zero claimable rows, ever.
This role is trusted system code, not a request-serving path: tenant scoping for
it comes from the repository layer's explicit tenant_id parameters (PLAN.md §5),
with RLS staying the API layer's defense-in-depth, not the worker's.

Password comes from EDITTA_WORKER_DB_PASSWORD (never hardcoded — see security.md).
Point WORKER_DATABASE_URL at this role in production; local dev falls back to the
same role as the API when that env var is unset (see config.py).

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08
"""

import os

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_TABLES = (
    "tenants",
    "users",
    "manuscripts",
    "analysis_jobs",
    "pass_results",
    "reports",
    "credit_ledger",
    "audit_events",
)


def upgrade() -> None:
    password = os.environ["EDITTA_WORKER_DB_PASSWORD"]
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'editta_worker') THEN
                CREATE ROLE editta_worker WITH LOGIN PASSWORD '{password}' BYPASSRLS;
            END IF;
        END
        $$;
        """
    )
    for table in _TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO editta_worker")


def downgrade() -> None:
    for table in _TABLES:
        op.execute(f"REVOKE ALL ON {table} FROM editta_worker")
    op.execute("DROP ROLE IF EXISTS editta_worker")
