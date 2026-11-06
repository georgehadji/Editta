"""a job may have several output files: `reports.kind`, unique per (job_id, kind)

Until now a job had one report (UNIQUE job_id). The annotated manuscript, the
acquisitions brief and the catalogue sheet are further files of the same job. Existing
rows are the editorial report, which the column default records.

No RLS change: `reports` is authorized through its job, and the policies from 0002/0004
test the job, not the row's own columns. No CHECK on `kind`: only the worker writes it,
from the ArtifactKind enum, and the download route validates the enum before any lookup.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reports", sa.Column("kind", sa.String, nullable=False, server_default="report"))
    # 0001 created the one-row-per-job rule as an inline UNIQUE; Postgres named it
    # reports_job_id_key. ix_reports_job_id (plain index) stays for the job lookup.
    op.drop_constraint("reports_job_id_key", "reports", type_="unique")
    op.create_unique_constraint("uq_reports_job_id_kind", "reports", ["job_id", "kind"])


def downgrade() -> None:
    # Only the editorial report fits the old one-row-per-job shape; the other files
    # are derived and can be rebuilt by re-running the job.
    op.execute("DELETE FROM reports WHERE kind <> 'report'")
    op.drop_constraint("uq_reports_job_id_kind", "reports", type_="unique")
    op.create_unique_constraint("reports_job_id_key", "reports", ["job_id"])
    op.drop_column("reports", "kind")
