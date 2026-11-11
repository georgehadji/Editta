"""How the rate limiter is built from the configured store, and request ids."""

import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from editta.api.middleware import RequestContextMiddleware, build_limiter
from editta.observability import configure_logging


@pytest.fixture
def client() -> Iterator[TestClient]:
    async def route(request: Request) -> PlainTextResponse:
        structlog.get_logger().info("inside")
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", route)])
    app.add_middleware(RequestContextMiddleware)
    configure_logging("api")
    yield TestClient(app)
    structlog.reset_defaults()


def _inside_line(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    (line,) = [x for x in lines if x["event"] == "inside"]
    return line


def test_every_response_carries_an_id_that_is_also_on_the_logs(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = client.get("/")

    request_id = response.headers["X-Request-ID"]
    assert len(request_id) == 32
    assert _inside_line(capsys)["request_id"] == request_id


def test_a_plain_id_from_the_caller_is_kept(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    response = client.get("/", headers={"X-Request-ID": "trace-0001.abc"})

    assert response.headers["X-Request-ID"] == "trace-0001.abc"
    assert _inside_line(capsys)["request_id"] == "trace-0001.abc"


@pytest.mark.parametrize("supplied", ["short", "has spaces in it!", "x" * 65, 'quote"d-id-123'])
def test_an_id_that_is_not_plain_is_replaced(client: TestClient, supplied: str) -> None:
    response = client.get("/", headers={"X-Request-ID": supplied})

    assert response.headers["X-Request-ID"] != supplied
    assert len(response.headers["X-Request-ID"]) == 32


def test_without_a_store_counters_stay_in_the_process() -> None:
    limiter = build_limiter(None)

    assert limiter._storage_uri is None
    assert limiter._swallow_errors is False


def test_with_a_shared_store_an_outage_does_not_fail_requests() -> None:
    limiter = build_limiter("memory://")

    assert limiter._storage_uri == "memory://"
    assert limiter._swallow_errors is True
