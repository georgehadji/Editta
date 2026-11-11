"""analysis_jobs.failure_kind: whose fault a dead job is

A scanned PDF or an over-budget manuscript dies with a clear error and a refund: the
customer's input, nothing for an operator to look at. The queue check (worker/ops_check.py)
must not alert on those, so the runner records which kind of death it was. NULL means
"not marked", which the check counts as ours: rows written before this migration, and
every failure nobody classified, stay visible.

No RLS change: the policies from 0002/0004 test tenant_id, not this column.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("analysis_jobs", sa.Column("failure_kind", sa.String, nullable=True))


def downgrade() -> None:
    op.drop_column("analysis_jobs", "failure_kind")
