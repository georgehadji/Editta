"""Tenants, users and magic-link authentication — invite-only.

Access model
------------
A user can sign in to a tenant only if an account already exists for that email in
that tenant. Accounts are created by a tenant admin (`invite_user`) or, for a tenant's
first admin, by `bootstrap_tenant` (the `editta create-tenant` CLI). Signing in never
creates an account. The previous behaviour — any email that asked for a link was made
an editor of any tenant, and the JSON API returned the login token in its response —
let anyone who knew a tenant's id read its unpublished manuscripts.

  * `request_login` emails a link only to an existing user and behaves identically
    otherwise, so the endpoint does not reveal which emails have accounts.
  * `complete_login` verifies the signed token and requires the account to still exist.
  * `authenticate_session` re-checks the account on every request: removing a user
    revokes their sessions instead of leaving a 30-day cookie valid.

Every user lookup runs inside a tenant-scoped transaction (`UserStore`), so these
pre-authentication paths are subject to row-level security like everything else.
"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from editta.adapters.persistence.repositories import TenantRepository, UserRepository
from editta.domain.errors import EdittaError, PermissionDenied
from editta.domain.ports import EmailSender

MAGIC_LINK_MAX_AGE_SECONDS = 900
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
_MAGIC_LINK_SALT = "magic-link"
_SESSION_SALT = "session-cookie"

ROLES = frozenset({"admin", "editor"})

# Opens one tenant-scoped (RLS) transaction with a UserRepository bound to it.
UserStore = Callable[[UUID], AbstractAsyncContextManager[UserRepository]]
# Opens one transaction with a TenantRepository (the tenants table has no RLS).
TenantStore = Callable[[], AbstractAsyncContextManager[TenantRepository]]


class InvalidMagicLink(EdittaError):
    pass


class InvalidSession(EdittaError):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    tenant_id: UUID
    user_id: UUID
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def normalize_email(email: str) -> str:
    return email.strip().lower()


def login_link(base_url: str, tenant_id: UUID, token: str) -> str:
    return f"{base_url.rstrip('/')}/t/{tenant_id}/auth/click?token={token}"


class AccountService:
    def __init__(
        self,
        *,
        users: UserStore,
        session_secret: str,
        tenants: TenantStore | None = None,
    ) -> None:
        self._users = users
        self._tenants = tenants
        self._magic_link_serializer = URLSafeTimedSerializer(session_secret, salt=_MAGIC_LINK_SALT)
        self._session_serializer = URLSafeTimedSerializer(session_secret, salt=_SESSION_SALT)

    # --- provisioning ---------------------------------------------------------------

    async def bootstrap_tenant(self, *, name: str, admin_email: str) -> AuthenticatedUser:
        """Create a tenant and its first admin. Operator-only (CLI); never exposed
        over HTTP."""
        if self._tenants is None:
            raise EdittaError("tenant provisioning is not configured")
        async with self._tenants() as tenants:
            tenant = await tenants.create(name=name)
            tenant_id = tenant.id
        return await self.provision_user(tenant_id=tenant_id, email=admin_email, role="admin")

    async def invite_user(
        self, *, actor: AuthenticatedUser, email: str, role: str = "editor"
    ) -> AuthenticatedUser:
        """Admins add users to their own tenant. Idempotent for an existing email."""
        if not actor.is_admin:
            raise PermissionDenied("only a tenant admin can invite users")
        return await self.provision_user(tenant_id=actor.tenant_id, email=email, role=role)

    async def provision_user(
        self, *, tenant_id: UUID, email: str, role: str = "editor"
    ) -> AuthenticatedUser:
        """Add a user without an authorization check — for `invite_user` after its check
        and for operators (CLI). Never call it from an HTTP route directly."""
        if role not in ROLES:
            raise EdittaError(f"unknown role: {role!r}")
        email = normalize_email(email)
        async with self._users(tenant_id) as users:
            user = await users.get_by_email(tenant_id, email)
            if user is None:
                user = await users.create(tenant_id=tenant_id, email=email, role=role)
            return AuthenticatedUser(tenant_id, user.id, user.email, user.role)

    # --- login ----------------------------------------------------------------------

    async def request_login(
        self, *, tenant_id: UUID, email: str, sender: EmailSender, base_url: str
    ) -> str | None:
        """Email a sign-in link if this email has an account in this tenant.

        Returns the link (for the dev console sender only — callers must never put it in
        an HTTP response) or None. Callers respond identically in both cases.
        """
        email = normalize_email(email)
        async with self._users(tenant_id) as users:
            user = await users.get_by_email(tenant_id, email)
        if user is None:
            return None
        token = str(
            self._magic_link_serializer.dumps({"tenant_id": str(tenant_id), "email": email})
        )
        link = login_link(base_url, tenant_id, token)
        await sender.send_magic_link(to_email=email, link_url=link)
        return link

    async def complete_login(
        self, token: str, *, expected_tenant_id: UUID | None = None
    ) -> AuthenticatedUser:
        tenant_id, email = self.verify_magic_link_token(token)
        if expected_tenant_id is not None and tenant_id != expected_tenant_id:
            raise InvalidMagicLink("magic link does not match this tenant")
        async with self._users(tenant_id) as users:
            user = await users.get_by_email(tenant_id, email)
        if user is None:
            raise InvalidMagicLink("no account for this email in this tenant")
        return AuthenticatedUser(tenant_id, user.id, user.email, user.role)

    async def authenticate_session(self, cookie: str) -> AuthenticatedUser:
        tenant_id, user_id, email = self.verify_session_cookie(cookie)
        async with self._users(tenant_id) as users:
            user = await users.get_by_id(tenant_id, user_id)
        if user is None or user.email != email:
            raise InvalidSession("account no longer active")
        return AuthenticatedUser(tenant_id, user.id, user.email, user.role)

    # --- signed tokens (pure) -------------------------------------------------------

    def issue_magic_link_token(self, *, tenant_id: UUID, email: str) -> str:
        payload = {"tenant_id": str(tenant_id), "email": normalize_email(email)}
        return str(self._magic_link_serializer.dumps(payload))

    def verify_magic_link_token(self, token: str) -> tuple[UUID, str]:
        try:
            payload = self._magic_link_serializer.loads(token, max_age=MAGIC_LINK_MAX_AGE_SECONDS)
        except SignatureExpired as exc:
            raise InvalidMagicLink("magic link has expired") from exc
        except BadSignature as exc:
            raise InvalidMagicLink("magic link is invalid") from exc
        return UUID(payload["tenant_id"]), str(payload["email"])

    def issue_session_cookie(self, *, tenant_id: UUID, user_id: UUID, email: str) -> str:
        payload = {"tenant_id": str(tenant_id), "user_id": str(user_id), "email": email}
        return str(self._session_serializer.dumps(payload))

    def verify_session_cookie(self, token: str) -> tuple[UUID, UUID, str]:
        try:
            payload = self._session_serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
        except SignatureExpired as exc:
            raise InvalidSession("session has expired") from exc
        except BadSignature as exc:
            raise InvalidSession("session is invalid") from exc
        return UUID(payload["tenant_id"]), UUID(payload["user_id"]), str(payload["email"])
