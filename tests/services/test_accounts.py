from datetime import UTC, datetime, timedelta
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
from tests.services.fakes import FakeTenantRepo, FakeUserRepo, make_account_service

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


async def test_issue_then_open_round_trips() -> None:
    service, _, tenant_id = await _with_user()

    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")
    user = await service.complete_login(token, consume=False)

    assert (user.tenant_id, user.email) == (tenant_id, "editor@example.com")


async def test_a_tampered_token_is_rejected() -> None:
    service = _service()
    token = service.issue_magic_link_token(tenant_id=uuid4(), email="editor@example.com")

    with pytest.raises(InvalidMagicLink, match="invalid"):
        await service.complete_login(token + "tampered", consume=False)


async def test_an_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # itsdangerous owns the actual clock-based expiry math; we only need to verify
    # that AccountService translates SignatureExpired into our own InvalidMagicLink.
    service = _service()

    def _raise_expired(token: str, max_age: int, **_: object) -> None:
        raise SignatureExpired("token is stale")

    monkeypatch.setattr(service._magic_link_serializer, "loads", _raise_expired)

    with pytest.raises(InvalidMagicLink, match="expired"):
        await service.complete_login("irrelevant-token", consume=False)


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
async def test_login_for_an_unknown_email_yields_nothing_and_creates_nothing() -> None:
    """Regression: any email used to be auto-created as an editor of any tenant."""
    users, tenant_id = FakeUserRepo(), uuid4()

    login = await _service(users).request_login(
        tenant_id=tenant_id, email="attacker@example.org", base_url=BASE_URL
    )

    assert login is None
    assert await users.get_by_email(tenant_id, "attacker@example.org") is None


@pytest.mark.asyncio
async def test_login_link_for_an_existing_user_signs_them_in() -> None:
    service, _, tenant_id = await _with_user()

    login = await service.request_login(
        tenant_id=tenant_id, email=" Editor@Example.com ", base_url=BASE_URL
    )
    assert login is not None
    user = await service.complete_login(login.url.split("token=", 1)[1])

    assert login.to_email == "editor@example.com"
    assert login.url.startswith(f"{BASE_URL}/t/{tenant_id}/auth/click?token=")
    assert (user.tenant_id, user.email, user.role) == (tenant_id, "editor@example.com", "editor")


def test_rate_limit_subject_requires_a_validly_signed_cookie() -> None:
    from editta.services.accounts import session_rate_limit_subject

    service = _service()
    user_id = uuid4()
    cookie = service.issue_session_cookie(tenant_id=uuid4(), user_id=user_id, email="e@x.com")

    assert session_rate_limit_subject(cookie, SECRET) == str(user_id)
    assert session_rate_limit_subject("made-up-cookie", SECRET) is None
    assert session_rate_limit_subject(cookie, "another-secret") is None


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


# --- sign-in links are single-use -----------------------------------------------------


@pytest.mark.asyncio
async def test_a_sign_in_link_works_only_once() -> None:
    """Regression: the link was a stateless token, so a copy from a proxy log or a
    forwarded email signed in again until it expired."""
    service, _, tenant_id = await _with_user()
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")

    await service.complete_login(token)

    with pytest.raises(InvalidMagicLink, match="already used"):
        await service.complete_login(token)


@pytest.mark.asyncio
async def test_checking_a_link_does_not_spend_it() -> None:
    service, _, tenant_id = await _with_user()
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")

    await service.complete_login(token, consume=False)
    await service.complete_login(token, consume=False)
    await service.complete_login(token)  # the confirming request spends it

    with pytest.raises(InvalidMagicLink, match="already used"):
        await service.complete_login(token, consume=False)


@pytest.mark.asyncio
async def test_a_link_issued_after_the_previous_sign_in_works() -> None:
    service, users, tenant_id = await _with_user()
    user = await users.get_by_email(tenant_id, "editor@example.com")
    user.last_login_at = datetime.now(UTC) - timedelta(minutes=5)  # signed in earlier
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")

    assert (await service.complete_login(token)).email == "editor@example.com"


@pytest.mark.asyncio
async def test_a_link_issued_before_the_previous_sign_in_is_refused() -> None:
    service, users, tenant_id = await _with_user()
    token = service.issue_magic_link_token(tenant_id=tenant_id, email="editor@example.com")
    user = await users.get_by_email(tenant_id, "editor@example.com")
    user.last_login_at = datetime.now(UTC) + timedelta(minutes=1)  # signed in since

    with pytest.raises(InvalidMagicLink, match="already used"):
        await service.complete_login(token)
