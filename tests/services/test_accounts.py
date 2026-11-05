from uuid import UUID, uuid4

import pytest
from itsdangerous import SignatureExpired

from editta.domain.errors import PermissionDenied
from editta.services.accounts import (
    MAGIC_LINK_MAX_AGE_SECONDS,
    AccountService,
    AuthenticatedUser,
    InvalidMagicLink,
    InvalidSession,
)
from tests.services.fakes import (
    FakeTenantRepo,
    FakeUserRepo,
    RecordingEmailSender,
    make_account_service,
)

SECRET = "test-secret"  # noqa: S105 -- test fixture, not a real secret
BASE_URL = "https://editta.test"


def _service(users: FakeUserRepo | None = None) -> AccountService:
    return make_account_service(users or FakeUserRepo(), secret=SECRET)  # type: ignore[no-any-return]


async def _with_user(
    email: str = "editor@example.com", role: str = "editor"
) -> tuple[AccountService, FakeUserRepo, UUID]:
    users, tenant_id = FakeUserRepo(), uuid4()
    await users.create(tenant_id=tenant_id, email=email, role=role)
    return _service(users), users, tenant_id


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


# --- invite-only access ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_for_an_unknown_email_sends_nothing_and_creates_nothing() -> None:
    """Regression: any email used to be auto-created as an editor of any tenant."""
    users, sender, tenant_id = FakeUserRepo(), RecordingEmailSender(), uuid4()

    link = await _service(users).request_login(
        tenant_id=tenant_id, email="attacker@evil.test", sender=sender, base_url=BASE_URL
    )

    assert link is None
    assert sender.sent == []
    assert await users.get_by_email(tenant_id, "attacker@evil.test") is None


@pytest.mark.asyncio
async def test_login_emails_an_existing_user_and_the_link_signs_them_in() -> None:
    service, _, tenant_id = await _with_user()
    sender = RecordingEmailSender()

    await service.request_login(
        tenant_id=tenant_id, email=" Editor@Example.com ", sender=sender, base_url=BASE_URL
    )
    user = await service.complete_login(sender.last_token_for("editor@example.com"))

    assert sender.sent[0][1].startswith(f"{BASE_URL}/t/{tenant_id}/auth/click?token=")
    assert (user.tenant_id, user.email, user.role) == (tenant_id, "editor@example.com", "editor")


@pytest.mark.asyncio
async def test_a_valid_token_for_a_removed_account_is_rejected() -> None:
    service, users, tenant_id = await _with_user()
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")
    users.remove(tenant_id, "editor@example.com")

    with pytest.raises(InvalidMagicLink, match="no account"):
        await service.complete_login(token)


@pytest.mark.asyncio
async def test_a_token_for_another_tenant_is_rejected() -> None:
    service, _, tenant_id = await _with_user()
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")

    with pytest.raises(InvalidMagicLink, match="tenant"):
        await service.complete_login(token, expected_tenant_id=uuid4())


@pytest.mark.asyncio
async def test_removing_a_user_revokes_their_session() -> None:
    service, users, tenant_id = await _with_user()
    user = await service.complete_login(
        service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")
    )
    cookie = service.issue_session_cookie(
        tenant_id=tenant_id, user_id=user.user_id, email=user.email
    )
    assert (await service.authenticate_session(cookie)).user_id == user.user_id

    users.remove(tenant_id, "editor@example.com")

    with pytest.raises(InvalidSession, match="no longer active"):
        await service.authenticate_session(cookie)


@pytest.mark.asyncio
async def test_only_admins_can_invite() -> None:
    service, _, tenant_id = await _with_user()
    editor = AuthenticatedUser(tenant_id, uuid4(), "editor@example.com", "editor")
    admin = AuthenticatedUser(tenant_id, uuid4(), "boss@example.com", "admin")

    with pytest.raises(PermissionDenied):
        await service.invite_user(actor=editor, email="new@example.com")
    invited = await service.invite_user(actor=admin, email="New@Example.com")

    assert (invited.tenant_id, invited.email, invited.role) == (
        tenant_id,
        "new@example.com",
        "editor",
    )


@pytest.mark.asyncio
async def test_bootstrap_creates_a_tenant_with_its_first_admin() -> None:
    users = FakeUserRepo()
    service = make_account_service(users, secret=SECRET, tenants=FakeTenantRepo())

    admin = await service.bootstrap_tenant(name="Εκδόσεις Πόλις", admin_email="Owner@Polis.gr")

    assert admin.role == "admin" and admin.email == "owner@polis.gr"
    assert await users.get_by_email(admin.tenant_id, "owner@polis.gr") is not None
