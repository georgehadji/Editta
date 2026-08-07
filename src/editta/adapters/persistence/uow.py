"""Unit of Work: transactional boundary over an async SQLAlchemy session.

Also sets the RLS session variable that migration 0002 reads, scoped to the
transaction (`is_local=true`) so it never leaks across pooled connections.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class UnitOfWork:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    @asynccontextmanager
    async def begin(self, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            yield session
