"""row-level security — defense in depth alongside tenant-scoped repositories

Every tenant-scoped table only returns rows matching the connection's
`app.current_tenant_id` session setting. The application sets this once per
request/transaction (see adapters/persistence/uow.py); a bug that forgets a
`WHERE tenant_id = ...` clause in application code still can't leak cross-tenant
rows, because Postgres itself refuses to return them.

The worker's queue-claiming connection reads across ALL tenants by design and
can't set app.current_tenant_id before it knows which tenant a job belongs to —
see migration 0003, which creates a BYPASSRLS role for exactly that connection.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_DIRECT_TENANT_TABLES = ("users", "manuscripts", "analysis_jobs", "credit_ledger", "audit_events")

# pass_results / reports have no tenant_id column of their own; scope is transitive
# through analysis_jobs.job_id, so the policy joins out to it.
_JOB_SCOPED_TABLES = ("pass_results", "reports")


def upgrade() -> None:
    for table in _DIRECT_TENANT_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true)::uuid)
        """)

    for table in _JOB_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (job_id IN (
                SELECT id FROM analysis_jobs
                WHERE tenant_id = current_setting('app.current_tenant_id', true)::uuid
            ))
        """)


def downgrade() -> None:
    for table in _DIRECT_TENANT_TABLES + _JOB_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
