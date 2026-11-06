"""BlobStore port + S3-compatible adapter. Works for AWS S3, R2, Hetzner, and MinIO
(dev) — they all speak the same S3 API, only endpoint_url changes.
"""

from collections.abc import AsyncIterator
from urllib.parse import quote

import aioboto3

from editta.domain.errors import EdittaError


class S3BlobStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        region: str = "auto",
    ) -> None:
        self._bucket = bucket
        self._session = aioboto3.Session()
        self._client_kwargs = {
            "endpoint_url": endpoint_url,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "region_name": region,
        }

    async def put(self, key: str, data: AsyncIterator[bytes]) -> None:
        body = b"".join([chunk async for chunk in data])
        async with self._session.client("s3", **self._client_kwargs) as client:
            await client.put_object(
                Bucket=self._bucket, Key=key, Body=body, ServerSideEncryption="AES256"
            )

    async def get(self, key: str) -> AsyncIterator[bytes]:
        async with self._session.client("s3", **self._client_kwargs) as client:
            response = await client.get_object(Bucket=self._bucket, Key=key)
            async with response["Body"] as stream:
                async for chunk in stream.iter_chunks():
                    yield chunk

    async def delete_prefix(self, prefix: str) -> None:
        async with self._session.client("s3", **self._client_kwargs) as client:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    response = await client.delete_objects(
                        Bucket=self._bucket, Delete={"Objects": keys}
                    )
                    # S3 answers 200 even when single keys were refused; the retention
                    # sweep must not mark content purged that is still there.
                    if errors := response.get("Errors"):
                        raise EdittaError(f"{len(errors)} object(s) could not be deleted")

    async def presigned_get_url(
        self, key: str, *, expires_in: int = 3600, download_name: str | None = None
    ) -> str:
        params = {"Bucket": self._bucket, "Key": key}
        if download_name is not None:
            params["ResponseContentDisposition"] = attachment_disposition(download_name)
        async with self._session.client("s3", **self._client_kwargs) as client:
            url = await client.generate_presigned_url(
                "get_object", Params=params, ExpiresIn=expires_in
            )
        return str(url)


def attachment_disposition(filename: str) -> str:
    """`Content-Disposition: attachment` value (RFC 6266 / 5987). `attachment` makes the
    browser save an uploaded file instead of rendering it; `filename*` carries the real
    (often Greek) name, the quoted ASCII `filename` is the fallback for old clients."""
    ascii_name = "".join(
        c if c.isascii() and c.isprintable() and c not in '"\\;' else "_" for c in filename
    )
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
