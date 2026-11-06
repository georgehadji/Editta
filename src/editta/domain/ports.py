"""Behavioral contracts (ports)."""

from collections.abc import AsyncIterator
from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

from editta.domain.artifacts import ArtifactKind, ExportContext
from editta.domain.models import CachePolicy, ClaimedJob, LLMResult, PromptParts

T = TypeVar("T", bound=BaseModel)


class LLMClient(Protocol):
    async def analyze(
        self,
        parts: PromptParts,
        schema: type[T],
        *,
        cache: CachePolicy,
        effort: str,
        model: str,
        # Models to try, in order, if `model` fails; adapters without a second
        # provider ignore them.
        fallbacks: tuple[str, ...] = (),
    ) -> LLMResult[T]: ...

    async def count_tokens(self, parts: PromptParts, *, model: str) -> int: ...


class JobQueue(Protocol):
    async def claim(self, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None: ...
    async def heartbeat(self, job_id: UUID, worker_id: str, *, lease_seconds: int) -> None: ...
    async def release(self, job_id: UUID, worker_id: str) -> None:
        """No-op unless `worker_id` still holds the lease (fenced)."""
        ...

    async def defer(self, job_id: UUID, worker_id: str, *, delay_seconds: int) -> None:
        """Release the lease but keep the job unclaimable until the delay elapses.
        No-op unless `worker_id` still holds the lease (fenced)."""
        ...


class BlobStore(Protocol):
    async def put(self, key: str, data: AsyncIterator[bytes]) -> None: ...

    # Deliberately not `async def`: implementations are async *generator* functions,
    # so calling get() returns the AsyncIterator directly — no separate await needed.
    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete_prefix(self, prefix: str) -> None: ...
    # `download_name` makes the browser save the object under that name instead of
    # rendering it (Content-Disposition: attachment); stores without URLs ignore it.
    async def presigned_get_url(
        self, key: str, *, expires_in: int, download_name: str | None = None
    ) -> str: ...


class ArtifactExporter(Protocol):
    """Builds one output file of a job. Sync and CPU-bound: the runner calls it in a
    worker thread under a timeout, because some exporters walk attacker-shaped files."""

    kind: ArtifactKind

    def build(self, ctx: ExportContext) -> bytes | None:
        """The file's bytes, or None when this artifact does not apply to the job."""
        ...


class EmailSender(Protocol):
    async def send_magic_link(self, *, to_email: str, link_url: str) -> None: ...
