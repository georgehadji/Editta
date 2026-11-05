"""Oversized request bodies are refused with 413 while they stream in, before any route
or dependency runs (FastAPI would otherwise spool a multipart body to disk first)."""

from collections.abc import Iterator

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from editta.api.main import app as real_app
from editta.api.middleware import MAX_REQUEST_BYTES, BodySizeLimitMiddleware

MIB = 1024 * 1024
LIMIT = 4 * MIB


def _app(ran: list[int]) -> FastAPI:
    probe = FastAPI()
    probe.add_middleware(BodySizeLimitMiddleware, max_bytes=LIMIT)

    @probe.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        ran.append(len(body))
        return {"bytes": len(body)}

    return probe


def _chunks(count: int) -> Iterator[bytes]:
    for _ in range(count):
        yield b"x" * MIB


def test_declared_oversize_is_refused_before_the_app_runs() -> None:
    ran: list[int] = []
    with TestClient(_app(ran)) as client:
        response = client.post("/echo", content=b"x", headers={"content-length": str(5 * MIB)})

    assert response.status_code == 413
    assert ran == []


def test_chunked_body_without_content_length_is_cut_off() -> None:
    ran: list[int] = []
    with TestClient(_app(ran), raise_server_exceptions=False) as client:
        response = client.post("/echo", content=_chunks(10))  # 10 MiB, no length declared

    assert response.status_code == 413
    assert response.json()["detail"].startswith("request body exceeds")
    assert ran == []


def test_body_within_the_limit_passes_through() -> None:
    ran: list[int] = []
    with TestClient(_app(ran)) as client:
        response = client.post("/echo", content=_chunks(3))

    assert response.status_code == 200
    assert ran == [3 * MIB]


def test_real_app_refuses_an_oversized_anonymous_upload_before_auth() -> None:
    big = b"x" * (MAX_REQUEST_BYTES + 1)
    client = TestClient(real_app, raise_server_exceptions=False)

    response = client.post("/manuscripts", files={"file": ("a.docx", big)})

    assert response.status_code == 413  # not 401: nothing was parsed or authenticated
