# Architecture remediation plan

Fixes for the 12 findings (A-1 to A-12) of the architecture audit of 2026-11-11, plus the
open items left by PR #14. Every item says what changes, which design pattern carries it and
why that pattern fits this codebase, the files it touches, the tests that prove it, and when
it is done. Written for a solo maintainer: nothing here adds a framework, a DI container, a
message broker or a new deployable.

Evidence labels: **[VERIFIED]** was read in the code or config; **[CHECK]** is an assumption
the first step of the item must confirm before any code moves.

## 1. Ground rules from the existing architecture

The target is the architecture `docs/PLAN.md` §2 already promises: a modular monolith with a
pure domain, ports as `Protocol`s, adapters behind them, services as the imperative shell, and
composition roots that do all the wiring. The audit found the code drifting from that promise,
so the plan moves the code toward the docs, and fixes the docs only where the promise was
wrong or premature.

| Layer | Where | Paradigm to keep | Rule for the fixes |
|---|---|---|---|
| Domain | `src/editta/domain/` | Functional core: pure functions, frozen value objects, typed errors | No I/O, no outward imports. New ports and read models live here. Retryability is the error *class*, never message text. |
| Passes | `src/editta/passes/` | Strategy over a template (`passes/base.py`) | Depends on `domain` only. Untouched except for one log binding (item 2). |
| Services | `src/editta/services/` | Transaction scripts with injected collaborators | Import `domain` and `passes` only. Collaborators arrive as constructor or function arguments typed by a port. |
| Adapters | `src/editta/adapters/` | Anti-corruption layer; vendor shapes and errors stop here | Import `domain` only (one documented sibling edge: `export` → `parsing`). Return domain types, never ORM rows. |
| Delivery | `src/editta/api/`, `worker/`, `cli.py` | Composition roots | The only places that name concrete adapters. Routes receive ports through `Depends`. |
| Cross-cutting | new `src/editta/observability.py` | Processor pipeline (structlog) | Called once from each composition root, never from services. |

Existing idioms reused instead of new patterns: Decorator over a port (`_RecordFailedSpend`,
`jobs.py:72`), Repository + Unit of Work, fenced writes, error-class retry policy, the
"run once, exit non-zero, let the scheduler alert" shape of `worker/retention.py`.

Checks every PR must keep green, as in CI today: `ruff check src tests evals`, `mypy src`
(strict), `pytest` (coverage gate 80 %), bandit, pip-audit, gitleaks. Items touching
repositories or queue SQL also run the Postgres integration tests
(`EDITTA_TEST_DATABASE_URL=… pytest tests/adapters/test_persistence_integration.py`).

## 2. Findings → work items

| Item | Ledger | Module(s) | PR |
|---|---|---|---|
| 1 Bound every model call | A-6 | `adapters/llm`, `services/jobs`, `domain/errors` | PR 1 |
| 2 One logging policy, request and job context | A-3, A-4 (logs) | `observability.py`, `api`, `worker`, `services` | PR 2 |
| 3 Per-call LLM tracing | A-4 (tracing), A-5 (visibility) | `adapters/llm` | PR 2 |
| 4 Health, readiness, alertable ops check | A-4 (health, alerts), A-11 | `api/routes/health.py`, `worker/ops_check.py`, `infra/Dockerfile` | PR 3 |
| 5 Architecture contracts in CI | A-2 | `pyproject.toml`, CI | PR 4 |
| 6 Repository and parser ports | A-1, A-12 | `domain`, `adapters/persistence`, `services`, `api`, `worker` | PRs 5-8 |
| 7 Rate limits across processes | A-5 | `config`, `adapters/llm`, README | PR 1 (config), later (shared store) |
| 8 Smaller route and email fixes | A-8, A-9, A-10 | `api/routes`, `adapters/email` | PR 9 |
| 9 Close the PR #14 loose ends | O-1 to O-6 | infra, tests, docs | PR 10 |
| 10 Reconcile `docs/PLAN.md` | all `[FALSE]` claims | docs | with each PR |
| Accepted | A-7 | — | no change |

Why this order. Items 1 to 4 are cheap, touch behaviour that matters to the first paid report
(a hung call, a blind operator, a log leak) and do not depend on the big refactor. Item 5 comes
before item 6 so progress on item 6 is measurable: the contract starts with today's violations
listed and the list shrinks to zero. Items in the same PR touch the same files (see "Sequence and
PR slicing" below).

---

## Item 1: Bound every model call (A-6)

**Finding.** `AsyncAnthropic(api_key=api_key)` (`adapters/llm/client.py:82`) sets no timeout and
leaves retries at the SDK default. OpenRouter has one (`openrouter.py:162`). `JobRunner` bounds
parse and export (`jobs.py:221`, `:339`) but not `run_passes` or `run_synthesis`
(`jobs.py:245`, `:253`), and the worker heartbeat keeps extending the lease while they hang.

**Pattern.** *Timeout at two levels, failure classified by error class.* Per request, in the
adapter (anti-corruption layer: vendor timeouts become domain errors). Per job phase, in the
service (the `asyncio.wait_for` idiom the runner already uses for parse and export). Both map to
one retryable domain error so `decide_after_failure` (`domain/states.py`) treats them as it treats
any transient LLM failure: retry with backoff, then DEAD plus refund.

**Changes.**
- `domain/errors.py`: add `LLMTimeout(LLMError)`, retryable by class. **[CHECK]** how `LLMError`
  subclasses declare retryability today and follow that exact mechanism.
- `adapters/llm/client.py`: build the SDK client with an explicit `httpx.Timeout` (connect short,
  read long enough for a streamed book-length answer) and `max_retries` chosen so the SDK's own
  retries do not multiply the circuit breaker's. Translate the SDK's timeout exception into
  `LLMTimeout` next to the existing `APIStatusError` handling (`client.py:50`, `:152`).
- Move the timeout constants to one place, `adapters/llm/limits.py`, imported by `client.py` and
  `openrouter.py`, so the two adapters cannot drift apart again.
- `services/jobs.py`: add `ANALYSIS_TIMEOUT_SECONDS` and `SYNTHESIS_TIMEOUT_SECONDS`; wrap
  `run_passes` and `run_synthesis` in `asyncio.timeout(...)`, raising `LLMTimeout` on expiry.
  Completed passes are already checkpointed (`jobs.py:274-311`), so a retry resumes after them.
- `config.py`: expose the two job-level timeouts as settings with defaults. Size the defaults from
  the live smoke eval (item 9, O-6), not a guess: a timeout shorter than the slowest honest
  Opus 5.5 pass would kill good jobs.

**Files.** `domain/errors.py`, `adapters/llm/{client,openrouter,limits}.py`, `services/jobs.py`,
`config.py`, `.env.example`.

**Tests.**
- Adapter: a fake SDK call that never returns raises `LLMTimeout` within the configured bound.
- Service: `run_passes` replaced by an `asyncio.sleep(∞)` coroutine; `JobRunner.run` returns a
  retry outcome (state back to queued with backoff), and checkpoints written before the hang
  survive. Use `JobRunner` with the existing in-memory fakes (`tests/services/fakes.py`).
- Policy: `decide_after_failure(LLMTimeout(...))` retries, then dead-letters after the attempt cap.

**Done when.** No external call on the job path lacks a timeout. A test proves a hung pass ends the
job attempt instead of holding the lease. The live smoke eval has run once with the new bounds.

**Risk.** Too short a timeout turns slow honest jobs into dead jobs. Mitigation: defaults from
measured data, and the refund path already exists.

---

## Item 2: One logging policy, with request and job context (A-3, A-4)

**Finding.** Only the API installs `_scrub_manuscript_content` (`api/main.py:25,52`). The worker and
the retention sweep configure a bare `JSONRenderer` (`worker/main.py:191`, `worker/retention.py:71`),
yet the worker is where manuscript text flows. PLAN.md §5 promises no manuscript text in logs.
Correlation fields are partial: `jobs.py:357` binds `job_id` and `tenant_id`, `jobs.py:401` and the
worker bindings lack `tenant_id`, and no line carries `pass_name`.

**Pattern.** *Processor pipeline (structlog chain) with a single factory, plus context variables
for correlation.* One `configure_logging(service)` builds the chain; the composition roots call it
and nothing else configures structlog. Correlation ids are bound once at the edge
(`structlog.contextvars`) so inner code stops passing ids to loggers by hand.

**Changes.**
- New `src/editta/observability.py` with `configure_logging(service: str) -> None`. Chain:
  `merge_contextvars`, `add_log_level`, ISO timestamper, `service` field, `_scrub_manuscript_content`
  (moved here, unchanged behaviour), `JSONRenderer`. Move `_RedactTokens` here too and attach it
  from the API only (it is an uvicorn access-log concern).
- `api/main.py`, `worker/main.py`, `worker/retention.py`, `cli.py`: replace their structlog setup
  with `configure_logging("api" | "worker" | "retention" | "cli")`. The API stops configuring at
  import time in the middle of the module; it calls the function once, in the module that builds
  `app`.
- `api/middleware.py`: add `RequestContextMiddleware` (pure ASGI like the existing two): generate a
  request id (or accept a well-formed inbound `X-Request-ID`), bind it with `bind_contextvars`,
  echo it in the response header, clear on exit.
- `services/jobs.py`: bind `job_id`, `tenant_id`, `attempt` once at the top of `JobRunner.run`;
  `services/analysis.run_pass` binds `pass_name` around each call. Delete the hand-passed ids where
  they become redundant.
- Make the scrub stronger than a three-key blacklist: also drop any event value that is `bytes`
  or a `str` over a length cap (say 2 000 chars). **[CHECK]** no legitimate log field is that long;
  the cap is a backstop, not the primary guard (errors are already sanitized by `_error_message`,
  `jobs.py:134`).

**Files.** `observability.py` (new), `api/main.py`, `api/middleware.py`, `worker/main.py`,
`worker/retention.py`, `cli.py`, `services/jobs.py`, `services/analysis.py`.

**Tests.**
- `configure_logging` for each service name: render an event with a `text` key and a 5 000-char
  string value; assert both are absent from the output.
- A test that counts `structlog.configure(` in `src/` and expects exactly one (guards against a
  fourth place configuring it differently).
- Middleware: the response carries `X-Request-ID`; a log line emitted inside a route carries it.

**Done when.** `structlog.configure(` appears in exactly one file. A log line from the worker for a
pass carries `service`, `job_id`, `tenant_id`, `pass_name`. The worker cannot emit `text`.

**Risk.** Contextvars leak across tasks if not cleared. The middleware clears in `finally`; the
worker binds per job and clears at the end of `run`.

---

## Item 3: Per-call LLM tracing without a tracing stack (A-4, A-5 visibility)

**Finding.** Token usage is stored per pass (`jobs.py:283-296`), but latency, the model actually
served, the fallback taken and the outcome are not recorded anywhere. PLAN §7 promises tracing.

**Pattern.** *Decorator over the `LLMClient` port*, the same pattern as `_RecordFailedSpend`. An
`InstrumentedLLMClient` wraps any `LLMClient`, times the call and emits one structured log line
`llm.call` with `model`, `served_model`, `input_tokens`, `output_tokens`, `cache_read_tokens`,
`duration_ms`, `outcome` (`ok`, or the error class name). Because it is a decorator on the port it
works for Anthropic and OpenRouter alike and needs no vendor code.

**Why not OpenTelemetry now.** A solo operator with one worker gets what they need from structured
lines plus item 4's alert. OTel adds a collector, an exporter and a dependency to operate. The
trigger to adopt it is in "Switching triggers" below.

**Changes.**
- New `adapters/llm/instrumented.py` with `InstrumentedLLMClient`.
- `build_llm_client` wraps its result **[CHECK]** (its module path), so every consumer gets it and
  the composition roots do not change.
- Log numbers only; never prompt or output text.

**Tests.** Fake inner client: success logs one line with the measured fields; an `LLMError` logs
`outcome=<ClassName>` and re-raises unchanged; no field contains the prompt.

**Done when.** One `llm.call` line per model call, with latency, in the worker's output.

**Implemented as (PR 2), where it differs from the text above.**
- `_RedactTokens` stays in `api/main.py`: it is an uvicorn access-log filter, API-only, and
  `tests/api/test_access_log.py` imports it from there.
- The scrub replaces a too-long string or raw bytes with a fixed placeholder instead of dropping
  the key, so a reader still sees that a field was there.
- `InstrumentedLLMClient` is wired in `worker/main.py`, the composition root, not inside
  `build_llm_client`, so the factory keeps returning the concrete adapters its tests check.
- `cli.py` is not configured: it is the operator's own local run, and its stdout carries the
  report summary. The API does not bind `tenant_id` per request; request id only.
- Job context is bound in `JobRunner.run` (`job_id`, `tenant_id`, `attempt`) and in
  `Worker._process` (adds `worker_id`, and covers the heartbeat task); `pass_name` is bound
  around each model call in `services/analysis.py`.

---

## Item 4: Health, readiness and an alertable ops check (A-4, A-11)

**Finding.** No `/health` route, no Docker `HEALTHCHECK`, no DEAD-job alert; the Dockerfile has no
`USER`, so API and worker run as root.

**Pattern.** *Liveness vs readiness split; the "run once, exit non-zero" ops command.* This is the
shape `worker/retention.py` already has and README already tells operators to alert on.

**Changes.**
- `api/routes/health.py`: `GET /healthz` returns 200 with no dependencies (is the process up);
  `GET /readyz` runs `SELECT 1` through the existing `get_uow` dependency and returns 503 on
  failure (can it serve). Exempt both from rate limits and CSRF. Do not expose version or config.
- `worker/ops_check.py`: runs once, prints one line, exits 1 when either holds: any job in `dead`
  state created in the last N hours, or the oldest `queued` job is older than M minutes (nobody is
  claiming). Invoked as `python -m editta.worker.ops_check`, scheduled like the retention sweep.
  The queries are methods on `AnalysisJobRepository` (`count_dead_since`, `oldest_queued_age`),
  consumed through the port introduced in item 6.
- `infra/Dockerfile`: create a non-root user, `USER` it in both stages, add `HEALTHCHECK` on the API
  stage calling `/healthz` with Python's stdlib (no curl dependency). The worker stage keeps no
  `HEALTHCHECK`; its liveness signal is the queue heartbeat, checked by `ops_check`.
- README: document `ops_check` next to the retention sweep (same systemd timer / cron example).

**Tests.** Route tests for 200 and for 503 when the UoW raises; `ops_check` against the Postgres
integration fixture (a dead job → exit 1; a clean database → exit 0); build the image once locally
and run it as the non-root user.

**Done when.** `curl /readyz` reflects database state, `docker inspect` shows a health status, the
container runs as a non-root user, and a dead job makes the scheduled ops check fail.

**Risk.** A non-root user can break writes to a path the app assumes writable. **[CHECK]** the local
blob store path and temp usage in the image. S3 is the production store, so this should only affect
dev images.

---

## Item 5: Architecture contracts in CI (A-2)

**Finding.** Nothing enforces the dependency rule, so A-1 grew unguarded.

**Pattern.** *Fitness function: the architecture rule as an executable test.* Use import-linter,
which reads `pyproject.toml`, adds one dev dependency and one CI step.

**Contracts** (`[tool.importlinter]` in `pyproject.toml`):

| Contract | Type | Rule |
|---|---|---|
| Domain is pure | forbidden | `editta.domain` must not import `editta.services`, `adapters`, `api`, `worker`, `passes` |
| Passes are domain-only | forbidden | `editta.passes` must not import anything above `domain` |
| Adapters do not climb | forbidden | `editta.adapters` must not import `services`, `api`, `worker`, `cli` |
| Services stay in their layer | forbidden | `editta.services` must not import `editta.adapters` (initially with `ignore_imports` listing today's violations, below), `api`, `worker` |
| Routes use ports | forbidden | `editta.api.routes` must not import `editta.adapters` (initially with its own ignore list; `api/deps.py` is the composition root and is exempt) |
| Adapters are siblings | independence | `adapters.{llm,persistence,parsing,export,storage,queue,email}` independent, except the one documented edge `export` → `parsing` |

The ignore lists are exactly the edges found by the audit: `services/{jobs,uploads,accounts,reports,
retention,pipeline}` → adapters (`jobs.py:39-48`, `uploads.py:26-33`, `accounts.py:32`,
`reports.py:11`, `retention.py:27`, `pipeline.py:8`) and `api/routes/{web,billing,manuscripts}` →
adapters. Each item-6 PR deletes the lines it fixes; the contract fails if one is removed wrongly or
re-added. When the lists are empty the ignore blocks are deleted.

**Changes.** `pyproject.toml` (config + `import-linter` in the `dev` extra), `ci.yml` (one step
`lint-imports` after mypy), optional `pre-commit` hook.

**Tests.** The tool is the test. Prove it can fail: add a forbidden import on a scratch branch and
confirm CI goes red.

**Done when.** CI runs `lint-imports`, it passes today, and a new forbidden import fails the build.

---

## Item 6: Repository and parser ports (A-1, A-12)

The largest item, so it is sliced. It turns the `[FALSE]` claims at PLAN.md:49 ("file parsing,
storage, persistence, and export are adapters behind ports") and :123 ("swap storage, mock in
tests") into true ones.

**Finding in two parts.**
1. *Services import concrete adapters.* Six services import repositories, `parse_manuscript`,
   `validate_upload`/`sanitize_filename` or exporter classes. `jobs.py:124-127` instantiates the
   exporters at import time. The cost is visible in `tests/services/fakes.py:386-387,409-413`: seven
   `# type: ignore[arg-type]` to hand fakes to concretely typed code.
2. *ORM rows cross the boundary.* `repositories.py` imports its row classes from
   `adapters/persistence/tables.py` (line 14) and its methods return them (`-> Tenant`,
   `-> Manuscript`, `-> AnalysisJob`, …). **[CHECK]** confirm those names are the SQLAlchemy
   classes. If so, mutable session-bound objects reach services and routes, and ports cannot be
   honest until the return types are domain types.

**Pattern.** *Hexagonal ports with consumer-sized interfaces (interface segregation), and read
models as frozen value objects.* Each service needs only the methods it calls, declared in
`domain/ports.py`. Repositories keep their SQL and map rows to frozen domain read models at the
boundary (*data mapper*). The Unit of Work stays the transaction boundary and yields the
repositories. Composition roots build the concrete repositories; services never see them.

**Slices.**

**PR 5: read models and the ports for jobs.**
- Add frozen read models to `domain/models.py` for what services actually read: `ManuscriptRecord`,
  `JobRecord`, `ReportRecord`, `TenantRecord`, `UserRecord`. Keep only the fields used. **[CHECK]**
  with a grep of attribute reads in `services/` and `api/routes/` before choosing fields.
- Add protocols to `domain/ports.py`: `JobStore`, `ManuscriptStore`, `PassResultStore`,
  `ReportStore`, `CreditLedger`, each with only the methods `JobRunner` uses.
- Each repository method maps ORM → record at its return statement. The repository classes stay
  where they are and satisfy the protocols structurally.
- `JobRepositories` (`jobs.py:104`) is retyped with the protocols, and `JobUnitOfWork` follows.
  Remove the matching `type: ignore`s from the fakes.
- Shrink the `services → adapters` ignore list by the repository lines of `jobs.py`.

**PR 6: parser and exporter ports; the composition root owns the defaults.**
- `domain/ports.py`: add `ManuscriptParser` (`parse(data: bytes, title: str) -> Manuscript`, sync and
  CPU-bound like `ArtifactExporter`; the runner still calls it in a thread under a timeout).
- `validate_upload` and `sanitize_filename`: **[CHECK]** whether they are pure byte and string
  logic. If so, move them to a new `domain/uploads.py` (functional core, no port needed). If they
  import python-docx or pypdf, add an `UploadValidator` port instead.
- `JobRunner.__init__` takes `parser: ManuscriptParser` and a required `exporters`; drop
  `DEFAULT_EXPORTERS`. `worker/main.py` and `cli.py` construct the adapters and pass them in. This
  removes import-time instantiation (`jobs.py:124-127`).
- `services/pipeline.py` (the CLI path) takes the parser the same way.
- Delete the matching ignore lines.

**PR 7: the other services.** `uploads.py`, `accounts.py`, `reports.py`, `retention.py` retyped
against the protocols from PR 5 (extend `ManuscriptStore`; add `TenantStore`, `UserStore`,
`LoginStore` as needed). One small commit per service module; each ends with its ignore lines
deleted and `lint-imports` green.

**PR 8: routes and roots.** `api/routes/{web,billing,manuscripts}.py` stop importing repositories:
they receive services or ports through `Depends` providers in `api/deps.py` (the composition root).
`worker/main.py` is typed against `JobQueue` (the existing port, `ports.py:32`) instead of
`PostgresJobQueue`, which gives that port its first consumer and closes A-12 without deleting a
port PLAN.md lists as core. Delete the last ignore lines and the `ignore_imports` blocks.

**Tests.**
- The repository integration tests (`tests/adapters/test_persistence_integration.py`) assert the
  mapped records, not ORM rows.
- Service tests use the fakes with no `type: ignore`; `mypy` over `tests` for the fakes is part of
  the gate. **[CHECK]** whether CI's mypy target includes `tests` today and extend it if not.
- Mutation check on the contract: re-adding one forbidden import fails `lint-imports`.

**Done when.** `lint-imports` passes with no `ignore_imports`; the seven `type: ignore[arg-type]`
lines are gone; `JobQueue`, `ManuscriptParser` and the repository ports each have a consumer typed
against them; no service module imports from `editta.adapters`.

**Risk.** Medium: it touches every service. Mitigation: four small PRs, each shippable on its own
and run against the full integration suite; behaviour does not change, only types and wiring.
Mapping ORM → record adds an allocation per read, negligible at this load.

**Rejected alternatives.** A generic `Repository[T]` base protocol (the repositories have different
shapes, so the generic would be wider than any consumer needs). A DI container (the composition
roots are short and explicit; a container would hide the wiring the audit wants visible). Casting
ORM rows through `TYPE_CHECKING` (hides the leak, does not fix it).

---

## Item 7: Rate limits across processes (A-5)

**Finding.** Two per-process counters stand behind limits described as global: the slowapi limiter
and the LLM `TokenBucket` (`client.py:85-87`, aligned with the organisation's provider limit but
built per client). The README recommends several workers.

**Status.** The API half is done in PR #14: `EDITTA_RATE_LIMIT_STORAGE_URI` and the optional
`redis` extra. What remains is the LLM side, the documentation, and proof for the Redis path.

**Pattern.** *Static partition first, shared store only on a trigger.* Do the cheapest correct
thing: divide the organisation's rate limit by the number of worker processes, in configuration.

**Changes.**
- `config.py`: add `llm_requests_per_minute` (the share *per worker process*). README states the
  rule `share = organisation limit ÷ number of workers`. `build_llm_client` passes it to the
  `TokenBucket`.
- README "Scaling workers": state the rule and that adding a worker means lowering every worker's
  share. Add `EDITTA_RATE_LIMIT_STORAGE_URI` and the new setting to `.env.example` (open item O-2).
- The module-level `limiter` in `api/middleware.py` stays: slowapi binds it in route decorators at
  import time, so it cannot move into the app factory without rewriting every route. It is backed by
  the shared store when configured, which removes the per-process counter problem it was flagged
  for. Not a defect to rewrite.

**Trigger for a shared LLM bucket.** Autoscaling workers, or provider 429s showing in `llm.call`
logs (item 3 makes them visible). Then implement a `RateLimiter` port over the Redis store already
configured for the API, behind `TokenBucket`'s interface, inside the LLM adapter only.

**Tests.** `build_llm_client` passes the configured share through (unit). Redis path: see O-3.

**Done when.** The per-worker share is configurable and documented; `.env.example` lists both
variables.

---

## Item 8: Smaller route and email fixes (A-8, A-9, A-10)

**A-8: `isinstance(email_sender, ConsoleEmailSender)` in a route (`web.py:131-133`).**
Pattern: *capability decided at the composition root.* `api/deps.py` already names adapters; add a
dependency `get_show_dev_login_link() -> bool` there, computed from the sender it built. The route
asks the boolean and stops importing `ConsoleEmailSender`. Test: a route test with the dependency
overridden both ways.

**A-9: `web.py` (402 lines) mixes login, dashboard, upload and download.**
Pattern: *split by use case, mechanically.* Three modules `web_login.py`, `web_dashboard.py`,
`web_artifacts.py`, each exposing a router; `api/main.py` includes them. No logic changes, so the
existing route tests are the proof. Do it after item 6 has removed the adapter imports, and before
FEATURES_PLAN PR 3 adds more to the file.

**A-10: the sign-in email is a fire-and-forget `BackgroundTask` (`web.py:125`, `auth.py:44`).**
Pattern: *Decorator over `EmailSender` for bounded retry.* `RetryingEmailSender` (3 attempts,
exponential backoff, jitter) wraps the real sender in the composition root and logs
`email.send_failed` after the last attempt, with the tenant id but no address. The timing-safe
behaviour of `request_login` (`accounts.py:146-153`) does not change: the response still returns
before any send. Not an outbox: the sign-in link is cheap to re-request, so a durable queue buys
little. Switch to a transactional outbox only if mail becomes business-critical (see "Switching
triggers"). Test: a fake sender fails twice then succeeds; one that always fails produces one error
log.

**Done when.** The route has no adapter import, `web.py` is three modules under 150 lines each, and
a failing SMTP call is retried then logged.

---

## Item 9: Close the PR #14 loose ends (O-1 to O-6)

| ID | Item | Action | Who |
|---|---|---|---|
| O-1 | `infra/docker-compose.yml` still uses `minio/minio:latest`, whose pull is denied | Replace the dev S3 service with an image that is actually pullable (candidates: `motoserver/moto`, `localstack/localstack`). **Verify the pull works before committing**; no image could be verified from this environment. | user picks the image, then a one-line change |
| O-2 | `.env.example` lacks `EDITTA_RATE_LIMIT_STORAGE_URI` (and, after items 1 and 7, the job timeouts and the LLM share) | Add the lines with comments | PR 1 |
| O-3 | The Postgres concurrency test for `record_usage` skips without Docker; the Redis rate-limit path is untested | Confirm the CI log shows the Postgres test running (CI has Docker). For Redis add a CI service container `redis:7` and one test that two limiter instances sharing the URI share a count, using the existing Docker skip convention | PR 10 |
| O-4 | The presigned-URL signature is unproven against a store that validates signatures (moto does not) | One manual check against the real bucket before launch: generate a link, download it with `curl`, confirm the file name | user, once |
| O-5 | The commented `.docx` was never opened in real Word | Manual check with 3 real manuscripts (tracked changes, a table, footnotes): comments anchor to the quoted text, no repair prompt | user, once |
| O-6 | The live smoke eval was never run with the current wiring (`client.py:3-7`) | `python -m evals.run` once; it costs real money, as the README says. Use the result to set item 1's timeouts | user, once |

---

## Item 10: Reconcile `docs/PLAN.md` with the code

The audit's `[FALSE]` claims are closed either by making them true or by changing the sentence.

| Claim | Where | Resolution |
|---|---|---|
| Persistence, parsing, export are adapters behind ports | PLAN.md:49, :123 | True after item 6. No edit. |
| `JobQueue` is a core port | PLAN.md:142 | True after item 6 (PR 8). |
| No manuscript content in logs | PLAN.md §5 / :289 | True in every process after item 2. |
| Logs carry tenant, job, pass | PLAN.md:289 | True after item 2. |
| Prometheus metrics, OpenTelemetry tracing, Sentry, DEAD-job alerts | PLAN.md:289-292 | Rewrite: the structured `llm.call` lines (item 3) and `ops_check` (item 4) are the current mechanism. Move metrics, tracing and Sentry to a "later, when" list with the triggers below. Do not leave promises the code does not keep. |

Add a short ADR, `docs/adr/0001-dependency-rule.md` (half a page): the rule, the import-linter
contracts that enforce it, and the one allowed sibling edge `export` → `parsing`. This is the
"intent baseline" a future audit would otherwise have to infer.

---

## Module by module

| Module | Paradigm and patterns after the work | Items |
|---|---|---|
| `domain` | Functional core. Gains `LLMTimeout`, read models, repository and parser ports, `uploads` pure functions. Stays free of outward imports (contract). | 1, 6 |
| `passes` | Strategy over a template; binds `pass_name` into log context. Otherwise unchanged. | 2 |
| `services` | Transaction scripts over ports. No adapter imports (contract). `JobRunner`: fenced UoW per step, Decorators on `LLMClient`, wall-clock bounds per phase. | 1, 2, 6 |
| `adapters/llm` | Anti-corruption layer; explicit timeouts; `InstrumentedLLMClient` decorator; shared `limits`. | 1, 3, 7 |
| `adapters/persistence` | Repository + Unit of Work + data mapper to domain read models; new ops-check queries. | 4, 6 |
| `adapters/parsing`, `export` | Behind `ManuscriptParser` and `ArtifactExporter`; built in the roots. | 6 |
| `adapters/queue` | Behind `JobQueue`, consumed by the worker. | 6 |
| `adapters/email` | `RetryingEmailSender` decorator. | 8 |
| `adapters/storage` | Unchanged. | — |
| `api` | Routes depend on ports via `Depends`; `RequestContextMiddleware`; `/healthz`, `/readyz`; `web.py` split in three. Root: `deps.py`. | 2, 4, 6, 8 |
| `worker` | Root: builds adapters, calls `configure_logging`, typed against `JobQueue`; new `ops_check`. | 2, 4, 6 |
| `cli.py` | Root: same wiring as the worker for the parser and exporters. | 2, 6 |
| `config.py` | New settings: job timeouts, per-worker LLM share. | 1, 7 |
| `infra` / CI | Non-root image, `HEALTHCHECK`, `lint-imports`, optional Redis service. | 4, 5, 9 |
| `docs` | PLAN.md reconciled; ADR 0001. | 10 |

## Sequence and PR slicing

| PR | Contents | Depends on | Touches |
|---|---|---|---|
| PR 1 | Item 1 (timeouts), item 7 (per-worker share), `.env.example` | none | `adapters/llm`, `services/jobs`, `config` |
| PR 2 | Items 2 and 3 (logging, tracing) | PR 1 (both edit `services/jobs.py`) | `observability`, roots, `adapters/llm` |
| PR 3 | Item 4 (health, ops check, Dockerfile) | PR 2 for logging; the ops queries land on the concrete repository first and move behind a port in PR 5 | `api`, `worker`, `infra` |
| PR 4 | Item 5 (import-linter, ignore lists) | none | `pyproject`, CI |
| PR 5 | Item 6: read models and job ports | PR 4 | `domain`, `persistence`, `jobs.py`, fakes |
| PR 6 | Item 6: parser and exporter ports | PR 5 | `jobs.py`, `pipeline.py`, roots |
| PR 7 | Item 6: remaining services | PR 5 | `uploads`, `accounts`, `reports`, `retention` |
| PR 8 | Item 6: routes and roots; `JobQueue` consumer | PR 7 | `api`, `worker` |
| PR 9 | Item 8 (A-8, A-9, A-10) | PR 8 for A-8; A-9 and A-10 are independent | `api/routes`, `adapters/email` |
| PR 10 | Item 9 (O-1, O-3) and item 10 docs | PR 8 | infra, tests, docs |

PR 1 and PR 4 are independent and can run in parallel. Every other pair touches shared files
(`services/jobs.py` is edited by PRs 1, 2, 5 and 6), so merge them in order. Each PR keeps the repo
green on its own and is shippable alone. Interaction notes from the audit hold: A-3 and A-4 share
PR 2 because they share the logging setup; A-5 and A-6 share PR 1 because they share the LLM
adapter; item 5 precedes item 6 so each slice is measured by the ignore list shrinking.

## Switching triggers (what would change this plan)

| Condition | What it unlocks |
|---|---|
| A second worker process is run, or workers autoscale | Shared LLM rate bucket on the Redis store (item 7). |
| Paying customers with an availability promise, or more than one operator | Prometheus metrics, OpenTelemetry traces, Sentry, replacing grep-able `llm.call` lines (items 3, 4). Added as a middleware plus another `LLMClient` decorator; no service changes. |
| Mail becomes business-critical (invoices, deadlines) | Transactional outbox drained by the worker, replacing `RetryingEmailSender` (item 8). |
| A second developer joins | Make `lint-imports` a required status check; add CODEOWNERS for `domain/` and `domain/ports.py`. |
| Extracting the worker as its own service | Item 6 must already be done; the worker then depends on `domain` and `adapters` only. |

## Accepted debt

- **A-7** (provider model ids and prices in `domain/budget.py` and `passes/*.py`): documented in
  README, the single place to change a model is the pass class, and moving them behind the adapter
  would put pricing policy in a layer that should not own it. Revisit only if a second provider
  family needs different pass-to-model mappings per deployment.
- The module-level slowapi `limiter` stays a module singleton (item 7), for the reason given there.

## Definition of done for the whole plan

1. `lint-imports` passes with no `ignore_imports`, and it runs in CI.
2. `services/` imports nothing from `editta.adapters`; `tests/services/fakes.py` has no
   `type: ignore[arg-type]`.
3. `structlog.configure(` appears once; a worker log line carries `service`, `job_id`, `tenant_id`,
   `pass_name`; a `text` field cannot reach output in any process.
4. Every external call on the job path has a timeout; a hung call ends the attempt, not the lease.
5. `/readyz`, a non-root image with `HEALTHCHECK`, and `ops_check` exist and are documented.
6. `docs/PLAN.md` makes no claim the code contradicts; ADR 0001 records the dependency rule.
7. A re-run of `/arch-audit` scores the architecture 8/10 or more with no ledger row above LOW.

## What this plan does not cover

Bugs (use the defect hunt), feature work (`docs/FEATURES_PLAN.md`), and runtime behaviour the audit
could not see statically: RLS correctness in migrations 0001-0006, the prompts and domain maths
(`budget`, `quotes`) that were read only through the import graph, and behaviour under load or real
provider failures. A two-worker trace after PR 3 would be the first real evidence for A-5.
