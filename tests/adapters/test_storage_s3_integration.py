"""S3BlobStore.presigned_get_url against a real S3-compatible server (MinIO).

The unit tests fake the client, so nothing proved that the signed `ResponseContentDisposition`
survives signing and comes back as the download name. That is what the author's browser
saves the commented manuscript as. Skips cleanly without a Docker daemon.
"""

import asyncio
import subprocess
import time
import urllib.request
from collections.abc import AsyncIterator, Iterator

import aioboto3
import pytest

from editta.adapters.storage.s3 import S3BlobStore, attachment_disposition

_USER = "editta"
_PASSWORD = "editta-test-only"  # noqa: S105 -- throwaway local test server
_BUCKET = "editta-test"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)  # noqa: S607
    except Exception:
        return False
    return True


pytestmark = pytest.mark.skipif(not _docker_available(), reason="no Docker daemon is available")


def _get(url: str) -> tuple[int, str | None, bytes]:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 -- local test server
        return response.status, response.headers.get("Content-Disposition"), response.read()


@pytest.fixture(scope="module")
def minio_endpoint() -> Iterator[str]:
    from testcontainers.core.container import DockerContainer

    container = (
        DockerContainer("minio/minio:latest")
        .with_command("server /data")
        .with_env("MINIO_ROOT_USER", _USER)
        .with_env("MINIO_ROOT_PASSWORD", _PASSWORD)
        .with_exposed_ports(9000)
    )
    with container:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        deadline = time.monotonic() + 30
        while True:
            try:
                _get(f"{endpoint}/minio/health/live")
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield endpoint


@pytest.mark.asyncio
async def test_a_presigned_link_downloads_the_object_under_the_forced_name(
    minio_endpoint: str,
) -> None:
    async with aioboto3.Session().client(
        "s3",
        endpoint_url=minio_endpoint,
        aws_access_key_id=_USER,
        aws_secret_access_key=_PASSWORD,
        region_name="auto",
    ) as client:
        await client.create_bucket(Bucket=_BUCKET)
    store = S3BlobStore(
        bucket=_BUCKET, endpoint_url=minio_endpoint, access_key=_USER, secret_key=_PASSWORD
    )

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
