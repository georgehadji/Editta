"""S3BlobStore.presigned_get_url against a real S3 server, in process (moto).

The unit tests fake the client, so nothing proved that the signed `ResponseContentDisposition`
reaches the server and comes back as the download name. That is what the author's browser
saves the commented manuscript as. No Docker needed: moto serves the S3 API over HTTP.
It does not check signatures, so this proves the parameter is honoured, not that a given
store accepts the signature; MinIO no longer publishes a public image to test that against.
"""

import asyncio
import socket
import urllib.request
from collections.abc import AsyncIterator, Iterator

import aioboto3
import pytest
from moto.server import ThreadedMotoServer

from editta.adapters.storage.s3 import S3BlobStore, attachment_disposition

_BUCKET = "editta-test"
_KEYS = {"access_key": "test", "secret_key": "test"}  # noqa: S105 -- moto accepts anything


def _get(url: str) -> tuple[int, str | None, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 -- local test server
        return response.status, response.headers.get("Content-Disposition"), response.read()


@pytest.fixture(scope="module")
def s3_endpoint() -> Iterator[str]:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@pytest.mark.asyncio
async def test_a_presigned_link_downloads_the_object_under_the_forced_name(
    s3_endpoint: str,
) -> None:
    async with aioboto3.Session().client(
        "s3",
        endpoint_url=s3_endpoint,
        aws_access_key_id=_KEYS["access_key"],
        aws_secret_access_key=_KEYS["secret_key"],
        region_name="us-east-1",
    ) as client:
        await client.create_bucket(Bucket=_BUCKET)
    store = S3BlobStore(bucket=_BUCKET, endpoint_url=s3_endpoint, **_KEYS)

    async def body() -> AsyncIterator[bytes]:
        yield b"the commented manuscript"

    key = "jobs/j/annotated_manuscript.docx"
    await store.put(key, body())
    name = "Το βιβλίο-editta-comments.docx"

    url = await store.presigned_get_url(key, download_name=name)
    status, disposition, content = await asyncio.to_thread(_get, url)

    assert status == 200
    assert content == b"the commented manuscript"
    assert disposition == attachment_disposition(name)
