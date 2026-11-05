"""`editta` command-line entrypoint.

  editta analyze manuscript.(docx|pdf) -o report.docx   run the pipeline locally
  editta create-tenant --name NAME --admin-email EMAIL  provision a tenant + first admin
  editta add-user --tenant ID --email EMAIL [--role R]  operator: add/restore a user

Access is invite-only (services/accounts.py): nobody can sign in to a tenant until an
account exists, so a new tenant's first admin is created here, by an operator, never
over HTTP. Admins then invite others via POST /auth/invite.
"""

import argparse
import asyncio
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from editta.adapters.export.docx_builder import ReportDocxBuilder
from editta.adapters.llm.client import AnthropicLLMClient
from editta.adapters.persistence.repositories import TenantRepository, UserRepository
from editta.adapters.persistence.uow import UnitOfWork
from editta.domain.errors import EdittaError
from editta.services.accounts import AccountService
from editta.services.pipeline import run_pipeline

DEFAULT_COST_CEILING_USD = 6.0


def main() -> None:
    parser = argparse.ArgumentParser(prog="editta")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze = subparsers.add_parser("analyze", help="run editorial analysis on a manuscript")
    analyze.add_argument("manuscript", type=Path, help="path to a .docx or .pdf manuscript")
    analyze.add_argument(
        "-o", "--output", type=Path, required=True, help="path to write the .docx report"
    )
    analyze.add_argument(
        "--cost-ceiling",
        type=float,
        default=DEFAULT_COST_CEILING_USD,
        help="abort if projected cost exceeds this many USD",
    )

    create_tenant = subparsers.add_parser(
        "create-tenant", help="create a tenant and its first admin user"
    )
    create_tenant.add_argument("--name", required=True)
    create_tenant.add_argument("--admin-email", required=True)

    add_user = subparsers.add_parser("add-user", help="operator: add a user to a tenant")
    add_user.add_argument("--tenant", required=True, type=UUID)
    add_user.add_argument("--email", required=True)
    add_user.add_argument("--role", choices=("admin", "editor"), default="editor")

    args = parser.parse_args()
    commands = {
        "analyze": _analyze,
        "create-tenant": _create_tenant,
        "add-user": _add_user,
    }
    try:
        commands[args.command](args)
    except EdittaError as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"{name} is not set", file=sys.stderr)
        raise SystemExit(1)
    return value


def _analyze(args: argparse.Namespace) -> None:
    api_key = _require_env("EDITTA_ANTHROPIC_API_KEY")
    try:
        manuscript_bytes = args.manuscript.read_bytes()
    except OSError as exc:
        print(f"failed to read {args.manuscript}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    report = asyncio.run(
        run_pipeline(
            manuscript_bytes=manuscript_bytes,
            filename=args.manuscript.name,
            llm=AnthropicLLMClient(api_key=api_key),
            cost_ceiling_usd=args.cost_ceiling,
        )
    )
    args.output.write_bytes(ReportDocxBuilder(report).build())
    pass_findings = sum(len(r.findings) for r in report.pass_results)
    reported = sum(len(section.findings) for section in report.synthesis.sections)
    print(
        f"report written to {args.output} "
        f"({reported} findings reported, from {pass_findings} anchored pass findings)"
    )


def _create_tenant(args: argparse.Namespace) -> None:
    admin = asyncio.run(
        _with_accounts(
            lambda accounts: accounts.bootstrap_tenant(name=args.name, admin_email=args.admin_email)
        )
    )
    print(f"tenant {admin.tenant_id} created")
    print(f"admin {admin.email} can sign in at /t/{admin.tenant_id}/login")


def _add_user(args: argparse.Namespace) -> None:
    user = asyncio.run(
        _with_accounts(
            lambda accounts: accounts.provision_user(
                tenant_id=args.tenant, email=args.email, role=args.role
            )
        )
    )
    print(f"{user.email} added to tenant {user.tenant_id} as {user.role}")


async def _with_accounts[R](action: Callable[[AccountService], Awaitable[R]]) -> R:
    """Wire an AccountService to the database for one operator command."""

    engine = create_async_engine(_require_env("EDITTA_DATABASE_URL"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    uow = UnitOfWork(sessions)

    @asynccontextmanager
    async def users(tenant_id: UUID) -> AsyncIterator[UserRepository]:
        async with uow.begin(tenant_id) as session:
            yield UserRepository(session)

    @asynccontextmanager
    async def tenants() -> AsyncIterator[TenantRepository]:
        async with sessions() as session, session.begin():
            yield TenantRepository(session)

    accounts = AccountService(
        users=users,
        tenants=tenants,
        # Operator commands issue no tokens; the secret only has to be non-empty.
        session_secret=os.environ.get("EDITTA_SESSION_SECRET", "unused-by-operator-commands"),
    )
    try:
        return await action(accounts)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    main()
