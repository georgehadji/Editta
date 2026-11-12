"""CHECK constraints on the free-text state columns

`analysis_jobs.state`, `analysis_jobs.failure_kind`, `manuscripts.status` and
`reports.kind` were plain strings. A typo or a hand edit would silently drop a job out of
the queue's claim and health SQL, or show the wrong thing on the dashboard
(docs/STATE_MACHINES_PLAN.md, item 2).

Each constraint is added NOT VALID: from now on it guards every insert and update, and it
never fails on rows already there. Then it is VALIDATEd, unless an existing row breaks
it. Then the migration warns and leaves that one constraint NOT VALID instead of failing
the deploy: fix the rows (the data checks are in the plan, section 5) and run
`ALTER TABLE ... VALIDATE CONSTRAINT ...` by hand.

The value lists are frozen here on purpose: a migration must not change when the code's
enums do. Adding a JobState or ArtifactKind value needs a new migration.

No RLS change.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

_CHECKS = (
    (
        "analysis_jobs",
        "ck_job_state",
        "state IN ('pending','parsing','analyzing','synthesizing','exporting','completed','dead')",
    ),
    # Only a dead job can carry a failure kind, and 'customer' is the only kind
    # (domain/states.py CUSTOMER_FAILURE). NULL means "not marked", counted as ours.
    (
        "analysis_jobs",
        "ck_job_failure_kind",
        "failure_kind IS NULL OR (failure_kind = 'customer' AND state = 'dead')",
    ),
    ("manuscripts", "ck_manuscript_status", "status IN ('uploaded','purged')"),
    # One fact, two columns: retention empties blob_key when it purges (repositories.py).
    (
        "manuscripts",
        "ck_manuscript_purged_blob",
        "(status = 'purged') = (blob_key = '')",
    ),
    ("reports", "ck_report_kind", "kind IN ('report','annotated_manuscript')"),
)


def upgrade() -> None:
    for table, name, condition in _CHECKS:
        op.create_check_constraint(name, table, condition, postgresql_not_valid=True)
        # Identifiers are module constants above, not input.
        op.execute(
            "DO $$ BEGIN "  # noqa: S608
            f"ALTER TABLE {table} VALIDATE CONSTRAINT {name}; "
            "EXCEPTION WHEN check_violation THEN "
            f"RAISE WARNING '{name}: existing rows break it; left NOT VALID'; "
            "END $$;"
        )


def downgrade() -> None:
    for table, name, _ in reversed(_CHECKS):
        op.drop_constraint(name, table, type_="check")
