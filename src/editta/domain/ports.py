"""Behavioral contracts (ports)."""

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

from editta.domain.models import CachePolicy, ClaimedJob, LLMResult, PromptParts

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    async def analyze(
        self, parts: PromptParts, schema: type[T], *, cache: CachePolicy, effort: str
    ) -> LLMResult[T]: ...

    async def count_tokens(self, parts: PromptParts) -> int: ...


class JobQueue(Protocol):
    async def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None: ...
    async def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int) -> None: ...
    async def release(self, job_id: UUID) -> None: ...

    async def defer(self, job_id: UUID, *, delay_seconds: int) -> None:
        """Release the lease but keep the job unclaimable until the delay elapses."""
        ...


class BlobStore(Protocol):
    async def put(self, key: str, data: AsyncIterator[bytes]) -> None: ...

    # Deliberately not `async def`: implementations are async *generator* functions,
    # so calling get() returns the AsyncIterator directly — no separate await needed.
    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete_prefix(self, prefix: str) -> None: ...
    async def presigned_get_url(self, key: str, *, expires_in: int) -> str: ...


class EmailSender(Protocol):
    async def send_magic_link(self, *, to_email: str, link_url: str) -> None: ...
