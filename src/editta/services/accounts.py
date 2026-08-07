"""Tenants, users, magic-link auth token issuance/verification.

Magic-link email delivery needs a transactional-email provider (SES/Postmark/etc.)
that PLAN.md's stack table doesn't pin down yet. This module issues and verifies the
signed link token; wiring an actual send is a follow-up once a provider is chosen.
"""

from uuid import UUID

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from editta.adapters.persistence.repositories import TenantRepository, UserRepository
from editta.adapters.persistence.tables import Tenant, User
from editta.domain.errors import EdittaError

MAGIC_LINK_MAX_AGE_SECONDS = 900
SESSION_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
_MAGIC_LINK_SALT = "magic-link"
_SESSION_SALT = "session-cookie"


class InvalidMagicLink(EdittaError):
    pass


class InvalidSession(EdittaError):
    pass


class AccountService:
    def __init__(
        self, *, tenants: TenantRepository, users: UserRepository, session_secret: str
    ) -> None:
        self._tenants = tenants
        self._users = users
        self._magic_link_serializer = URLSafeTimedSerializer(session_secret, salt=_MAGIC_LINK_SALT)
        self._session_serializer = URLSafeTimedSerializer(session_secret, salt=_SESSION_SALT)

    async def create_tenant(self, name: str) -> Tenant:
        return await self._tenants.create(name=name)

    async def invite_user(self, tenant_id: UUID, email: str, role: str = "editor") -> User:
        existing = await self._users.get_by_email(tenant_id, email)
        if existing is not None:
            return existing
        return await self._users.create(tenant_id=tenant_id, email=email, role=role)

    def issue_magic_link_token(self, *, tenant_id: UUID, email: str) -> str:
        payload = {"tenant_id": str(tenant_id), "email": email}
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
