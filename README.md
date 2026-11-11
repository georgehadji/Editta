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

## LLM provider and models

Hard passes (language, structure) run on Claude Opus 5.5, integrity and synthesis on
Sonnet 5.5, metadata on Haiku 5.5 (`model` on each class in `src/editta/passes/`).
Set `EDITTA_OPENROUTER_API_KEY` to send these calls through OpenRouter, or only
`EDITTA_ANTHROPIC_API_KEY` to call Anthropic directly. With both set, OpenRouter serves
the calls and the Anthropic key is used only for free, exact token counts.

On OpenRouter each pass also names fallback models (`fallback_models`), tried in order
when its model errors: Gemini 4 Argon for language, GPT-6.1-sol for structure and
integrity, Gemini 3.8 Flash for metadata, GPT-6.1-terra for synthesis. Calls are streamed
and routed only to zero-data-retention endpoints. The budget projection prices each
pass's primary model, so a job that falls back can cost more than projected.

## Accounts (invite-only)

Nobody can sign in until an account exists. Create a tenant and its first admin from the
command line; the admin then invites others (`POST /auth/invite`):

```bash
editta create-tenant --name "Εκδόσεις Πόλις" --admin-email owner@polis.gr
editta add-user --tenant <tenant-id> --email editor@polis.gr --role editor   # operator use
```

Outside `EDITTA_ENVIRONMENT=development`, SMTP must be configured: sign-in links are
only ever emailed, never logged or shown on the page.

## Billing (Stripe)

Point a Stripe webhook endpoint at `POST /billing/webhook` and enable these events:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`

Credits are granted only once the payment is settled. A checkout paid by a delayed method
(SEPA debit, bank transfer) completes as `unpaid`; the credits arrive with
`async_payment_succeeded`. Without that second event such customers are never credited.

## Behind a reverse proxy

- **Request size.** The app refuses bodies over 26 MB with 413. Set the same cap at the
  proxy so oversized uploads never reach it (nginx: `client_max_body_size 26m;`).
- **Access logs.** Sign-in links carry a live token (`/t/<tenant>/auth/click?token=…`,
  valid 15 minutes). The app redacts it in uvicorn's access log, but the proxy's own log
  must not record query strings for that path: log `$uri` instead of `$request`, or
  `access_log off;` on that location.

## Running the worker and the retention sweep

The web app only queues work. Two more processes must run, from the same environment as
the app (same `.env`, plus `EDITTA_WORKER_DATABASE_URL`: the `BYPASSRLS` role from
migrations 0002-0003; both refuse to start on a role without it):

- **The worker** runs analyses. Keep it running as a service; on SIGTERM it finishes the
  job in hand before exiting.

  ```bash
  python -m editta.worker.main
  ```

  Without it, uploads stay queued forever.

- **The retention sweep** deletes manuscript content past its retention date (GDPR). It
  runs once and exits, so schedule it daily. It prints `retention sweep: N purged, M failed`
  and exits 1 if any manuscript failed, so let the scheduler alert on a non-zero exit; the
  next run retries what failed.

  ```bash
  python -m editta.worker.retention
  ```

  As a systemd timer: an `editta-retention.service` (`Type=oneshot`, the command above) and

  ```ini
  # editta-retention.timer
  [Timer]
  OnCalendar=daily
  Persistent=true

  [Install]
  WantedBy=timers.target
  ```

  or as cron: `15 3 * * * cd /srv/editta && .venv/bin/python -m editta.worker.retention`.

## Scaling workers

Each worker process runs one job at a time. To process jobs in parallel, run more worker
processes; they claim jobs with `FOR UPDATE SKIP LOCKED`, so they never take the same job.

## Live-API smoke evals

Before the first paid report and after every prompt change: `python -m evals.run` (see
[evals/README.md](evals/README.md)). Costs real money; not part of CI.
