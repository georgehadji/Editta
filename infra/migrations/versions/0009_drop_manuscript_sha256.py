"""Drop manuscripts.sha256

Written on every upload, read by nothing: blob keys stopped using the hash, and no
deduplication was ever built on it.

No RLS change. The downgrade restores the column as in 0001; existing rows get an
empty hash, since the original bytes may already be purged.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-11
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("manuscripts", "sha256")


def downgrade() -> None:
    op.add_column("manuscripts", sa.Column("sha256", sa.String, nullable=False, server_default=""))
