from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from editta.adapters.storage.local import LocalBlobStore


async def _bytes_iter(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


@pytest.mark.asyncio
async def test_put_then_get_round_trips(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)

    await store.put("tenant/manuscript.docx", _bytes_iter([b"hello ", b"world"]))

    collected = b"".join([chunk async for chunk in store.get("tenant/manuscript.docx")])
    assert collected == b"hello world"


@pytest.mark.asyncio
async def test_delete_prefix_removes_directory(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    await store.put("tenant/a.docx", _bytes_iter([b"a"]))
    await store.put("tenant/b.docx", _bytes_iter([b"b"]))

    await store.delete_prefix("tenant")

    assert not (tmp_path / "tenant").exists()


@pytest.mark.asyncio
async def test_presigned_get_url_returns_file_uri(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    await store.put("report.docx", _bytes_iter([b"x"]))

    url = await store.presigned_get_url("report.docx")

    assert url.startswith("file:")


def test_path_traversal_key_is_rejected(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError, match="unsafe blob key"):
        store._path("../../etc/passwd")
