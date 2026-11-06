"""S3BlobStore.delete_prefix: S3 reports a refused delete as HTTP 200 plus an Errors list."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from editta.adapters.storage.s3 import S3BlobStore
from editta.domain.errors import EdittaError


class _FakeS3Client:
    """Stands in for the aioboto3 client: one page of two keys, a canned delete answer."""

    def __init__(self, delete_response: dict[str, Any]) -> None:
        self._delete_response = delete_response
        self.delete_calls: list[list[dict[str, str]]] = []

    async def __aenter__(self) -> "_FakeS3Client":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def get_paginator(self, _operation: str) -> "_FakeS3Client":
        return self

    async def paginate(self, **_: object) -> AsyncIterator[dict[str, Any]]:
        yield {"Contents": [{"Key": "t/a"}, {"Key": "t/b"}]}

    async def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        self.delete_calls.append(Delete["Objects"])
        return self._delete_response


def _store(monkeypatch: pytest.MonkeyPatch, client: _FakeS3Client) -> S3BlobStore:
    store = S3BlobStore(
        bucket="b",
        endpoint_url="http://localhost:9000",
        access_key="a",
        secret_key="s",  # noqa: S106 -- test fixture
    )
    monkeypatch.setattr(store._session, "client", lambda *_a, **_k: client)
    return store


@pytest.mark.asyncio
async def test_a_refused_delete_is_an_error_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the answer was never read, so the retention sweep marked a manuscript
    purged while its source file was still in the bucket."""
    client = _FakeS3Client({"Errors": [{"Key": "t/a", "Code": "AccessDenied"}]})

    with pytest.raises(EdittaError, match="1 object"):
        await _store(monkeypatch, client).delete_prefix("t/")


@pytest.mark.asyncio
async def test_a_clean_delete_removes_every_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeS3Client({"Deleted": [{"Key": "t/a"}, {"Key": "t/b"}]})

    await _store(monkeypatch, client).delete_prefix("t/")

    assert client.delete_calls == [[{"Key": "t/a"}, {"Key": "t/b"}]]
