# Editta — Architecture & Build Plan

**Product:** AI editorial assistant for publishing houses. Ingests essay/book manuscripts (DOCX/PDF), runs multi-pass LLM analysis (language/style, structure/argument, metadata/positioning, integrity-light), and produces a structured editorial report (DOCX) that supports — never replaces — human editorial judgment. Greek-language manuscripts are the primary market and moat.

**Non-goals (hard product constraints):** no accept/reject recommendations, no auto-editing, no AI-detection or plagiarism scoring, no numeric "quality scores" presented as objective metrics.

**Status:** validated via concierge MVP (manual reports → positive editor feedback). This plan covers the production-ready system.

---

## 1. Architecture Decision

### Chosen: Modular monolith with hexagonal (ports & adapters) core + async job worker

One deployable codebase, three runtime processes:

```
┌────────────┐   HTTPS    ┌─────────────────┐
│  Web UI /  │ ─────────► │   API (FastAPI)  │──┐
│  Clients   │            │   stateless      │  │ enqueue (Postgres)
└────────────┘            └─────────────────┘  ▼
                          ┌─────────────────────────────┐
                          │  PostgreSQL                  │
                          │  • relational data           │
                          │  • job queue (SKIP LOCKED)   │
                          └─────────────────────────────┘
                                        ▲
                          ┌─────────────┴───────────────┐
                          │  Worker(s) (asyncio)         │
                          │  parse → analyze ×4 → synth  │
                          │  → export                    │
                          └──────┬───────────────┬──────┘
                                 ▼               ▼
                        ┌───────────────┐  ┌────────────┐
                        │ S3-compatible │  │ Claude API  │
                        │ object store  │  │ (Opus 5)    │
                        └───────────────┘  └────────────┘
```

**Why this and not the alternatives:**

| Alternative | Verdict | Reason |
|---|---|---|
| Microservices (6 "agents" as services) | Rejected | Solo/small team, LLM-bound (not CPU-bound) workload. Network boundaries between analysis passes add latency, failure modes, and ops cost with zero scaling benefit — the bottleneck is the LLM API, not compute. |
| Serverless functions (Lambda/Cloud Run jobs) | Rejected | Jobs run 3–10 min with multi-step state; checkpoint/resume and prompt-cache timing are awkward across cold starts. Vendor lock-in without a compensating win. |
| Workflow engine (Temporal, Prefect) | Deferred | The DAG is 6 nodes and linear-with-one-fanout. A state machine table + idempotent steps covers it. Documented as the migration path if the pipeline grows past ~15 nodes or needs human-in-the-loop steps. |
| Redis/Kafka queue | Deferred | Postgres `FOR UPDATE SKIP LOCKED` handles thousands of jobs/day with transactional job state (job row and queue entry commit atomically — no dual-write problem). Swap to Redis/SQS behind the same `JobQueue` port if volume ever demands it. |

**Why hexagonal:** the domain core (pipeline logic, report assembly, token budgeting) stays pure and framework-free. LLM provider, file parsing, storage, persistence, and export are adapters behind ports (Python `Protocol`s). This gives: unit tests without API calls, a fake LLM for CI, provider swap freedom, and clean seams if modules are ever extracted to services.

**Language/stack:** Python 3.12+. Rationale: best-in-class document tooling (`python-docx`, `pypdf`), first-class Anthropic SDK, mature async ecosystem. The workload is I/O-bound so Python's CPU story is irrelevant here.

| Concern | Choice |
|---|---|
| API | FastAPI + Pydantic v2 (validation at every boundary) |
| DB | PostgreSQL 16, SQLAlchemy 2.0 async, Alembic migrations |
| Queue | Postgres `SKIP LOCKED` behind a `JobQueue` port |
| Object storage | S3-compatible (Cloudflare R2 / Hetzner / MinIO dev), SSE, presigned URLs |
| LLM | `anthropic` SDK, model `claude-opus-5` |
| DOCX in/out | `python-docx` (read + report builder) |
| Frontend (Phase 3) | Server-rendered Jinja2 + HTMX (no SPA build chain until warranted) |
| Billing | Stripe Checkout, credit-pack model |
| Deploy | Docker Compose → single EU VPS/Fly.io; scale = add worker containers |
| Observability | structlog (JSON), Prometheus metrics, Sentry, OpenTelemetry traces |

---

## 2. Module Breakdown — Paradigms & Patterns

Repo layout (src layout, many small files, 200–400 lines each):

```
editta/
  pyproject.toml
  src/editta/
    domain/            # pure functional core — zero I/O, zero framework imports
      models.py        #   frozen Pydantic models: Manuscript, Finding, PassResult, Report
      report.py        #   pure report assembly from pass results
      budget.py        #   token/cost estimation, guardrails
      states.py        #   job state machine (enum + allowed transitions)
    passes/            # Strategy pattern — one class per analysis pass
      base.py          #   AnalysisPass Protocol + shared prompt scaffolding
      language.py      #   language & style
      structure.py     #   structure & argumentation
      metadata.py      #   metadata & positioning
      integrity.py     #   unsupported claims + repetitions (light)
      synthesis.py     #   editorial report synthesis
    adapters/
      llm/             # Adapter: Anthropic client wrapper
        client.py      #   caching-aware calls, structured outputs
        resilience.py  #   retry w/ jitter, circuit breaker, rate budget
      parsing/         # Adapter: DOCX/PDF → normalized text + section map
        docx.py, pdf.py, safety.py   # zip-bomb/MIME/size guards
      export/          # Builder pattern: Report → styled DOCX
        docx_builder.py
      storage/         # Port + S3/local adapters
      persistence/     # Repository + Unit of Work over SQLAlchemy
        repositories.py, uow.py, tables.py
      queue/           # JobQueue port + Postgres SKIP LOCKED impl
    services/          # application layer (imperative shell)
      pipeline.py      #   orchestrates parse→analyze→synth→export
      jobs.py          #   job lifecycle, checkpointing, credits
      accounts.py      #   tenants, users, auth
    api/               # thin controllers, DTOs, response envelope
      routes/, deps.py, middleware.py, schemas.py
    worker/
      main.py          #   queue consumer, graceful shutdown
    config.py          # pydantic-settings, fail-fast on missing secrets
  tests/               # unit / integration / e2e mirrors src
  evals/               # 5 reference essays + expected-findings checklists
  infra/               # Dockerfiles, compose, migrations, CI
  docs/
```

### Paradigm map

| Module | Paradigm | Key patterns | Why |
|---|---|---|---|
| `domain/` | **Functional core**: pure functions, immutable (frozen) models | Value objects; explicit state machine | Deterministic, trivially unit-testable, no hidden side effects |
| `passes/` | OO strategy over a shared protocol | **Strategy**, **Factory** (per-pass model/effort config), **Template method** (shared prompt scaffold) | Add/modify a pass without touching the orchestrator |
| `services/` | **Imperative shell**: async orchestration, owns all side effects | **Pipeline**, **Unit of Work**, checkpointed **State machine** | One place where I/O, transactions, and ordering live |
| `adapters/llm` | OO adapter | **Adapter**, **Retry+jitter**, **Circuit breaker**, **Token-bucket** rate budget | Isolate vendor; survive 429/529/overload without cascading |
| `adapters/persistence` | Repository pattern | **Repository**, **UoW**; every query tenant-scoped | Swap storage, mock in tests, enforce tenancy in one layer |
| `adapters/parsing` | Defensive functional | Guard-clause validation, early returns | Untrusted file input |
| `adapters/export` | **Builder** | Fluent DOCX report builder | Complex document assembly, golden-file testable |
| `api/` | Thin controllers + DTOs | Response envelope `{success, data, error, meta}`; dependency injection via FastAPI `Depends` | Controllers < 20 lines; all logic in services |
| `worker/` | Event loop consumer | Competing consumers, graceful drain | Horizontal scale by replica count |

### Core ports (contracts everything hangs off)

```python
class AnalysisPass(Protocol):
    name: str
    output_schema: type[BaseModel]          # structured-output schema
    def build_prompt(self, m: ManuscriptText) -> PromptParts: ...

class LLMClient(Protocol):
    async def analyze(self, parts: PromptParts, schema: type[T],
                      *, cache: CachePolicy, effort: str) -> LLMResult[T]: ...
    async def count_tokens(self, parts: PromptParts) -> int: ...

class JobQueue(Protocol):
    async def enqueue(self, job_id: UUID) -> None: ...
    async def claim(self) -> ClaimedJob | None: ...   # SKIP LOCKED under the hood

class BlobStore(Protocol):
    async def put(self, key: str, data: AsyncIterator[bytes]) -> None: ...
    async def get(self, key: str) -> AsyncIterator[bytes]: ...
    async def delete_prefix(self, prefix: str) -> None: ...
```

**Dependency injection:** constructor injection, wired manually in one composition root per process (`api/deps.py`, `worker/main.py`). No DI framework — explicit wiring is grep-able and 40 lines total.

---

## 3. Data Model

All rows carry `tenant_id`; `created_at`/`updated_at` everywhere; soft-delete only where legally required (audit), hard-delete for manuscript content (GDPR).

```
Tenant        (id, name, plan, credit_balance*, settings)
User          (id, tenant_id, email, role[admin|editor], auth)
Manuscript    (id, tenant_id, title, blob_key, sha256, token_count,
               status, retention_until)
AnalysisJob   (id, tenant_id, manuscript_id, state, error, attempt,
               cost_input_tokens, cost_output_tokens, cache_read_tokens)
PassResult    (id, job_id, pass_name, blob_key(JSON), model, usage, ...)
Report        (id, job_id, blob_key(DOCX), summary_blob_key)
CreditLedger  (id, tenant_id, delta, reason[purchase|job|refund],
               job_id?, stripe_ref?, idempotency_key UNIQUE)  -- append-only
AuditEvent    (id, tenant_id, actor, action, subject, at)     -- append-only
```

\* `credit_balance` is a **derived value** — the ledger is the source of truth (immutable, append-only; balance = SUM(delta), cached on Tenant, recomputed on write inside the same transaction).

**Job state machine** (`domain/states.py`, transitions enforced in code + CHECK constraint):

```
PENDING → PARSING → ANALYZING → SYNTHESIZING → EXPORTING → COMPLETED
   │          │          │            │             │
   └──────────┴──────────┴────────────┴─────────────┴──► FAILED (retryable ≤3)
                                                          └──► DEAD (alert)
CANCELLED reachable from any non-terminal state.
```

Each pass checkpoint persists to `PassResult` on completion. **Retry resumes from the last checkpoint — completed passes are never re-run and never re-billed in tokens.**

**Transactions:** every pipeline step commits in its own short transaction (`services/jobs.py`); no LLM call runs inside an open transaction. A failure therefore never rolls back checkpoints or its own FAILED record. **Retry policy** (`domain/states.py`): `EdittaError.retryable` decides — `LLMError` retries with exponential backoff (60s, 120s, … cap 30 min, via `JobQueue.defer`, i.e. the lease's expiry) up to 3 attempts; `ParseError`/`BudgetExceeded` go straight to DEAD. The credit is reserved once per job and refunded only on DEAD. **Attempts are counted at claim** (the claim UPDATE increments `attempt`), so a worker crash or OOM still consumes one and a crash-looping job is dead-lettered instead of re-run forever. **Fencing:** every runner state write is conditional on its `lease_owner` (`set_state_if_owned`), and `release`/`defer` are fenced the same way; a worker that lost its lease abandons the job without writing, so it cannot DEAD-and-refund a job another worker completed. Synthesis is checkpointed like the passes.

---

## 4. LLM Strategy (token minimization is designed-in, not bolted-on)

**Model:** `claude-opus-5` ($5/$25 per MTok, 1M context, 128K output) for all passes. Per-pass model routing is deliberately **not** built — at ~$2/manuscript the routing config would cost more in complexity than it saves in cents. The `Factory` seam exists; populate it only if volume changes the math.

**Call plan per manuscript** (Greek essay ~50k words ≈ ~175k tokens; Greek tokenizes at ~3–3.5 tok/word):

```
1. count_tokens preflight  → gate: reject > 700k tokens; record baseline
2. Pass 1 (language)       → manuscript block marked cache_control
                             (cache WRITE, 1.25×) — await first token
3. Passes 2–4              → asyncio.gather, same prefix (cache READ, 0.1×)
4. Synthesis               → manuscript (uncached: different output schema) + findings
                             JSON with ids; returns a plan that cites findings by id
```

**Ordering matters:** the cache becomes readable only after the first response begins. Prime with one call, then fan out — firing all 4 concurrently pays full input price 4×. The budget ceiling is checked **before** the first billable call, from the `count_tokens` preflight (`domain/budget.project_remaining_cost_usd`), on both the CLI and the worker path (shared core: `services/analysis.py`).

| Cost component | ≈ per manuscript |
|---|---|
| Input w/ caching (1×1.25 write + 3×0.1 reads + 1× uncached synthesis on ~175k) | ~$2.2 |
| Output (~25k tokens across 5 calls) | ~$0.60 |
| **Total** | **~$2.9** (synthesis cannot share the cache; see below) |

At 25–40€/report → ~89–93% gross margin (~$2.9 LLM cost per report). **Budget guardrail:** `domain/budget.py` computes a hard cost ceiling per job (default $6); the pipeline aborts before any billable call (state DEAD, credit refunded) if projected cost exceeds it.

**Further token techniques:**

- **Structured outputs** (`output_config.format` + Pydantic schema) on all 4 analysis passes: compact JSON findings (`section`, `quote`, `issue`, `severity`, `suggestion`) — no prose padding in intermediate results, guaranteed parseable, zero retry-on-malformed-JSON waste.
- **Adaptive thinking** (`thinking: {type: "adaptive"}`) with one `effort` (`"high"`, the Opus 5 default) for **all** passes and synthesis. Per the prompt-caching docs, changing `output_config.effort` invalidates the messages cache — where the manuscript lives — so mixed per-pass effort silently turns cache reads into full-price misses. The passes must also share one output schema: per the structured-outputs docs, changing `output_config.format` invalidates the prompt cache. **Synthesis therefore cannot read the passes' cache** (it has its own schema); it sends no `cache_control` and the budget prices its manuscript input at the full rate. Per-pass effort is possible later via the beta per-message effort feature (`passes/base.py`).
- **Checkpoint resume** (§3) — failed jobs never re-pay completed passes.
- **Batches API (Phase 5):** 50% off everything for bulk/overnight submissions ("upload evening, reports by morning"). Same code path, different endpoint — designed behind the `LLMClient` port from day one.
- **Never estimate Greek tokens with tiktoken** — always the `count_tokens` endpoint.
- **Prompt-injection containment doubles as token hygiene:** passes have no tools enabled; nothing a hostile manuscript says can trigger extra calls.

**Resilience around the API:** SDK-native retries for 429/5xx + circuit breaker (open after 5 consecutive failures, half-open probe at 60s) + per-worker semaphore (max 4 concurrent LLM calls) + global token-bucket aligned to the org rate limits.

---

## 5. Security & Safety

Manuscripts are unpublished intellectual property — confidentiality is the product's license to exist.

**Upload hardening (`adapters/parsing/safety.py`):**
- Size cap (25 MB), MIME check by magic bytes (not extension), reject `.docm`/macro containers.
- Zip-bomb guard: max decompressed size 100 MB, max archive entries 2,000, no path traversal on entry names.
- Files stored under UUID keys (`{tenant}/manuscripts/{uuid}/source`); original filename kept only as DB metadata (sanitized, `parsing.sanitize_filename`). Format is dispatched on the *detected* type (`parsing.parse_manuscript`); PDFs need a text layer (no OCR).

**Prompt-injection defense (manuscripts are untrusted input):**
- Manuscript text isolated in a clearly delimited data block; system prompt states it is content to analyze, never instructions.
- No tools enabled on analysis calls; structured-output schema constrains the response shape.
- **Quote anchoring:** every finding must include a verbatim quote that identifies one passage: long quotes (≥ 24 chars) may match fuzzily (≥ 0.9); short ones must match exactly and occur once (a unique typo anchors, a common word does not). Text is NFC- and whitespace-normalized first. Kills both injection-steered and hallucinated findings before they reach an editor. **Synthesis cites findings by id** (`SynthesisPlan.finding_ids`, resolved in `domain/synthesis.py`): its output type cannot carry a finding, so it cannot add, alter or re-quote one — unknown ids are dropped. Its overview/section summaries remain free prose.

**Tenancy & access:**
- Every repository method takes `tenant_id`; Postgres **Row-Level Security** as defense-in-depth (second, independent enforcement layer). **Forced** on the table owner (migration 0004) — the app role owns the tables, and Postgres otherwise skips RLS for owners. Every app path, including pre-auth user lookups, runs inside a tenant-scoped transaction; a missing tenant context deterministically sees no rows.
- AuthN: magic-link email login (no passwords to breach) + session cookies (HttpOnly, Secure, SameSite=Lax); CSRF tokens on mutations. API keys (hashed at rest) for the later API tier.
- **Sign-in links are confirmed, not followed:** opening a link shows which account and workspace it signs into; the POST that signs in needs a nonce cookie set by that page, so neither a forwarded link nor a cross-site form can swap someone's session. Sign-in emails are sent after responding (no timing or delivery-failure signal of which emails have accounts); rate limits key on the signed-in user or the IP, never on a raw cookie.
- **Invite-only access** (`services/accounts.py`): a link is emailed only to an existing account and never returned in an HTTP response; signing in never creates an account. Tenants and first admins are created by an operator (`editta create-tenant`); admins invite others (`POST /auth/invite`). Sessions are re-checked against the database on every request, so removing a user revokes access. The console email sender exists only with `EDITTA_ENVIRONMENT=development` (the default is production).
- AuthZ: role check in the service layer, not in controllers.

**Data protection:**
- TLS everywhere; SSE on object storage; EU-region hosting; DPA with subprocessors.
- **Retention:** manuscript content auto-purged after a configurable window (default 30 days) — the source file, every analysis checkpoint and the final report (all of them quote the text) and their rows; the manuscript row and its jobs are kept, marked `purged`, because the credit ledger and audit trail reference them. Storage is deleted before the record is marked, so a failure is retried, never left behind (`services/retention.py`).- Manuscript text **never** appears in logs, error messages, or Sentry payloads (scrubbing middleware); LLM calls run under the API's standard no-training terms.
- Secrets via environment/secret manager only; `config.py` fails fast at startup if any are missing.
- **Worker database role:** the queue and the retention sweep must connect as the BYPASSRLS `editta_worker` role (`EDITTA_WORKER_DATABASE_URL`); with RLS forced, any other role sees no rows. Both refuse to start otherwise. **Deploy order for migration 0004:** ship the code first, then migrate — older code's unscoped login lookups stop working once RLS is forced.

**Platform:** rate limiting per tenant and per IP (SlowAPI), strict input validation on every endpoint (Pydantic), security headers (CSP, HSTS), dependency scanning (`pip-audit`), SAST (`bandit`), secret scanning (`gitleaks`) in CI, append-only `AuditEvent` for uploads/downloads/deletions/credit changes.

**Product safety rails (encoded, not just documented):** report template contains no verdict/score fields; synthesis schema has no "recommendation" property — the non-goals are structurally impossible to violate.

---

## 6. Robustness, Performance, Scalability

**Robustness**
- Idempotent pipeline steps + checkpoints (§3); job claim uses lease + heartbeat so a crashed worker's job is reclaimed after timeout.
- Explicit typed exception hierarchy (`EdittaError` → `ParseError | LLMError | BudgetExceeded | ...`); no silent catches; every failure sets job state + human-readable error + refunds the credit.
- Graceful shutdown: worker drains current job on SIGTERM before exit.
- DB backups (daily + WAL), restore drill in Phase 4.

**CPU & memory**
- Workload is I/O-bound: one asyncio worker process handles multiple jobs on a 1-vCPU container. `uvloop` for the event loop.
- Streaming everywhere files move (upload → S3, S3 → parser): no whole-file buffering beyond the parsed text (~2 MB/manuscript ceiling).
- Per-worker concurrency: `MAX_JOBS_PER_WORKER = 2`, `MAX_LLM_CONCURRENCY = 4` (named constants, config-overridable).
- LLM responses streamed (required near max_tokens limits anyway) and written incrementally.

**Parallelization layers**
1. Within a job: 4 passes fan out via `asyncio.gather` (after cache prime).
2. Within a worker: multiple jobs interleave on the event loop.
3. Across workers: competing consumers on `SKIP LOCKED` — scale is "add a container."
4. Fleet-wide: global rate budget prevents workers from tripping org rate limits.

**Scalability path (documented, not pre-built):** Postgres queue → Redis/SQS at ~10k jobs/day; single VPS → API/worker split across nodes (already separate processes); Temporal if the DAG grows. Each swap sits behind an existing port.

---

## 7. Observability

- **Structured logs** (structlog, JSON): every log line carries `tenant_id`, `job_id`, `pass_name`. No manuscript content.
- **Metrics** (Prometheus): jobs by state, job duration histogram, tokens in/out per pass, **cache hit ratio**, **cost per job**, credit purchases, queue depth, circuit-breaker state.
- **Tracing** (OpenTelemetry): one trace per job, spans per pass — makes "why was this report slow" a 10-second question.
- **Errors:** Sentry with scrubbing. **Alerts:** DEAD jobs, queue depth > threshold, cache hit ratio < 60% (means the prime-then-fan-out ordering broke), daily cost anomaly.

---

## 8. Testing & Evals

Per-layer strategy (80% coverage gate in CI):

| Layer | Approach |
|---|---|
| `domain/` | Pure unit tests; property-based (Hypothesis) for budget math & state transitions |
| `passes/` | Prompt-construction unit tests + schema validation with a **FakeLLM** adapter |
| `adapters/parsing` | Corpus of hostile files: zip bombs, macro docs, traversal names, 0-byte, wrong-magic |
| `adapters/export` | Golden-file DOCX tests (unzip + XML compare) |
| `adapters/persistence` | Integration via testcontainers-postgres; RLS verified with cross-tenant access attempts |
| Pipeline | End-to-end with FakeLLM: happy path, mid-pass crash + resume, budget abort, injection quote-anchor rejection |
| API | Contract tests per endpoint incl. authZ matrix (role × endpoint) |
| **Evals** | `evals/`: 5 reference Greek essays with known issues + expected-findings checklists. Run against the real API on every prompt change (manual trigger, not CI — costs real money). Regression = a known finding disappears. |

TDD discipline: failing test first for every service/domain unit; adapters may be spiked then covered.

---

## 9. Build Phases

**Phase 0 — Foundation (0.5 wk)**
Repo scaffold (layout above), `pyproject.toml`, ruff + mypy(strict) + pytest + coverage gate, pre-commit, gitleaks/bandit/pip-audit, GitHub Actions CI, Docker Compose (postgres, minio), `config.py`.
*Done when: CI green on empty skeleton; `docker compose up` gives a working dev stack.*

**Phase 1 — Core pipeline + CLI (1.5 wk)** ← the real product
Domain models, 5 passes with structured-output schemas, LLM adapter with caching/resilience, DOCX parse (hardened) + export builder, quote-anchor validation, budget guardrail. CLI: `editta analyze manuscript.docx -o report.docx`. Evals seeded with the 2 concierge essays + 3 more.
*Done when: CLI produces an editor-quality Greek report end-to-end; cost per run logged ≈ $2; all 5 eval essays pass their checklists.*

**Phase 2 — Service layer (2 wk)**
Postgres schema + migrations + RLS, repositories/UoW, job queue + worker with checkpoint/resume + lease reclaim, S3 storage, FastAPI (upload, job status, report download via presigned URL), magic-link auth, tenancy, rate limiting, audit log, retention deletion job.
*Done when: two tenants can concurrently upload → poll → download with full isolation (verified by tests); kill -9 on a worker mid-job loses nothing.*

**Phase 3 — UI + billing (1.5 wk)**
Jinja2+HTMX: upload page, job list with status, report download, credit balance. Stripe Checkout credit packs; append-only ledger with idempotency keys; reserve **at upload** (tenant row locked, balance checked, HTTP 402 when short) / refund when the job dies.
*Done when: a non-technical editor completes upload→pay→report unassisted; Stripe webhooks replay-safe.*

**Phase 4 — Production hardening (1 wk)**
Observability stack live, alerts wired, backup + restore drill, load test (20 concurrent jobs), security review (security-reviewer pass over auth/upload/payment code), GDPR docs (privacy policy, DPA, deletion endpoint), EU deploy.
*Done when: restore drill succeeds; load test holds; security findings CRITICAL/HIGH = 0.*

**Phase 5 — Post-launch levers (on demand, gated on paying customers)**
Batches API bulk mode (-50% cost), PDF ingestion polish, expanded eval suite, per-pass model routing (only if unit economics change), RAG bibliography module (only after hallucination-safe design), API tier for publisher integrations.

**Explicitly deferred:** orchestrator platform, multi-agent framework, dashboards/analytics, share links, SPA frontend, AI-detection (never), plagiarism (never), quality scores (never).

---

## 10. Risks

| Risk | Mitigation |
|---|---|
| Prompt/report quality regression while iterating | Eval suite (§8) run on every prompt change |
| Greek token cost higher than estimate | `count_tokens` preflight + per-job budget abort + cost metrics from day 1 |
| Cache misses silently 5×-ing input cost | Cache-hit-ratio metric + alert < 60% |
| Manuscript confidentiality breach | RLS + scoped repositories + encryption + retention deletion + no-content logging; audit trail |
| Hostile uploads | Parsing hardening corpus in CI (§8) |
| LLM API outage | Circuit breaker + jobs park in queue (PENDING); nothing lost, reports delayed not failed |
| Scope creep back toward "v2 disguised as MVP" | Phase gates: Phase 5 items require paying customers |
