"""/healthz and /readyz: unauthenticated, no detail, and /healthz never touches the database."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from editta.api.deps import get_uow
from editta.api.main import app


class _Database:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def ping(self) -> None:
        if self.error:
            raise self.error


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_healthz_is_up_without_touching_the_database(client: TestClient) -> None:
    def unreachable() -> None:
        raise AssertionError("liveness must not need the database")

    app.dependency_overrides[get_uow] = unreachable

    response = client.get("/healthz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_readyz_is_ok_when_the_database_answers(client: TestClient) -> None:
    app.dependency_overrides[get_uow] = lambda: _Database()

    response = client.get("/readyz")

    assert (response.status_code, response.json()) == (200, {"status": "ok"})


def test_readyz_is_503_and_leaks_nothing_when_the_database_is_down(client: TestClient) -> None:
    app.dependency_overrides[get_uow] = lambda: _Database(
        ConnectionRefusedError("postgres://user:secret@db/editta")
    )

    response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "secret" not in response.text
