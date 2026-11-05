"""force row-level security on the table owner; make missing tenant context fail closed

1. FORCE. Postgres does not apply RLS policies to a table's owner unless the table is
   set to FORCE ROW LEVEL SECURITY. The app connects with the role that runs these
   migrations (.env.example's single EDITTA_DATABASE_URL), which therefore owns every
   table — so the policies from 0002 were silently bypassed and the "second,
   independent enforcement layer" (PLAN.md §5) did not exist.

2. NULLIF. 0002's policies cast `current_setting('app.current_tenant_id', true)` to
   uuid. On a pooled connection that has run a tenant-scoped transaction before, the
   setting reverts to '' (not NULL) afterwards, and ''::uuid raises — so an unscoped
   query errored on reused connections but returned no rows on fresh ones. NULLIF(…, '')
   makes every unscoped query deterministically see no rows.

Prerequisite (same change): every app code path touching these tables sets the tenant
context first — including the pre-authentication user lookups in services/accounts.py,
which previously used an unscoped session. The worker's queue/retention connection is
unaffected: it uses the BYPASSRLS `editta_worker` role from 0003, and BYPASSRLS (like
superuser) bypasses even forced RLS.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_DIRECT_TENANT_TABLES = ("users", "manuscripts", "analysis_jobs", "credit_ledger", "audit_events")
_JOB_SCOPED_TABLES = ("pass_results", "reports")

_TENANT = "NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_TENANT_0002 = "current_setting('app.current_tenant_id', true)::uuid"


def _create_policies(tenant_expr: str) -> None:
    # Table names come from the constant tuples above, never from input.
    for table in _DIRECT_TENANT_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = {tenant_expr})
            WITH CHECK (tenant_id = {tenant_expr})
        """)  # noqa: S608
    for table in _JOB_SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"""
            CREATE POLICY tenant_isolation ON {table}
            USING (job_id IN (SELECT id FROM analysis_jobs WHERE tenant_id = {tenant_expr}))
        """)  # noqa: S608


def upgrade() -> None:
    _create_policies(_TENANT)
    for table in _DIRECT_TENANT_TABLES + _JOB_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in _DIRECT_TENANT_TABLES + _JOB_SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    _create_policies(_TENANT_0002)
