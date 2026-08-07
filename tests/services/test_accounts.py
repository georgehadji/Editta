from uuid import uuid4

import pytest
from itsdangerous import SignatureExpired

from editta.services.accounts import (
    MAGIC_LINK_MAX_AGE_SECONDS,
    AccountService,
    InvalidMagicLink,
    InvalidSession,
)


class _FakeTenantRepo:
    async def create(self, *, name: str) -> None:
        raise NotImplementedError


class _FakeUserRepo:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def get_by_email(self, tenant_id: object, email: str) -> None:
        return None

    async def create(
        self, *, tenant_id: object, email: str, role: str = "editor"
    ) -> dict[str, object]:
        record = {"tenant_id": tenant_id, "email": email, "role": role}
        self.created.append(record)
        return record


def _service() -> AccountService:
    return AccountService(
        tenants=_FakeTenantRepo(),  # type: ignore[arg-type]
        users=_FakeUserRepo(),  # type: ignore[arg-type]
        session_secret="test-secret",  # noqa: S106 -- test fixture, not a real secret
    )


def test_issue_then_verify_round_trips() -> None:
    service = _service()
    tenant_id = uuid4()

    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")
    verified_tenant_id, email = service.verify_magic_link_token(token)

    assert verified_tenant_id == tenant_id
    assert email == "editor@example.com"


def test_verify_rejects_tampered_token() -> None:
    service = _service()
    token = service.issue_magic_link_token(tenant_id=uuid4(), email="editor@example.com")

    with pytest.raises(InvalidMagicLink, match="invalid"):
        service.verify_magic_link_token(token + "tampered")


def test_verify_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    # itsdangerous owns the actual clock-based expiry math; we only need to verify
    # that AccountService translates SignatureExpired into our own InvalidMagicLink.
    service = _service()

    def _raise_expired(token: str, max_age: int) -> None:
        raise SignatureExpired("token is stale")

    monkeypatch.setattr(service._magic_link_serializer, "loads", _raise_expired)

    with pytest.raises(InvalidMagicLink, match="expired"):
        service.verify_magic_link_token("irrelevant-token")


def test_max_age_constant_is_fifteen_minutes() -> None:
    assert MAGIC_LINK_MAX_AGE_SECONDS == 900


def test_session_cookie_round_trips() -> None:
    service = _service()
    tenant_id, user_id = uuid4(), uuid4()

    token = service.issue_session_cookie(tenant_id=tenant_id, user_id=user_id, email="e@x.com")
    verified_tenant_id, verified_user_id, email = service.verify_session_cookie(token)

    assert (verified_tenant_id, verified_user_id, email) == (tenant_id, user_id, "e@x.com")


def test_session_cookie_rejects_tampered_token() -> None:
    service = _service()
    token = service.issue_session_cookie(tenant_id=uuid4(), user_id=uuid4(), email="e@x.com")

    with pytest.raises(InvalidSession, match="invalid"):
        service.verify_session_cookie(token + "tampered")
