"""Drop columns nothing reads or writes

Created by 0001 for features that were never built: tenants.plan, tenants.settings,
manuscripts.token_count, reports.summary_blob_key. And tenants.credit_balance_cache,
which stayed 0 for every tenant: the balance is SUM(credit_ledger.delta), so a query or
report reading the "cache" saw every tenant as broke.

No RLS change: the policies from 0002/0004 test tenant_id only. The downgrade restores
the columns with their 0001 definitions, empty (the values were never used).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("tenants", "plan")
    op.drop_column("tenants", "credit_balance_cache")
    op.drop_column("tenants", "settings")
    op.drop_column("manuscripts", "token_count")
    op.drop_column("reports", "summary_blob_key")


def downgrade() -> None:
    op.add_column("tenants", sa.Column("plan", sa.String, nullable=False, server_default="trial"))
    op.add_column(
        "tenants",
        sa.Column("credit_balance_cache", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "tenants",
        sa.Column("settings", postgresql.JSONB, nullable=False, server_default="{}"),
    )
    op.add_column("manuscripts", sa.Column("token_count", sa.Integer, nullable=True))
    op.add_column("reports", sa.Column("summary_blob_key", sa.String, nullable=True))
