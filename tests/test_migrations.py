"""Migration 0003 builds SQL from an environment value: check its quoting without a DB."""

import importlib.util
from pathlib import Path

import pytest

VERSIONS = Path(__file__).resolve().parent.parent / "infra" / "migrations" / "versions"


def _worker_role_sql(monkeypatch: pytest.MonkeyPatch, password: str) -> str:
    spec = importlib.util.spec_from_file_location("m0003", VERSIONS / "0003_worker_role.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executed: list[str] = []
    monkeypatch.setattr(module.op, "execute", executed.append, raising=False)
    monkeypatch.setenv("EDITTA_WORKER_DB_PASSWORD", password)
    module.upgrade()
    return executed[0]


def test_a_quote_in_the_worker_password_does_not_end_the_sql_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "PASSWORD 'ab''cd' BYPASSRLS" in _worker_role_sql(monkeypatch, "ab'cd")
