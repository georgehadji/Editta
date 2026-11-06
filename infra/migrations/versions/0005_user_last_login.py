"""remember when each user last signed in, so a sign-in link works only once

A sign-in link is a signed token with no server-side state: anyone who saw the URL (a
proxy log, a forwarded email) could use it again until it expired. The sign-in time lets
the server refuse every link issued before the user's latest sign-in.

Nullable, no backfill: a user who never signed in has no spent links. RLS needs no change;
the column belongs to the already-protected `users` table.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-06
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "last_login_at")
