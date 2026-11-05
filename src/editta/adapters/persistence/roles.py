"""Startup check for connections that must see every tenant's rows.

The job queue and the retention sweep read across tenants before they know which
tenant a row belongs to, so they need a role with BYPASSRLS (migration 0003's
`editta_worker`). Since migration 0004 forces RLS on the table owner, the app role
sees *nothing* without a tenant context — a worker accidentally pointed at it would
claim no jobs and sweep no manuscripts, silently, forever, while uploads keep
reserving credits. This check turns that misconfiguration into a startup error.
"""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from editta.domain.errors import EdittaError


class MisconfiguredRole(EdittaError):
    pass


_ROLE_SQL = text(
    "SELECT current_user, rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user"
)


async def require_rls_bypass(engine: AsyncEngine, *, purpose: str) -> None:
    async with engine.connect() as conn:
        row = (await conn.execute(_ROLE_SQL)).one()
    role, bypasses = row[0], bool(row[1])
    if not bypasses:
        raise MisconfiguredRole(
            f"{purpose} connects as role {role!r}, which is subject to row-level security "
            "and would see no rows. Set EDITTA_WORKER_DATABASE_URL to the BYPASSRLS "
            "'editta_worker' role created by migration 0003."
        )
