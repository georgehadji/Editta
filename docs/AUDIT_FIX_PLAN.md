# Audit fix plan

Fixes for the 13 findings of the repo audit of 2026-11-05. Each item says what changes,
which design pattern carries it and why that pattern fits this codebase, the files it
touches, the tests that prove it, and when it is done.

## Ground rules from the existing architecture

The fixes keep the structure the code already has. Nothing here adds a framework, a DI
container or a new layer.

| Layer | Where | Paradigm already in use | Rule for these fixes |
|---|---|---|---|
| Domain | `src/editta/domain/` | Pure functions, frozen value objects, typed error hierarchy | No I/O. Retryability is decided by the error *class* (`errors.py`), never by message text. |
| Ports | `domain/ports.py` | Protocols (structural typing) | Unchanged. New behaviour wraps a port (Decorator), it does not widen it. |
| Application services | `src/editta/services/` | Transaction scripts with injected repositories (`submit_manuscript`), Observer hooks (`on_pass_completed`) | Shared use cases live here, not in routes. Raise domain errors; routes map them to HTTP. |
| Adapters | `src/editta/adapters/` | Anti-corruption layer: translate vendor shapes and errors into domain types | Vendor exceptions never cross the boundary with their text. |
| Delivery | `src/editta/api/`, `worker/` | Composition roots (`api/deps.py`, `worker/main.py`), ASGI middleware | Cross-cutting HTTP concerns are middleware; wiring stays in the roots. |
| Persistence | `adapters/persistence/` | Repository + Unit of Work, append-only idempotent ledger, fenced writes | No schema migration is needed for any item. |

Every change ships with the check CI already runs: `ruff check src tests evals`,
`mypy src` (strict), `pytest` (coverage ≥ 80 %), `bandit`, `pip-audit`. Items that touch
the ledger, reports or queue SQL also run the Postgres integration tests
(`EDITTA_TEST_DATABASE_URL=… pytest tests/adapters/test_persistence_integration.py`).

## Order of work

| Step | Items | Why this order |
|---|---|---|
| PR 1, commit 1 | 6 | Two red tests hide new failures. Make the suite green first. |
| PR 1 | 1, 2, 11, 12 | All four touch the LLM adapters and the error types. Item 2 builds on item 1's new error class. |
| PR 2 | 3 | Money path, reviewed on its own. Needs a Stripe dashboard change at deploy. |
| PR 3 | 4, 5 | HTTP edge hardening. Needs a deploy-config change. |
| PR 4 | 7, 8, 9, 10 | Deletions and one merge. No behaviour change for users. |
| PR 5 (conditional) | 13 | Only after a production data check. |

---

## Item 6: config tests read the developer's `.env`

**Problem.** `Settings` declares `env_file=".env"`. `tests/conftest.py` sets the required
variables but cannot stop pydantic-settings from filling the *other* fields from a real
`.env`: here `EDITTA_ENVIRONMENT=development` and `EDITTA_OPENROUTER_API_KEY` leak in, and
two tests in `tests/test_config.py` fail.

**Design.** Test isolation through a fixture (pytest's own mechanism). Nothing changes in
production code: reading `.env` is correct for local runs.

**Steps.**
1. In the autouse fixture `_settings_env` (`tests/conftest.py`):
   - `monkeypatch.setitem(Settings.model_config, "env_file", None)`, so no test reads a `.env`.
   - Delete every inherited `EDITTA_*` variable before setting the known ones:
     `for key in [k for k in os.environ if k.startswith("EDITTA_")]: monkeypatch.delenv(key)`.
   - Replace `os.environ.pop("EDITTA_ENVIRONMENT", None)` with the loop above.
     The pop is not undone after the test; the loop is.

**Tests.** The two failing tests pass with a populated `.env` present. Check by running
`pytest tests/test_config.py` from the repo root on this machine.

**Done when.** `pytest` is green locally with the real `.env` in place.

---

## Item 1: manuscript text leaks into job errors

**Problem.** `AnthropicLLMClient.analyze` wraps every exception as
`LLMError(f"LLM call failed: {exc}")`. A cut-off answer raises pydantic's `ValidationError`,
whose text contains `input_value='…'`, the model's output quoting the manuscript. That
string becomes `analysis_jobs.error`, is rendered on the dashboard
(`templates/_job_list.html:7`) and returned by `GET /manuscripts/jobs/{id}`. Seen live on the
Fykaris run (`«ἐνδυναμώ`).

**Design.** Anti-corruption layer. The adapter is the only place that knows vendor
exception types, so it translates them into domain errors with fixed, content-free
messages. A new error class (item 2) carries the meaning; no message text carries it.

**Steps.**
1. `domain/errors.py`: add `LLMOutputInvalid(LLMError)` (retryable, model returned output
   that does not fit the schema) and `LLMOutputTruncated(LLMError)` (item 2,
   `retryable = False`). Their docstrings say messages must never contain model output.
2. `adapters/llm/client.py`, in `analyze`:
   - Catch `pydantic.ValidationError` *before* the generic `except Exception`.
   - Classify (see item 2) and raise `LLMOutputTruncated` or `LLMOutputInvalid` with a
     fixed message, using `raise … from None`. `from None` matters: with `from exc`, any
     later `log.exception` (for example `worker.job_unrecorded_failure`) would print the
     pydantic text in the chained traceback.
   - Do **not** call `self._breaker.record_failure()` for these. The circuit breaker
     tracks provider health, and a bad answer comes back from a healthy provider.
   - The existing `parsed_output is None` branch raises `LLMOutputInvalid` (or
     `LLMOutputTruncated` when `stop_reason == "max_tokens"`) instead of a bare `LLMError`.
3. `adapters/llm/openrouter.py`, `_parse_output`: it already drops the text, but switch to
   `from None` and the new classes, so both adapters behave the same.
4. Defense in depth, one line: `services/jobs.py::_error_message` keeps using `str(exc)`
   for `EdittaError`. The adapters now guarantee the content. No sanitizer is added:
   scrubbing free text with a regex cannot be made reliable for Greek prose.

**Tests.** In `tests/adapters/test_llm_client.py` (it has no `analyze` tests today):
- Stub `client._client.messages.stream` with an async context manager whose
  `get_final_message()` raises a real `ValidationError` built from
  `PassFindings.model_validate_json('{"findings":[{"section":"«ἐνδυναμώ')`.
- Assert the raised error is `LLMOutputTruncated`, `"ἐνδυναμώ" not in str(err)`,
  `err.__cause__ is None` and `err.__suppress_context__ is True`, and that the breaker's
  failure count did not move.
- Mirror the test for OpenRouter with an SSE stream whose content is cut off.

**Done when.** No code path builds an `LLMError` message from a vendor exception's text.
Check with `grep -n 'LLMError(f".*{exc}' src/editta/adapters/llm` → the only remaining
hits are transport errors (`httpx.HTTPError`, `APIStatusError`), whose text holds no
manuscript content.

---

## Item 2: failed AI calls are paid, retried up to 9×, and not recorded

**Problem.** A cut-off answer is retryable, so one pass can run 3 job attempts × up to 3
adapter attempts (OpenRouter mid-stream retry). Each Opus try on a 300k-token book costs
about $2.50. Usage is written only in `JobRunner._checkpoint`, after a success, so failed
spend is invisible and the $6 ceiling never sees it.

**Design.**
- *Typed error decides retry policy.* `LLMOutputTruncated.retryable = False`. The policy in
  `domain/states.py` already reads the flag, so the job goes `DEAD` and the credit is
  refunded by the existing `_dead_letter` path. No change to the state machine.
- *Error carries its cost as a value object.* `LLMError` gains an optional
  `usage: LLMUsage | None` attribute, so the domain error tells the runner what the failed
  call cost without the runner knowing the vendor.
- *Decorator over the `LLMClient` port* records the usage of failed calls. It wraps the
  client the runner already holds, so `services/analysis.py` (shared with the CLI) does not
  change, and every call is covered: the cache-priming pass, the fanned-out siblings
  (including siblings whose error `_most_decisive` does not pick) and synthesis.
  Successful calls are still recorded by `_checkpoint`, as now, so nothing is counted twice.

**Steps.**
1. `domain/errors.py`: `class LLMError(EdittaError)` gets
   `def __init__(self, message: str, *, usage: LLMUsage | None = None)`. Import `LLMUsage`
   from `domain/models.py` (domain imports domain: allowed). Subclasses inherit it.
2. Classification in `adapters/llm/client.py`. **Checked against SDK 2.5.0: the snapshot
   idea does not work.** The SDK parses the answer at `content_block_stop`, before the
   `message_delta` that carries `stop_reason` and the final token usage. After a parse
   failure the snapshot has neither. Done instead:
   - Send the schema in `output_config["format"]` (what `output_format=` does internally)
     and validate the text ourselves after `get_final_message()`. Then `stop_reason` and
     usage are always available.
   - `output_error(stop_reason, usage)` (shared with the OpenRouter adapter): stop reason
     `max_tokens` / `length` → `LLMOutputTruncated`, anything else → `LLMOutputInvalid`.
     The `EOF` fallback is not needed.
3. `adapters/llm/openrouter.py`:
   - `_parse_output` receives the body. `finish_reason == "length"` →
     `LLMOutputTruncated(usage=parse_usage(body.get("usage") or {}))`. Other parse
     failures → `LLMOutputInvalid` with usage.
   - `_post` keeps retrying transport errors and provider errors *before* content
     arrives. It must not loop on a truncation: truncation is detected after `_post`
     returns, so the existing structure already guarantees this. Add a test that pins it.
4. `services/jobs.py`: add a small private decorator class.
   ```python
   class _RecordFailedSpend:
       """LLMClient decorator: a failed call is still billed, so its usage is
       recorded before the error propagates (successes are recorded with their
       checkpoint)."""
       def __init__(self, inner: LLMClient, record: Callable[[LLMUsage], Awaitable[None]]) -> None: ...
       async def count_tokens(self, parts, *, model): return await self._inner.count_tokens(parts, model=model)
       async def analyze(self, parts, schema, **kwargs):
           try:
               return await self._inner.analyze(parts, schema, **kwargs)
           except LLMError as exc:
               if exc.usage is not None:
                   await self._record(exc.usage)
               raise
   ```
   - `JobRunner._execute` builds it per job with
     `record=lambda usage: self._record_usage(job, usage)`. `_record_usage` opens one
     `self._uow()` and calls the existing `repos.jobs.record_usage(...)`.
   - Recording happens before `_handle_failure` writes the state, in its own short
     transaction, like every other step (the module docstring's transaction design).
   - If recording itself fails, log and re-raise the *original* error; a failed metric
     write must not change the job's fate. Use `contextlib.suppress` plus a `log.exception`.
5. `domain/budget.py`: no change. A cut-off answer is the costliest failure, and it now ends
   the job after one try. Capping cumulative spend across attempts would need dollars stored
   per job; the existing token columns are enough to see it. Note this in the
   `project_remaining_cost_usd` docstring as a `shortcut:` line.

**Tests.**
- `tests/services/test_jobs.py`: a fake LLM raising
  `LLMOutputTruncated(usage=LLMUsage(output_tokens=64_000, …))` → outcome `DEAD`, refund
  appended, `record_usage` called once with 64 000 output tokens, and the LLM called
  exactly once (no retry).
- Same fake raising `LLMOutputInvalid` on attempt 1 → outcome `RETRY`, usage recorded.
- A fan-out where both Opus siblings fail → both usages recorded, even though only one
  error propagates.
- `tests/adapters/test_openrouter.py`: `finish_reason: "length"` → `LLMOutputTruncated`
  with the usage chunk's numbers; the handler is called once.

**Done when.** A truncated answer costs one call, the job is refunded, and the job row
shows the tokens it spent.

---

## Item 11: OpenRouter JSON errors bypass classification

**Problem.** `_read_body` calls `json.loads(data)` per SSE line and `response.json()` for
plain bodies. A garbled line or an HTML page sent with status 200 raises
`json.JSONDecodeError`. That error is not an `httpx.HTTPError`, so `_post` does not retry
it, and the job records `internal error (JSONDecodeError)` with a crash trace.

**Design.** Anti-corruption layer again: an undecodable *transport* payload is a transient
provider fault, the same category as a dropped connection.

**Steps.**
1. In `_post`, widen the transport `except` to `except (httpx.HTTPError, json.JSONDecodeError) as exc:`
   and raise `LLMError("OpenRouter sent an unreadable response")` with a fixed message.
   Do not include `exc`: a decode error's `doc` attribute can hold streamed content.
2. Keep the decode inside `_read_body`. No new helper.

**Tests.** A handler returning `_sse_raw("data: {not json")` first and `_ok(...)` second →
the call succeeds on the retry. A handler always returning garbage → `LLMError`, retryable,
message without the garbage.

**Done when.** No `JSONDecodeError` can escape `OpenRouterLLMClient.analyze`.

---

## Item 12: circuit breaker lets every call through after cooldown

**Problem.** `CircuitBreaker.before_call` lets *every* call pass once the cooldown is over,
although its comment promises one probe. With 4 parallel passes, all 4 hit a provider that
is still down.

**Design.** Classic three-state circuit breaker (closed, open, half-open), kept implicit
in the two existing fields so the dataclass stays small. Letting the probe through restarts
the cooldown. Every other caller is refused until the probe's result arrives. A probe that
is cancelled and never reports leaves the breaker open for one more cooldown, then allows a
new probe, so it cannot stick half-open forever.

**Steps.** In `adapters/llm/resilience.py`:
```python
if time.monotonic() - self._opened_at < self.cooldown_seconds:
    raise LLMError("circuit breaker open: LLM provider failing repeatedly")
# Half-open: this call is the probe; everyone else waits another cooldown for its result.
self._opened_at = time.monotonic()
```
`record_success` (closes) and `record_failure` (re-opens past the threshold) are unchanged.

**Tests.** In `tests/adapters/test_llm_resilience.py`, extend
`test_circuit_breaker_half_open_probe_allowed_after_cooldown`: after the cooldown the first
`before_call()` passes and a second one raises. Then `record_success()` → calls pass again.
Add a case: probe passes, no outcome recorded, after another cooldown a new probe passes.

**Done when.** Exactly one call reaches the provider per cooldown while it is failing.

---

## Item 3: Stripe gives credits before the money arrives

**Problem.** `BillingService.extract_purchase` credits every `checkout.session.completed`.
With delayed payment methods (SEPA debit, bank transfer) that event carries
`payment_status: "unpaid"`. The money may never arrive, but the credits stay.

**Design.**
- *Pure policy function* inside the billing service: which Stripe events grant credits. No
  DB access (the module docstring's rule), so it is unit-testable with plain dicts.
- *Value object* `Purchase` (frozen, slotted dataclass) replaces the anonymous
  `tuple[UUID, int, str]`. The route reads fields by name, and adding a field later cannot
  silently shift positions.
- The idempotency key stays `stripe:{event id}`. For one Checkout Session, Stripe sends
  `completed` with `paid` **or** `completed` with `unpaid` followed by
  `async_payment_succeeded`, never both crediting events. So event-id keys cannot
  double-credit, and the ledger needs no change.

**Steps.** In `services/billing.py`:
1. Define the event and status sets:
   - `_CREDITING_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}`
   - `_SETTLED = {"paid", "no_payment_required"}` (the second covers a 100 % coupon).
2. `extract_purchase(event) -> Purchase | None`:
   - Return `None` for any other event type.
   - For `completed`, return `None` unless `session["payment_status"] in _SETTLED`.
   - `async_payment_succeeded` is settled by definition.
3. A missing `metadata.tenant_id` (a Payment Link or another integration on the same
   Stripe account) currently raises `KeyError` → HTTP 500 → Stripe retries for three days.
   Return `None` and log `billing.event_without_tenant` with the event id only.
4. `api/routes/billing.py` uses `purchase.tenant_id`, `purchase.credits` and
   `purchase.checkout_session_id`.
5. Deploy step: in the Stripe dashboard, add `checkout.session.async_payment_succeeded` to
   the webhook endpoint's events. Record this in the README's billing section.

**Tests.** In `tests/services/test_billing.py`, extend `_checkout_event` with
`payment_status` and `event_type` parameters:
- completed + `paid` → purchase
- completed + `unpaid` → `None`
- completed + `no_payment_required` → purchase
- `async_payment_succeeded` → purchase
- `async_payment_failed` → `None`
- no metadata → `None`

One route test in `tests/api/test_web_routes.py` (or a new `test_billing_routes.py`):
replaying the unpaid event and then the succeeded event credits exactly once.

**Done when.** Credits are granted only for settled payments, and an unknown checkout
never returns 500.

---

## Item 4: no request-size limit before the body is read

**Problem.** FastAPI parses the multipart body before it resolves dependencies, so an
unauthenticated `POST /manuscripts` of any size is spooled to the temp disk before the
401. For a signed-in user, `await file.read()` loads the whole file into RAM, and only then
does `validate_upload` enforce 25 MB. Uvicorn is exposed directly (`infra/Dockerfile`).

**Design.** A pure ASGI middleware (not `BaseHTTPMiddleware`) is the right tool: it sees
the raw `receive` channel, so it can stop a body *while it streams*, chunked uploads
included, before FastAPI buffers anything. It is a cross-cutting concern, and
`api/middleware.py` is where those live.

Why not raise an exception from `receive`: FastAPI catches any error during body parsing
and turns it into `400 There was an error parsing the body`. The middleware therefore
replaces the response itself.

**Steps.** In `api/middleware.py`:
1. `MAX_REQUEST_BYTES = MAX_UPLOAD_BYTES + 1024 * 1024`, with `MAX_UPLOAD_BYTES` imported
   from `adapters/parsing/safety.py` so the two limits cannot drift. The extra MiB covers
   multipart framing.
2. `class BodySizeLimitMiddleware` (plain ASGI callable, `__init__(app, max_bytes)`,
   `async __call__(scope, receive, send)`):
   - Non-HTTP scopes pass through.
   - Fast path: a `content-length` header above the limit → send `413` at once; the app
     never runs.
   - Streaming path: wrap `receive`. Count `len(message["body"])`. Once over the limit, set
     `exceeded = True` and return `{"type": "http.disconnect"}` from then on, which stops
     parsing.
   - Wrap `send`: if `exceeded`, drop the app's response messages and send one
     `413 Request Entity Too Large` (JSON body `{"detail": "upload exceeds 25 MB"}`) instead.
     Guard with a `response_started` flag so headers are never sent twice.
3. `api/main.py`: `app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)`
   *last*, so it is outermost and runs before SlowAPI and the security headers. The 413
   then lacks the security headers; that is acceptable for an error with no HTML. If
   wanted, add them by registering it before `SecurityHeadersMiddleware`.
4. Deploy: add the same cap at the reverse proxy (`client_max_body_size 26m` for nginx,
   or the equivalent) and note it in the README. The app check stays as defense in depth.
5. Optional, same PR: none. `await file.read()` is now bounded at 26 MB, which is fine;
   streaming the upload to S3 would be a larger change with no need at this load.

**Tests.** New `tests/api/test_body_limit.py` using `TestClient`:
- `content-length` of 30 MB with no session cookie → 413, and an injected spy shows no
  route or dependency ran.
- A chunked body (generator yielding 1 MB pieces, 30 of them) → 413.
- A 1 MB upload with a session still reaches the route (existing contract tests cover the
  happy path).

**Done when.** No request larger than 26 MB is parsed, buffered or spooled by the app.

---

## Item 5: sign-in tokens in the access log

**Problem.** The emailed link is `GET /t/{tenant}/auth/click?token=…`, valid for 15
minutes. Uvicorn's access log prints the full path with the query string, so log readers
can sign in as the user.

**Design.** A `logging.Filter` on the `uvicorn.access` logger. This keeps access logs (they
are useful) and redacts only the secret. It is the standard-library extension point for
this, and it sits next to the existing log scrubber in `api/main.py`. Moving the token into
a URL fragment would also work, but it needs client-side JavaScript on the confirm page and
changes the sign-in flow; not worth it here.

**Steps.**
1. In `api/main.py` (beside `_scrub_manuscript_content`):
   ```python
   _SECRET_QUERY = re.compile(r"(?<=[?&]token=)[^&\s]+")

   class _RedactTokens(logging.Filter):
       """Sign-in links carry a live token in the query string."""
       def filter(self, record: logging.LogRecord) -> bool:
           if isinstance(record.args, tuple):
               record.args = tuple(
                   _SECRET_QUERY.sub("REDACTED", a) if isinstance(a, str) else a
                   for a in record.args
               )
           return True

   logging.getLogger("uvicorn.access").addFilter(_RedactTokens())
   ```
   Uvicorn configures its loggers before it imports the app, so the filter added at import
   survives its `dictConfig`.
2. README, deploy section: the reverse proxy must not log query strings for `/t/*/auth/click`
   (nginx: a `log_format` without `$args`, or `access_log off` on that location).

**Tests.** A unit test builds a `LogRecord` with uvicorn's access-log argument shape
`(client, "GET", "/t/x/auth/click?token=abc.def&x=1", "1.1", 200)` and asserts the rendered
message contains `token=REDACTED&x=1` and not `abc.def`.

**Done when.** No token appears in the app's logs. Check by running the app, opening a dev
sign-in link and reading the console.

---

## Item 7: `max_jobs_per_worker` does nothing

**Problem.** The setting is read only by a test. `Worker.run_forever` runs one job at a
time.

**Design.** Delete it (YAGNI). Horizontal scaling (more worker processes, each claiming with
`FOR UPDATE SKIP LOCKED`) already works and is the simpler model. In-process concurrency
would share one event loop between CPU-bound PDF parsing and heartbeats, and the
`to_thread` call would compete with it.

**Steps.**
1. Remove `max_jobs_per_worker` from `config.py` and its assertion in `tests/test_config.py`.
2. README: one sentence under the worker or deploy notes, "Run more worker processes to
   process jobs in parallel; each runs one job at a time."

**Done when.** `grep -rn max_jobs_per_worker` returns nothing.

---

## Item 8: dead code, including one unfenced write

**Problem and design.** Delete code nobody calls. The unfenced `update_state` is the
priority: it is a public method that bypasses the lease fencing the job runner relies on.

**Steps.**
1. `adapters/persistence/repositories.py`: delete `AnalysisJobRepository.update_state`.
   Delete the matching fake in `tests/services/fakes.py:111`.
2. `api/deps.py`: delete `get_pass_result_repo` and the `PassResultRepository` import if it
   becomes unused.
3. `adapters/parsing/docx.py` and `pdf.py`: delete `parse_docx` and `parse_pdf`. Rewrite
   their tests in `tests/adapters/test_parsing.py` to call `parse_manuscript`, which
   validates and dispatches the same way. Update the two `extract_*` docstrings that point
   at them ("Callers must have run validate_upload (parse_manuscript does)").
4. `ReportRepository.create`: drop the `summary_blob_key` parameter and its fake parameter
   (`tests/services/fakes.py:198`). Keep the column: dropping it needs a migration and
   gains nothing.

**Tests.** The existing suite, rewritten where listed.

**Done when.** Each removed name returns no hits in `grep -rn` over `src tests evals`.

---

## Item 9: four unused dependencies

**Steps.**
1. Remove `prometheus-client`, `sentry-sdk[fastapi]`, `opentelemetry-sdk` and
   `opentelemetry-instrumentation-fastapi` from `pyproject.toml`.
2. Leave PLAN.md's observability section as the plan it is. Add one line there saying
   these get added back with the code that uses them.
3. Rebuild the venv (`pip install -e ".[dev]"`) and run the full CI check list,
   `pip-audit` included.

**Done when.** The image no longer installs them, and CI is green.

---

## Item 10: report download written twice

**Problem.** `api/routes/manuscripts.py::get_report_download_url` and
`api/routes/web.py::download_report` both look up the job, then the report, presign a URL
and write an audit event. They hard-code the expiry separately.

**Design.** An application service (transaction script), the same shape as
`services/uploads.py::submit_manuscript`. The use case lives once; each delivery adapter
only maps the outcome to its format (a JSON envelope or a redirect). Missing data is a
domain error, mapped to 404 in the route, the way `InsufficientCredits` is mapped to 402.

**Steps.**
1. `domain/errors.py`: `class NotFound(EdittaError)`, with `JobNotFound(NotFound)` and
   `ReportNotReady(NotFound)`. Messages stay as they are today ("job not found",
   "report not ready").
2. New `services/reports.py`:
   ```python
   REPORT_URL_EXPIRES_SECONDS: Final = 3600

   @dataclass(frozen=True, slots=True)
   class ReportDownload:
       url: str
       expires_in: int

   async def report_download(*, tenant_id, job_id, actor, jobs, reports, audit, blobs) -> ReportDownload:
       """Report rows have no tenant_id (PLAN.md §3): authorize through the job."""
       ...  # get job (tenant-scoped) -> JobNotFound; get report -> ReportNotReady;
            # presign; audit "download"; return ReportDownload
   ```
3. Both routes call it and map `NotFound` to `HTTPException(404, str(exc))`. The JSON route
   wraps the result in `ReportDownloadResponse`; the web route returns
   `RedirectResponse(download.url)`. Delete `REPORT_URL_EXPIRES_SECONDS` from
   `routes/manuscripts.py`.

**Tests.** New `tests/services/test_reports.py` with the fakes from `tests/services/fakes.py`:
wrong tenant → `JobNotFound`; no report → `ReportNotReady`; success → URL, expiry 3600,
one audit event. The existing route tests
(`test_report_download_before_report_exists_is_not_found`) must stay green unchanged.

**Done when.** One function owns the download rule, and both routes are under ten lines each.

---

## Item 13 (conditional): legacy credit reservation in the job runner

**Problem.** `JobRunner._reserve_credit_once` exists only for jobs queued before credits
were reserved at upload (PR #2). Once none of those is pending, it is dead code, costs one
transaction per job, and is a second place that can charge a credit.

**Gate.** Run this against production (or confirm the app was never deployed before PR #2):
```sql
SELECT count(*) FROM analysis_jobs j
WHERE j.state NOT IN ('completed', 'dead', 'cancelled')
  AND NOT EXISTS (
    SELECT 1 FROM credit_ledger l WHERE l.idempotency_key = 'job:' || j.id || ':reserve'
  );
```
Proceed only when it returns 0. Otherwise wait until those jobs finish.

**Steps.**
1. `services/jobs.py`: delete `_reserve_credit_once`, its transaction in `_execute`, and the
   "Credits:" paragraph's sentence about the fallback.
2. `domain/credits.py::reservation_key` docstring: drop "and reserves only for jobs created
   before…".
3. `_dead_letter` stays as it is: it refunds what the ledger says was reserved, which is
   still correct.
4. Tests in `tests/services/test_jobs.py` that relied on the runner reserving
   (`test_happy_path_completes_writes_report_and_reserves_one_credit`,
   `test_missing_manuscript_does_not_mint_a_credit`,
   `test_exhausted_job_without_reservation_gets_no_refund`): seed the reservation in the
   fake ledger the way `submit_manuscript` would, and rename the first one to drop
   "reserves". Add one test: a job with no reservation completes and appends nothing to
   the ledger.

**Done when.** The ledger is written by exactly two places: upload (reserve) and dead
letter (refund), plus the Stripe webhook (purchase).

---

## Summary of patterns by module

| Module | Items | Pattern | Why |
|---|---|---|---|
| `domain/errors.py` | 1, 2, 10 | Typed error hierarchy; error as carrier of a value object (`usage`) | Retry policy already keys on class flags; extends the existing rule. |
| `adapters/llm/*` | 1, 2, 11 | Anti-corruption layer | Vendor errors and text stop at the adapter. |
| `adapters/llm/resilience.py` | 12 | Circuit breaker, correct half-open state | Matches the documented intent with one line. |
| `services/jobs.py` | 2, 13 | Decorator over the `LLMClient` port | Records failed spend for every call without touching the shared analysis core. |
| `services/billing.py` | 3 | Pure policy function + value object | Keeps Stripe logic DB-free and testable with dicts. |
| `services/reports.py` | 10 | Application service (transaction script) | Same shape as `submit_manuscript`; one use case, two delivery adapters. |
| `api/middleware.py` | 4 | Pure ASGI middleware | Only layer that can stop a body while it streams. |
| `api/main.py` | 5 | `logging.Filter` | Standard extension point; redacts the secret, keeps the log. |
| config, deps, repositories, parsing, pyproject | 7, 8, 9 | Deletion (YAGNI) | Smallest correct change. |
| `tests/conftest.py` | 6 | Fixture-based isolation | Tests stop depending on the machine they run on. |

## Size

Roughly +260 lines (about two thirds of them tests) and −75 lines, minus 4 dependencies.
No database migration. Two deploy-side changes: the Stripe webhook event list (item 3) and
the reverse-proxy body limit and log format (items 4 and 5).
