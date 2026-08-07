"""Local filesystem BlobStore for dev/test. S3BlobStore is the production adapter."""

import shutil
from collections.abc import AsyncIterator
from pathlib import Path


class LocalBlobStore:
    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        root = self._root.resolve()
        if path != root and root not in path.parents:
            raise ValueError(f"unsafe blob key escapes storage root: {key}")
        return path

    async def put(self, key: str, data: AsyncIterator[bytes]) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as f:
            async for chunk in data:
                f.write(chunk)

    async def get(self, key: str) -> AsyncIterator[bytes]:
        with self._path(key).open("rb") as f:
            while chunk := f.read(65536):
                yield chunk

    async def delete_prefix(self, prefix: str) -> None:
        target = self._path(prefix)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()

    async def presigned_get_url(self, key: str, *, expires_in: int = 3600) -> str:
        return self._path(key).as_uri()
