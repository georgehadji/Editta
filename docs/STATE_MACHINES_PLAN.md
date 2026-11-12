# Implicit state machines: discovery and remediation plan

Date: 2026-11-11. Base: `main` at `2ba07f5`.

This plan finds every place where Editta keeps state without declaring it. For each one it
decides how far to make the state explicit. Every decision must do three things:

- improve the user experience (UX);
- improve the developer experience (DX);
- improve the agent experience (AX).

It must also break nothing that works today.

Evidence labels: **[VERIFIED]** was read in the code. **[CHECK]** is an assumption. The
first step of its item must confirm it before any code changes.

---

## 0. The decision in one paragraph

Do not add a state-machine library or framework. Make each machine explicit at the lowest
level that closes its real risk. Use the idioms the codebase already has:

- the pure functional core in `domain/`;
- fenced SQL writes;
- expand-only migrations;
- error-class policy.

This also keeps a promise the docs already make. `docs/PLAN.md:80` says `states.py` holds
"enum + allowed transitions". `docs/PLAN.md:176` says transitions are "enforced in code +
CHECK constraint". Today the code has neither **[VERIFIED]**. The plan moves the code to
the promise and fixes the parts of the promise that were wrong. `FAILED` and `CANCELLED`
appear in the PLAN.md diagram, but they do not exist (item 1).

| Level | What it means | When to use it |
|---|---|---|
| **L0 Document** | An entry in `docs/STATE_MACHINES.md` that lists the states, the transitions and where each is enforced, with file:line. | The design is sound. Only the knowledge is implicit. |
| **L1 Name** | A pure function in `domain/` derives the state from the fields as an enum, and a table test covers it. UI, API, CLI and ops call the function instead of re-deriving the state with ad-hoc `if`s. | Several readers interpret the same fields differently, or a user or operator cannot see the state. |
| **L2 Guard** | A table in `domain/` lists the legal transitions. The write path enforces the from-state in the same SQL `WHERE` as the lease fence. | An illegal transition is representable and costs money or data. |
| **L3 Constrain** | A DB `CHECK`, added `NOT VALID` and then `VALIDATE`d after a data check. | A free-text column where a typo or a hand edit would silently break claim, health or UI logic. |

Why this serves all three:

- **UX.** One derived "phase" gives users honest labels, each with a next action:
  - *queued*;
  - *retrying*;
  - *running: analyzing*;
  - *ready*;
  - *failed, credit refunded*.
- **DX.** One table replaces strings that are scattered across SQL, Jinja and Python. mypy
  and table tests turn a missing case into a build error.
- **AX.** The API and the CLI expose the same machine-readable phase.
  `docs/STATE_MACHINES.md` gives agents a map, so they do not have to re-read 6,500 lines
  to learn the lifecycle.

---

## 1. How the machines were found (repeatable method)

Run this again after any feature that adds a column, a cookie or a background step. The
next such feature is FEATURES_PLAN F2 (see SM-21).

1. **Fields.** Grep `tables.py` and `infra/migrations/versions/*.py` for:
   - nullable timestamps and `*_owner` columns;
   - `str` columns named `state`, `status`, `kind`, `reason` or `role`;
   - counters such as `attempt`;
   - pairs of columns that encode one fact, such as `status` and `blob_key`.
2. **Literals.** Grep Python, SQL text and Jinja for comparisons against state-like
   literals, such as `== "completed"` or `NOT IN ('completed', 'dead')`.
3. **Mutable instances.** Grep the adapters and the worker for `self._x =` outside
   `__init__`. This finds breakers, buckets and loops.
4. **Derived state.** Find state that is the *existence* of rows (ledger keys,
   `pass_results`, `reports`), and state that lives in cookies or in Stripe.
5. **Control flow.** Find ordered `_set_state` calls, sagas (put blob, then write the row,
   then commit) and polling loops.
6. **Surface check.** For each machine, answer three questions:
   - What does the user see in each state?
   - What does an operator or an agent see?
   - Which transitions have a test?

The first pass used five parallel read-only subagents:

- four Sonnet sweeps: job/queue/worker, auth/billing, adapters/background and web UI;
- one Haiku mechanical inventory of columns, enums, literals and mutable fields.

The main session then spot-checked every claim that drives a priority. Results are in §2.

**Remaining discovery.** Each item below is **[CHECK]** and is one small read-only task.

| ID | Question | Why it matters | Who |
|---|---|---|---|
| D-1 | Does the Anthropic SDK retry by default (`adapters/llm/client.py:96`)? | Decides whether the breaker counts one failure per call or one per retry. | Haiku |
| D-2 | Production data checks in §5. | Must pass before any L3 `VALIDATE`. | Operator, SQL given |
| D-3 | Do Stripe's documented semantics confirm "at most one crediting event per Checkout Session" (`services/billing.py:84-87`)? | Gates item 5. | Sonnet with web access |
| D-4 | Does every mutating web route declare `verify_csrf` (`api/middleware.py:180`)? | Lists the htmx error states that can occur. | Haiku |

---

## 2. Catalog: what was found

Paths are under `src/editta/` unless they start with `tests/`, `infra/` or `docs/`.

| ID | Machine | Encoded in | Today | Risk | Decision |
|---|---|---|---|---|---|
| SM-1 | Job pipeline state | `analysis_jobs.state`, a free `str` | An enum exists, but there is no transition table and terminal states are not absorbing at the SQL level. | Med | **L2 + L3** |
| SM-2 | Lease and claimability | `lease_owner`, `lease_expires_at`, `attempt` | One documented SQL rule. The states it produces have no names. | Low | **L1** |
| SM-3 | Failure classification | `failure_kind` (NULL means "ours"), `error` | Free text. A retrying PENDING job still shows its last `error`. | Med (UX) | **L1 + L3** |
| SM-4 | User-facing job phase | Ad-hoc `if`s in `_job_list.html`, `web.py:359` and `schemas.py:58` | Raw enum strings. `JobState(state)` can raise. | High (UX) | **L1** (read model) |
| SM-5 | Upload form | htmx attributes, `#upload-error` | No *submitting* state. Errors other than 402 and 422 are silent. A stale error stays after a success. | High (UX) | UI fix, **L0** |
| SM-6 | Magic-link token | Signed token plus `users.last_login_at` | Atomic and sound. Errors appear in the browser as raw JSON. | Low | **L0** + error page |
| SM-7 | Login confirm nonce | `editta_login_nonce` cookie | Sound. Its 403 is raw JSON. | Low | **L0** + error page |
| SM-8 | Session | Signed cookie, 30 days, no server-side store | No revocation. A 401 on a browser route is JSON, not a redirect. | Med | **L0** + redirect. Revocation is a separate decision (§6). |
| SM-9 | User activation and invite | `users` row plus `last_login_at IS NULL` | A re-invite silently keeps the old role. | Low | **L0** + response flag |
| SM-10 | Credit reservation | Ledger rows `job:{id}:reserve` and `:refund` | Atomic, idempotent and fenced. | Low | **L1** (name only) |
| SM-11 | Stripe purchase | Ledger key `stripe:{event_id}`; Stripe holds the session | Idempotent per *event*, not per *session*. The success page is static. | Med (money) | **L2** (key per session) |
| SM-12 | Manuscript lifecycle | `status` (`uploaded` or `purged`) **and** `blob_key == ""` | Two flags encode one fact, and readers check different flags. | Med | **L3** + one reader |
| SM-13 | Artifact availability | Whether `reports` rows exist | "Not ready", "not applicable" and "purged" all mean `ReportNotReady`. The report link can 404. | Med (UX) | Folded into SM-4 |
| SM-14 | Circuit breaker | `_consecutive_failures`, `_opened_at` | Half-open is not a state. Nothing is logged on open or close. | Med (ops) | **L1** + logs |
| SM-15 | Token bucket | `_tokens`, `_updated_at` | A refill rate of 0 raises ZeroDivisionError. | Low | Config validation only |
| SM-16 | Worker process | `asyncio.Event`, heartbeat task | Sound. No drain deadline. | Low | **L0** |
| SM-17 | OpenRouter retry loop | Local variables in `_post` | Local and bounded. Retries are not logged. | Low | **L0** |
| SM-18 | Rate-limit key | slowapi counters; the key moves from IP to user | Per process when no storage URI is set. This is already documented. | Low | **L0** |
| SM-19 | Upload saga | Put blob, write rows, commit | A commit failure leaves an orphan blob. This is a documented shortcut (`uploads.py:99`). | Low | **L0** + log line |
| SM-20 | Retention purge | Delete blobs, delete rows, mark purged | Re-running is idempotent. A job stays `completed` after its artifacts are gone. | Med (UX) | Folded into SM-4 and SM-12 |
| SM-21 | *Planned:* batch mode (FEATURES_PLAN F2) | Planned `AWAITING_BATCH` state plus a `batch_id` column that holds the marker `'pending:<uuid>'` (`docs/FEATURES_PLAN.md:250-260`) | Not built yet. As designed, the marker string encodes a state inside an id column. | Future | Design it explicitly (item 9) |

Rejected candidate: **budget** (`domain/budget.py`). It is a pure check that raises
`BudgetExceeded` and holds no state.

### Key evidence (all [VERIFIED])

- **SM-1.**
  - `set_state_if_owned` (`adapters/persistence/repositories.py:184-208`) fences only on
    `lease_owner`. Any lease holder can write any string, including COMPLETED → PENDING.
  - The real order exists only as the sequence of `_set_state` calls at
    `services/jobs.py:237, 262, 294, 307, 322`.
  - The resume path writes PARSING from any non-terminal state.
  - Retry writes PENDING (`jobs.py:426-432`). DEAD is written at `jobs.py:441-448`.
  - Exhaustion dead-letters at claim time from any non-terminal state
    (`jobs.py:196-201`).
  - The terminal set is hard-coded as `'completed', 'dead'` at
    `adapters/queue/postgres.py:41, 46, 85`, plus `'dead'` at `:80` and `"completed"` in
    `templates/_job_list.html:9`.
  - `state` has no `CHECK`. The table's only constraint is `attempt >= 0`
    (`tables.py:61`).
- **SM-2.** The rule itself is documented well at `postgres.py:8-14`. "Backing off" means
  the lease is in the future and the owner is NULL. `ops_check` counts only free or
  expired jobs as waiting. A job that is backing off, or exhausted and waiting for its
  next claim, does not show on any surface.
- **SM-3 and SM-4.**
  - A retrying job is PENDING with `error` set (`jobs.py:426-432`).
    `_job_list.html:7` renders it as `pending — <error>` in red, so it looks failed.
  - A DEAD job never says "credit refunded", although `_dead_letter` refunds every
    reservation (`jobs.py:452-460`).
  - Only the queue health SQL reads `failure_kind`.
  - `JobState(state)` at `api/routes/web.py:359` raises `ValueError` on an unknown string,
    and the dashboard returns 500.
  - `JobStatusResponse` is `{job_id, state: str, error}` (`api/schemas.py:58-61`), so
    every API client must hard-code both the state list and the terminal set.
- **SM-5.**
  - The upload form (`templates/dashboard.html:16-24`) has no `hx-disabled-elt` and no
    indicator.
  - Only 402 and 422 are retargeted (`web.py:76-90`). htmx does not swap other error
    responses by default, so 401, 403 (CSRF), 413 (body limit), 429 and 5xx all fail
    silently.
  - A successful upload swaps only `#job-list`, so an old error stays on screen.
  - The poll (`_job_list.html:1`) fails silently with a 401 when the session expires.
- **SM-6 and SM-7.** `web.py:163-164, 192-199` raise `HTTPException` on a browser GET or
  POST. The user sees `{"detail": "magic link has expired"}` and has no way forward.
- **SM-11.**
  - `api/routes/billing.py:62-68` keys the ledger row on `stripe:{event['id']}`.
  - `append_once` resolves conflicts only on `idempotency_key` (`repositories.py:343`).
  - The docstring at `services/billing.py:84-87` *assumes* that Stripe sends at most one
    crediting event per session.
  - `/billing/success` (`billing.py:75-77`) is static HTML.
- **SM-12.**
  - `mark_purged` sets `status='purged'` **and** `blob_key=''`
    (`repositories.py:144-150`).
  - `list_expired` reads `status`, while the runner reads `blob_key` (`jobs.py:241`).
  - A job on a purged manuscript raises a base `EdittaError` (`retryable=False`,
    `domain/errors.py:16`). It goes DEAD with `failure_kind=NULL`, which the ops check
    alerts on.
- **SM-13 and SM-20.** Retention deletes the `reports` rows
    (`services/retention.py:101-104`), but the job stays `completed`. The report link at
    `_job_list.html:10` is unconditional, so it returns a JSON 404.
- **SM-14.** In `adapters/llm/resilience.py:22-37`, half-open is only the moment when
  `before_call` re-stamps `_opened_at`. Nothing is logged on open, close or probe.
  Operators see only `job.error = "circuit breaker open"`.

---

## 3. Work items

Every item keeps the checks CI already runs:

- `ruff check src tests evals`;
- `mypy src` (strict);
- `lint-imports`;
- `pytest` with coverage of at least 80 %;
- bandit, pip-audit and gitleaks.

Items that touch repository or queue SQL also run the Postgres integration tests:
`EDITTA_TEST_DATABASE_URL=… pytest tests/adapters/test_persistence_integration.py`.

### Item 1: Job transition table, with absorbing terminal states (SM-1) — L2

**Change.**

1. In `domain/states.py`, add `ALLOWED_FROM: Mapping[JobState, frozenset[JobState]]`. It
   is the closure of today's call sites and adds no new behaviour:

   | To | Allowed from | Path |
   |---|---|---|
   | `PARSING` | every non-terminal state | first run, resume |
   | `ANALYZING` | `PARSING` | |
   | `SYNTHESIZING` | `ANALYZING` | |
   | `EXPORTING` | `SYNTHESIZING` | |
   | `COMPLETED` | `EXPORTING` | |
   | `PENDING` | every non-terminal state | retry |
   | `DEAD` | every non-terminal state | failure, exhaustion |

   No state can be entered from `COMPLETED` or `DEAD`.
2. In `repositories.py:184`, `set_state_if_owned`:
   - type the parameter as `state: JobState`;
   - add `AnalysisJob.state.in_(ALLOWED_FROM[state])` to the existing `WHERE`;
   - keep raising `LeaseLost` when zero rows match.

   On that failure path only, run one follow-up `SELECT` so the log says which case it
   was: lease lost, or illegal transition.
3. In `adapters/queue/postgres.py`, replace the four literal lists with one bound
   parameter, `state <> ALL(:terminal)`, built from `TERMINAL_STATES`. The terminal set
   then has a single source of truth.
4. Rewrite the `docs/PLAN.md:176-183` diagram to match:
   - "FAILED" is PENDING with an `error` set, shown to users as *retrying*.
   - `CANCELLED` is not built. Mark it as future work.

**Why it breaks nothing.** The table is the closure of today's call sites. Write this test
**first**: it walks every runner path in `tests/services/test_jobs.py` (resume, retry,
exhaustion, lease loss) and asserts that every recorded `state_history` pair is in
`ALLOWED_FROM`. If it fails, widen the table. Do not change behaviour.

**Tests.**

- `tests/domain/test_states.py`: no terminal state has a successor, and every
  non-terminal state can reach DEAD.
- An integration test: a COMPLETED job cannot be set to PENDING, even by its lease
  holder.

**UX/DX/AX.** Users see no change. Developers get one table and a typed signature.
Agents can read the lifecycle in 15 lines instead of 465.

**Subagent:** Sonnet. **PR:** A.

### Item 2: Constrain the free-text state columns (SM-1, SM-3, SM-12) — L3

**Change.** Add migration `0010_state_checks.py`. It only adds constraints. Each one is
added `NOT VALID` and then `VALIDATE`d in the same migration, and only after D-2 passes.

| Constraint | Rule |
|---|---|
| `ck_job_state` | `state IN (<JobState values>)` |
| `ck_job_failure_kind` | `failure_kind IS NULL OR (failure_kind = 'customer' AND state = 'dead')` |
| `ck_manuscript_status` | `status IN ('uploaded','purged')` |
| `ck_manuscript_purged_blob` | `(status = 'purged') = (blob_key = '')` |
| `ck_report_kind` | `kind IN (<ArtifactKind values>)` |

Mirror the constraints in `tables.py`, so autogenerate does not propose dropping them.
`tests/adapters/test_tables.py` checks that the value lists match the enums.

**SM-12: one reader.** The runner keeps its `if not record.blob_key` check
(`jobs.py:241`). With the new constraint, that check is provably the same fact as
`status`.

**Why it breaks nothing.** `NOT VALID` never rejects an existing row. A new row that
violates a constraint was a bug already. Downgrade drops the constraints.

**Subagent:** Sonnet for the migration. Haiku for the `tables.py` mirror. **PR:** A,
reviewed together with item 1 because both touch queue and repository SQL.

### Item 3: One job read model for UI, API, CLI and ops (SM-2, SM-3, SM-4, SM-13, SM-20) — L1

This item has the most UX value.

**Change.** Add `domain/job_view.py`. It is pure:

```python
class JobPhase(StrEnum):
    QUEUED = "queued"                # pending, no error
    RETRYING = "retrying"            # pending with error: waiting out backoff
    RUNNING = "running"              # parsing | analyzing | synthesizing | exporting
    READY = "ready"                  # completed and the report artifact exists
    EXPIRED = "expired"              # completed, artifacts purged by retention
    FAILED_INPUT = "failed_input"    # dead, failure_kind = customer: fix file, re-upload
    FAILED_SYSTEM = "failed_system"  # dead, failure_kind NULL: our fault, refunded
    UNKNOWN = "unknown"              # state not in JobState: a reader never raises

def job_view(state: str, error: str | None, failure_kind: str | None,
             artifacts: frozenset[ArtifactKind]) -> JobView: ...
```

`JobView` has these fields:

- `phase`.
- `step`: the running `JobState`, or None.
- `terminal: bool`.
- `refunded: bool`: true for both failed phases, because `_dead_letter` always refunds a
  reservation.
- `message`: plain language and safe to show the user. It is built from `error`, which
  `errors.py` already keeps free of vendor text.
- `next_action`: `"wait"`, `"download"`, `"reupload"` or `"contact"`.

**Readers switch to it.**

- `api/routes/web.py`: `_any_job_running` becomes `any(not v.terminal ...)`. This removes
  the `ValueError` crash. The template context gets the views.
- `templates/_job_list.html`:
  - Render the phase labels.
  - Show "Retrying — <message>" in a neutral style, not red.
  - Show "Failed — credit refunded" with the next action.
  - Show the report link only for `READY`, not on `state == "completed"`.
  - For `EXPIRED`, show "Report deleted after N days (retention)".
- `api/schemas.py`, `JobStatusResponse`: **add** `phase`, `terminal`, `refunded`,
  `next_action` and `artifacts: list[ArtifactKind]`. **Keep** `state` and `error`
  unchanged. The change is additive, so existing clients keep working.
- `cli.py`: add a read-only `editta jobs --tenant <id> [--json]`. It prints the view plus
  the lease status, so operators and agents can see jobs that are backing off or
  exhausted.
- SM-2: add `lease_status(lease_owner, lease_expires_at, now)`, which returns
  `FREE | HELD | BACKING_OFF | EXPIRED`. Only the CLI uses it. The claim SQL does not
  change.

**Tests.**

- `tests/domain/test_job_view.py`: a table test over every combination of state,
  failure_kind and artifacts, including UNKNOWN.
- Web tests for the renders the sweep found untested:
  - a dead row, for both an input failure and a system failure;
  - a retrying row;
  - a completed job with no report;
  - the empty list;
  - an unknown state.
- A contract test: the new API fields are present and the old fields are unchanged.

**UX/DX/AX.** Users get honest labels and a next step. Developers lose the ad-hoc
`if`-chains. API clients and agents stop hard-coding the terminal set.

**Subagent:** Sonnet. Haiku writes the parametrized table test from the spec above.
**PR:** B.

### Item 4: Upload, poll and auth UI states (SM-5, SM-6, SM-7, SM-8)

Check D-4 first.

**Upload form** (`dashboard.html`):

- Add `hx-disabled-elt="find button"` and an `hx-indicator`. This gives a visible
  *submitting* state and prevents an accidental double charge.
- On success, append an out-of-band swap to the success fragment to clear
  `#upload-error`: `<div id="upload-error" hx-swap-oob="true"></div>`. Then reset the file
  input.

**htmx error states.** In `base.html`, add one small `htmx:responseError` listener. It
covers upload, poll and sign-out in one place:

- 401: set `location` to `/t/{tenant}/login`.
- 403, 413, 429 and 5xx: write a plain message into a `role="alert"` region.

**401 on browser routes.** A browser GET to a page that needs a session (dashboard,
artifact download) redirects to `/t/{tenant}/login` with a 303. JSON routes keep the 401.
Decide by route group, not by sniffing the `Accept` header.

**Magic-link errors.** At `web.py:163-164, 192-199`, render `login.html` instead of a
JSON error:

- Pass the message through the existing `error` variable (`login.html:10`). Nothing sets
  it today.
- Add a "send a new link" form.
- Keep the 401 and 403 status codes.

**Polling accessibility.** Add `aria-live="polite"` to a status summary line, not to the
whole table. Screen readers then announce phase changes without re-reading every row.

**Checkout success** (`api/routes/billing.py:75-77`): render a template that shows the
current balance and a link back to the dashboard. The page stays static, with no new
state and no polling.

**Tests.** Web tests for each of these:

- An expired or already-used link renders HTML that includes the form.
- A 401 on a browser GET redirects to the login page.
- The success fragment carries the out-of-band clear.
- The upload button carries `hx-disabled-elt`.

**Subagent:** Sonnet. **PR:** C.

### Item 5: Credit a Stripe purchase at most once per Checkout Session (SM-11) — L2

**[CHECK] D-3 first.** If Stripe guarantees at most one crediting event per session, this
item becomes L0: document the guarantee and keep today's code.

**Change.** In `api/routes/billing.py:62-68`, key the ledger row on the session instead
of the event: `idempotency_key=f"stripe-session:{purchase.checkout_session_id}"`.

**Transition window.** Rows written before the deploy are keyed by event id. For the
first 3 days after the deploy, which is Stripe's redelivery window, the route also skips
the insert when a `purchase` row with the same `stripe_ref` already exists. Mark that
check `# shortcut: transition guard for pre-deploy event-keyed rows, delete after <deploy date + 3 days>`.

**Why not a unique index on `stripe_ref`?** `append_once` resolves conflicts only on
`idempotency_key` (`repositories.py:343`). With a second unique constraint, a replay
would raise `IntegrityError` and the route would return 500. Stripe would then retry for
days.

**Tests** (`tests/api/test_billing_routes.py`):

- `completed(paid)` followed by `async_payment_succeeded` for the same session credits
  once.
- An old event-keyed row plus a new event for the same session credits once.

**Subagent:** Sonnet. This is the money path, so the main session reviews it. **PR:** D,
on its own.

### Item 6: Make the circuit breaker's states visible (SM-14, SM-15) — L1

**Change.**

- In `adapters/llm/resilience.py`, add a `state` property that returns `CLOSED`, `OPEN`
  or `HALF_OPEN`. It is derived from the two existing fields, with no new field.
- Log `llm.breaker_opened`, `llm.breaker_probe` and `llm.breaker_closed` on transitions
  only, never on every call.
- In `config.py`, validate that the rate limit and the refill rate are greater than 0, so
  the `TokenBucket` cannot divide by zero.

D-1 decides whether the failure threshold needs retuning. Record the answer and change
nothing else.

**Tests** (`tests/adapters/test_llm_resilience.py`):

- A failed probe re-opens the breaker.
- The `state` property in each phase.
- Each log event fires once per transition.

**Subagent:** Haiku (small and mechanical). **PR:** B.

### Item 7: Small flags and log lines (SM-9, SM-19)

- **Invite.** `provision_user` returns `(user, created: bool)`. When the user already
  exists, `/auth/invite` answers `{"existing": true, "role": <actual role>}`, so the
  caller can see that the requested role was not applied. The response change is
  additive.
- **Upload saga.** When the cleanup at `services/uploads.py:97` itself raises:
  - log `upload.orphan_blob` with the blob key;
  - re-raise the **original** error. Today the cleanup failure masks it.

**Subagent:** Haiku. **PR:** C.

### Item 8: A catalog for humans and agents (all SMs) — L0

**Change.**

- Write `docs/STATE_MACHINES.md` with one section for each of SM-1 to SM-21. Each
  section gives:
  - the states;
  - a transition table: from, to, trigger, file:line, guard, and where it is enforced
    (DB, code or nothing);
  - what the user, the operator and an agent see in each state.
- Add Mermaid `stateDiagram-v2` diagrams for SM-1, SM-2, SM-4 and SM-14 only.
- Link the file from `README.md` and from the `domain/states.py` docstring.

**Drift guard.** A test asserts that every `JobState`, `JobPhase` and `ArtifactKind`
value appears in `docs/STATE_MACHINES.md`. It is a cheap string check, and it stops a
new state from shipping without documentation.

**Subagent:** Sonnet writes the catalog from this plan and the code. Haiku writes the
drift-guard test. **PR:** E, last, so the line numbers are final.

### Item 9: Design batch mode's states explicitly from day one (SM-21)

This is a rule for whoever builds FEATURES_PLAN F2. Nothing is built now.

- `AWAITING_BATCH` enters `ALLOWED_FROM` (item 1), `ck_job_state` (item 2, a new
  migration) and `JobPhase` (shown as `RUNNING` with step `awaiting_batch`). All three
  change in the same PR.
- Do not encode a state inside an id. The planned `batch_id = 'pending:<uuid>'` marker
  (`docs/FEATURES_PLAN.md:256-258`) should be two nullable columns instead:
  `batch_submit_token` (written before submit) and `batch_id` (written after). The
  states are then readable without string parsing:
  - none: neither column is set;
  - submitting: only `batch_submit_token` is set;
  - submitted: `batch_id` is set.

  Add a `CHECK` that `batch_id` implies `batch_submit_token`.
- Add an entry to `docs/STATE_MACHINES.md`. The drift guard enforces it.

**Subagent:** none now. Add one line to `docs/FEATURES_PLAN.md` §3.2 that points to this
item.

---

## 4. Sequence and PR slicing

| PR | Items | Why this order |
|---|---|---|
| A | 1, 2 | Hardens the write path and the schema first, so every later reader can trust the values. Queue and repository SQL get one review. |
| B | 3, 6 | The read model relies on item 2's guarantees. The breaker logs are independent and small. |
| C | 4, 7 | Mostly UI. Item 4 uses item 3's phases. |
| D | 5 | Money path, reviewed on its own. Gated on D-3. |
| E | 8, 9 (doc line) | Documents the final code and line numbers. |

Once A is merged, B and D are independent and can run in parallel. C waits for B.

**Agent allocation.**

| Work | Model |
|---|---|
| Implementing items 1-5 | Sonnet |
| Items 6 and 7, the `tables.py` mirror, parametrized test tables, the drift guard, D-1 and D-4 | Haiku |
| Reviewing PRs A and D: the money path and the migrations | Main session (plan owner) |

Give each subagent a brief that names its item, its files and its "done when". One
subagent never gets two PRs.

---

## 5. Production data checks (D-2, before PR A's `VALIDATE`)

```sql
SELECT state, count(*) FROM analysis_jobs GROUP BY state;            -- only JobState values
SELECT count(*) FROM analysis_jobs WHERE failure_kind IS NOT NULL
   AND (failure_kind <> 'customer' OR state <> 'dead');              -- expect 0
SELECT status, (blob_key = '') AS empty, count(*) FROM manuscripts
   GROUP BY 1, 2;                                                    -- expect uploaded/false, purged/true
SELECT kind, count(*) FROM reports GROUP BY kind;                    -- only ArtifactKind values
SELECT stripe_ref, count(*) FROM credit_ledger WHERE reason = 'purchase'
   AND stripe_ref IS NOT NULL GROUP BY 1 HAVING count(*) > 1;        -- already double-credited?
```

If a check returns a non-zero result, do not `VALIDATE` that constraint. It stays
`NOT VALID`, which still guards new rows. Write a fix migration for the bad rows first.

---

## 6. Accepted debt (decided, not done)

- **No server-side session revocation (SM-8).** A stolen cookie stays valid for 30 days.
  A fix needs either a sessions table or a per-user `sessions_valid_after` timestamp.
  That is a security product decision, not a state-machine cleanup. It is listed here so
  the owner can decide.
- **No ledger sign constraint by reason (SM-10).** `cli.py:173` grants negative credits
  with `reason="purchase"`, so a sign `CHECK` would first need a new `adjustment` reason.
  Every write is code-controlled and idempotent. Revisit this only if a second writer
  appears.
- **Retention racing a running job (SM-20).** Retention is 30 days (`config.py:38`). A job
  reaches a terminal state within about 1 hour: 3 attempts, with backoff of at most
  30 min. The race cannot happen in practice.
- **Worker drain deadline (SM-16), OpenRouter retry logging (SM-17), per-process rate
  limits (SM-18).** Documented in the catalog. No code change.
- **The claim SQL keeps its one-rule design (SM-2).** A separate "backing off" column
  would add a write path and gain nothing. Item 3 names the state for readers instead.

---

## 7. Definition of done

- SM-1 to SM-21 each have a catalog entry, and the drift guard is green.
- No state-like string literal remains in queue SQL or Jinja. They appear only in
  `domain/` and in migrations.
- `set_state_if_owned` cannot move a job out of COMPLETED or DEAD, and an integration
  test proves it.
- Every free-text state column has a validated `CHECK`, or a recorded reason why it
  stays `NOT VALID`.
- The dashboard, the JSON API and the CLI derive job status from one function. None of
  them raises on an unknown value.
- Every UI state the sweep found has a render test, and no htmx error is silent.
- Existing API fields are byte-for-byte unchanged, and the contract tests prove it.
- `docs/PLAN.md` §3 matches the code.
- CI is green, including the Postgres integration tests.

## 8. Not covered

- New features, new deployables or a state-machine library.
- Changes to what a job *does*: passes, pricing or retry counts.
- Session revocation (§6).
- Anything in `evals/`.
