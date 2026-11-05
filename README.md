# Editta

AI editorial assistant for publishing houses. Analyzes essay/book manuscripts and produces a
structured editorial report (language/style, structure/argument, metadata/positioning,
integrity-light) to support human editors. Never renders accept/reject verdicts, quality scores,
AI-detection, or plagiarism results.

Architecture and full build plan: [docs/PLAN.md](docs/PLAN.md).

## Dev setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
cp .env.example .env   # fill in real values (keeps EDITTA_ENVIRONMENT=development)
docker compose -f infra/docker-compose.yml up -d
alembic upgrade head
pytest
```

Integration tests (real Postgres, real migrations, RLS under production-like roles) run
when Docker is available, or against any Postgres you can `CREATE DATABASE` on:
`EDITTA_TEST_DATABASE_URL=postgresql+asyncpg://postgres@localhost:5432/postgres pytest`.

## Accounts (invite-only)

Nobody can sign in until an account exists. Create a tenant and its first admin from the
command line; the admin then invites others (`POST /auth/invite`):

```bash
editta create-tenant --name "Εκδόσεις Πόλις" --admin-email owner@polis.gr
editta add-user --tenant <tenant-id> --email editor@polis.gr --role editor   # operator use
```

Outside `EDITTA_ENVIRONMENT=development`, SMTP must be configured: sign-in links are
only ever emailed, never logged or shown on the page.

## Live-API smoke evals

Before the first paid report and after every prompt change: `python -m evals.run` (see
[evals/README.md](evals/README.md)). Costs real money; not part of CI.
