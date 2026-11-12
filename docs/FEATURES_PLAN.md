# Editta — Publisher Value Features: Build Plan

Status: draft, 2026-11-06. Nothing here is built yet. Read with [PLAN.md](PLAN.md); every
rule there still holds, above all the non-goals (intro) and the product safety rails (§5).

Five features:

| # | Feature | LLM cost | Own credit price? |
|---|---|---|---|
| F1 | Findings as Word comments in the author's own `.docx` | none (pure code) | no |
| F2 | Acquisitions brief for submitted manuscripts, with overnight batch mode | 1 Sonnet call, half price in batch | yes, lower |
| F3 | Catalogue metadata: back-cover drafts, keywords, Thema codes, ONIX fragment | 1 Haiku call | no (part of the editorial job) |
| F4 | Consistency checks: name variants, mixed typography, date contradictions | mostly code, 1 small Haiku call | no |
| F5 | Flags for legal and permissions review | 1 pass that reads the existing cache | no |

Order of work is in §9. F1 and F4 cost nothing or almost nothing to run and come first.

---

## 0. What the code does today (facts this plan builds on)

- One fixed product: `services/analysis.py` runs `ANALYSIS_PASSES` (language, structure,
  metadata, integrity), then synthesis. `JobRunner._execute` (`services/jobs.py`) hardcodes
  that list, checkpoints each pass under `jobs/{job_id}/`, and exports one DOCX report.
- Every finding is quote-anchored (`domain/quotes.py`): `anchor_findings` keeps a finding
  only if its quote identifies one passage. It answers yes/no; it does not return *where*.
- `extract_docx` keeps non-empty body paragraphs only, joined with `\n`. Tables, footnotes,
  headers and text boxes are not read.
- Passes that share a model, schema and effort share one prompt cache; a call with a
  different schema cannot read it (`passes/base.py`).
- `reports` has one row per job (`blob_key`, unused `summary_blob_key`) and no
  `tenant_id`; it is authorized through its job. Retention deletes the `jobs/{job_id}/`
  prefix and the rows (`services/retention.py`).
- Credits: one reserved per job at upload (`DEFAULT_JOB_COST_CREDITS = 1`).
- Installed: python-docx 1.2.0 (has `Document.add_comment`), rapidfuzz 3.14
  (`fuzz.partial_ratio_alignment` returns match offsets). `pyproject.toml` still says
  `python-docx>=1.1`; F1 raises it to `>=1.2`.

---

## 1. Shared foundation (built once, used by all five)

Today the pipeline knows one product and one output file. The features need two products
(editorial job, acquisitions brief) and several output files per job. Two small seams do
it, in the style the code already uses (a frozen registry of strategies, like
`adapters/parsing.EXTRACTORS`).

### 1.1 Job profiles (Strategy + Registry)

```python
# domain/profiles.py — pure data, no I/O
class JobKind(StrEnum):
    EDITORIAL = "editorial"
    ACQUISITIONS_BRIEF = "acquisitions_brief"

@dataclass(frozen=True, slots=True)
class JobProfile:
    kind: JobKind
    analysis_passes: tuple[AnalysisPass, ...]   # cached groups, PassFindings schema
    checks: tuple[DeterministicCheck, ...]      # F4: pure code, no tokens
    extra_calls: tuple[ExtraCall, ...]          # own schema, uncached: synthesis, brief, catalogue
    artifacts: tuple[ArtifactKind, ...]         # what the job exports
    credit_cost: int
    batch_eligible: bool

PROFILES: Mapping[JobKind, JobProfile] = MappingProxyType({...})
```

- `JobRunner._execute` reads `PROFILES[job.kind]` instead of `ANALYSIS_PASSES`. Ordering,
  caching, checkpoints and fencing stay as they are; only the work lists come from the
  profile. The CLI pipeline (`services/pipeline.py`) takes a `kind` the same way.
- `preflight` takes the profile. `project_remaining_cost_usd` takes a list of uncached
  extra-call models instead of the single `synthesis_model`, so the budget ceiling still
  runs before the first billable call for every profile.
- `credit_cost` replaces the constant at upload; `require_credits` already takes an amount.
- Why a registry and not `JobRunner` subclasses: the runner's job (lease, fencing,
  checkpoint, failure policy) is the same for every product. Only the work list differs.

### 1.2 Several artifacts per job

- `ArtifactKind` enum: `REPORT`, `ANNOTATED_MANUSCRIPT`, `BRIEF`, `CATALOGUE_ONIX`,
  `CATALOGUE_DOCX`. Each maps to a fixed blob key `jobs/{job_id}/{kind}.{ext}` and a fixed
  content type. Keys are built from the enum, never from request input.
- `ArtifactExporter` protocol (Strategy): `kind`, `content_type`,
  `build(ctx: ExportContext) -> bytes | None` (`None` = not applicable, e.g. comments for
  a PDF upload). `ExportContext` is frozen: manuscript, original bytes and format, pass
  results, synthesis, extra-call outputs.
- Each `build` runs in `asyncio.to_thread` with a timeout, as parsing does: some exporters
  walk attacker-shaped XML.
- Invariant kept: all artifact blobs are written first; then all `reports` rows and
  `COMPLETED` commit in one fenced transaction. A job is never complete with a missing
  artifact, and a worker that lost its lease writes no rows.

### 1.3 Migration 0006

> **As built (PR 1):** migration 0006 is `reports.kind` and the `(job_id, kind)` unique
> constraint only. `analysis_jobs.kind` ships with the job profiles (PR 6) and
> `analysis_jobs.batch_id` with batch mode (PR 7), each in its own migration, so no column
> exists before something writes it. `ArtifactKind` has the members that exist
> (`REPORT`); each feature PR adds its own. `reports.kind` has no CHECK: only the worker
> writes it, from the enum, and the download route validates the enum first.

- `analysis_jobs.kind text not null default 'editorial'` + CHECK on the enum values.
- `analysis_jobs.batch_id text null` (F2 batch mode).
- `reports.kind text not null default 'report'`; replace the unique constraint on
  `job_id` with unique `(job_id, kind)`. Existing rows become `kind = 'report'`.
- No RLS change: `reports` is still authorized through its job; `analysis_jobs` is already
  tenant-scoped with FORCE RLS.
- `ReportRepository.get_by_job_id` gains `kind`. `delete_for_jobs` already deletes every
  row of the jobs, so retention needs no change. Add an integration test that a job with
  every artifact kind is fully purged, blobs and rows, because F1 and F2 artifacts contain
  manuscript text.

### 1.4 Download

- `GET /manuscripts/jobs/{job_id}/artifacts/{kind}` with `kind: ArtifactKind` (FastAPI
  validates the enum; anything else is 422). The web route mirrors it.
- `services/reports.report_download` gains `kind` and keeps its checks: the job belongs to
  the tenant, the row exists, a short presigned URL, an audit event (subject includes the
  kind).
- The presigned URL sets `ResponseContentDisposition: attachment; filename*=UTF-8''…` from
  the sanitized original filename plus a suffix (`…-editta-comments.docx`). `attachment`
  stops a browser from rendering an uploaded file inline.
- The old `/jobs/{job_id}/report` route stays as an alias for `kind=report`.

---

## 2. F1 — Findings as Word comments in the author's file

Goal: the editor opens the author's own `.docx` and sees each finding as a Word comment on
the passage it is about. Not one character of the text changes.

### 2.1 Design

The functional core (pure, `domain/`) decides *where*; the adapter (`adapters/export/`)
only writes XML.

1. **Locate, not just anchor** — extend `domain/quotes.py`:
   - `normalize_with_map(text) -> (normalized, index_map)`, where `index_map[i]` is the
     original offset of normalized character `i`. Same folding rules as `normalize_text`;
     `normalize_text` becomes `normalize_with_map(text)[0]`, so the two can never disagree.
   - `locate_quote(quote, source) -> Span | None`, same rules as `_anchored`: a short quote
     must be the one exact whole-word match; a long quote uses
     `fuzz.partial_ratio_alignment` with the same 0.9 threshold. Returns original offsets.
   - `_anchored` becomes `locate_quote(...) is not None`, so "anchored" and "placeable"
     are one rule, not two.
2. **Map spans to paragraphs** — `domain/placement.py`, pure:
   `place_findings(paragraph_texts, findings) -> Placement(placed, unplaced)`.
   The paragraph list must be exactly what `extract_docx` reads. Move the paragraph walk
   into one helper, `docx_body_paragraphs(document)`, used by both `extract_docx` and the
   exporter, so offsets cannot drift between them.
3. **Write comments** — `adapters/export/annotated_docx.py`,
   `AnnotatedManuscriptBuilder` (Builder, like `ReportDocxBuilder`):
   - Open the original bytes and run `validate_upload` again (cheap; the bytes came back
     from storage).
   - For each placement: split runs at the span edges, copying each run's `w:rPr`
     unchanged, then `document.add_comment(runs, text=…, author="Editta", initials="ED")`.
   - Comment text: `[major] Γλώσσα — <issue>` / `Πρόταση: <suggestion>`, plus one fixed
     line saying the comment is machine-generated. Strip XML-illegal control characters
     from model text (lxml raises on them) and cap the length.
   - Findings that cannot be placed are listed in a report appendix ("not placed in the
     file") and counted in the logs. A position is never guessed.

### 2.2 Rules that keep the author's text intact

- Leave unplaced any span that touches a field, hyperlink, `w:ins`/`w:del` tracked change
  or content control, or crosses a paragraph boundary. A few correct comments beat many
  misplaced ones. Measure the placed rate in evals (§8); widen these rules only if it is
  low.
- The author's own comments and tracked changes are kept as they are.
- Golden test (the key safety test): for every eval DOCX, the concatenated `w:t` text of
  the output equals the input's, and `extract_docx(output).text == extract_docx(input).text`.
  A second test diffs the package: only `document.xml`, the comments parts, rels and
  `[Content_Types].xml` may change.
- The output is never `.docm`, and nothing is added except comments. Editta does not
  "clean" the author's file; that would change it.

### 2.3 Scope

- `.docx` uploads only. For a PDF the exporter returns `None` and the UI says comments
  need a Word file. PDF annotations are a separate, later decision.
- Part of the editorial profile. Zero tokens; CPU only, bounded by the exporter timeout.

> **As built (PR 2):**
> - `locate_quote(quote, source: MappedText)` takes the output of `normalize_with_map`, so
>   many quotes are located against one normalization. `normalize_text` is **not** made a
>   wrapper of `normalize_with_map`: it runs on the event loop once per pass and the mapped
>   version is several times slower. A test pins that both give the same text on tricky and
>   random input, and `place_findings` re-checks it at run time and places nothing on a
>   mismatch.
> - `_anchored` is now `_locate(...) is not None`, so "anchored" and "placeable" are one rule.
> - The map keeps a start *and* an end offset per character (`MappedText`), because NFC and
>   case folding can turn several characters into one or one into several.
> - `place_findings` returns finding indexes, not `Finding` objects, so two equal findings
>   from different passes stay distinct.
> - The exporter never fails the job: any error inside it logs the exception type and yields
>   no file. The report is still delivered.
> - Comment labels are Greek, in one table (`PASS_LABELS`), pending open question 4.
> - **Not built yet:** the "not placed in the file" appendix in the report (the report and
>   the commented file are separate exporters; the count is logged as `export.annotated`),
>   and a hint in the UI that a PDF gets no comments. Neither blocks the feature.
> - **Known limit:** a field that starts in one paragraph and ends in a later one is not
>   tracked across the break (marked `shortcut:` in `annotated_docx.py`).

---

## 3. F2 — Acquisitions brief (+ overnight batch mode)

Goal: a one-page, neutral description of a submitted manuscript, so an acquisitions editor
can decide what to read in full. Descriptive only: no score, no ranking, no accept/reject.

### 3.1 The brief (synchronous first)

- Profile `ACQUISITIONS_BRIEF`: no analysis passes, F4 typography check only, one extra
  call `BriefCall` on Sonnet 5.5, artifact `BRIEF`, `credit_cost` set by the business
  (§10), `batch_eligible=True`.
- Schema `AcquisitionsBrief` (frozen Pydantic, structured output): `synopsis`, `structure`
  (list of `{part, one_line}`), `themes`, `genre_and_form`, `intended_readership`,
  `tone_and_register`, `content_notes` (sensitive subjects, stated as facts), `evidence`
  (list of `{claim, quote}`).
- Facts computed by code, not by the model: word count, chapter count from headings,
  monotonic or polytonic (F4 typography check).
- Safety rail, structural like synthesis's: the schema has no field that implies a verdict.
  A unit test fails if any field name matches
  `score|rating|recommend|verdict|accept|reject|grade|rank`. The prompt says the reader is
  an editor deciding what to read, and forbids judging quality or comparing with other
  books.
- `evidence` quotes go through `locate_quote`; unanchored ones are dropped. Prose fields
  cannot be anchored, so the brief carries a fixed line: "summary generated from the text;
  check the quoted evidence".
- `BriefDocxBuilder`: one page, fixed layout, Greek labels.
- Bulk upload: `POST /manuscripts/bulk` takes up to N files (config, e.g. 20), runs each
  through the existing upload gate, and reserves the credits for all of them in one
  transaction under the ledger lock uploads already take. All or nothing: with too few
  credits, nothing is stored.

### 3.2 Overnight batch mode

> Before building this, read docs/STATE_MACHINES_PLAN.md item 9: model `AWAITING_BATCH`
> with explicit `batch_submit_token` and `batch_id` columns, not a `'pending:<uuid>'` marker.

PLAN.md Phase 5 planned this behind the `LLMClient` port. A batch result arrives hours
later, so it does not fit `analyze()` (request → response). It gets its own port:

```python
class BatchLLMClient(Protocol):
    async def submit(self, requests: Sequence[BatchRequest]) -> str: ...          # batch id
    async def poll(self, batch_id: str) -> BatchStatus: ...                        # pending | ended
    async def results(self, batch_id: str) -> Mapping[str, LLMResult[Any]]: ...    # by custom_id
    async def delete(self, batch_id: str) -> None: ...
```

Flow (state machine in `services/jobs.py`; new state `AWAITING_BATCH` in `domain/states.py`):

1. Claim → preflight (priced at batch rates) → `submit` with
   `custom_id = f"{job_id}:{call_name}"` → store `batch_id` in a fenced write → state
   `AWAITING_BATCH` → `defer` 15 minutes.
2. Next claim → `poll`. Pending: `defer` again. Ended: fetch results, checkpoint them like
   pass results, `delete` the batch, continue to export.
3. Idempotency: a worker that dies after `submit` but before storing the id would submit
   twice on retry. Store a marker first (`batch_id = 'pending:<uuid>'`); on retry, look up
   recent batches by `custom_id` before submitting again.

Decisions this needs:

- **Attempts.** Every claim increments `attempt`, and 3 attempts dead-letter the job.
  Polling must not use them up: a claim of a job in `AWAITING_BATCH` does not increment
  `attempt` (one condition in the claim UPDATE). A wall-clock limit replaces it (e.g. 26
  hours since submit), then DEAD with refund.
- **Provider.** Batches go to Anthropic directly. With only an OpenRouter key,
  batch-eligible jobs run synchronously. The composition root decides; services do not
  know.
- **Data retention — check before building.** The provider stores batch inputs and results
  until the batch is deleted or expires. Check Anthropic's current terms for the Message
  Batches API, including whether any zero-data-retention arrangement the house relies on
  covers it. Delete each batch right after fetching its results, and tell the house in
  plain words what is stored, where, and for how long.
- **Budget.** A batch price multiplier (0.5) in `domain/budget.py`, applied in the
  projection only for batch jobs.

---

## 4. F3 — Catalogue metadata ready to use

Goal: draft copy and subject metadata the house can paste into its systems.

### 4.1 Content

- `CatalogueCall`: one extra call on Haiku 5.5 with its own schema `CatalogueDraft`:
  `back_cover_drafts` (2 variants, 120–180 words each), `short_description` (≤ 350
  characters), `keywords` (≤ 15, Greek), `thema_subjects` (list of `{code, reason}`),
  `thema_qualifiers` (place, time, language; same shape), `audience` (ONIX audience code,
  fixed enum).
- No comparable titles, no author bio, no prizes, no sales claims: the schema has no field
  for them, and the prompt forbids naming any book or person that is not in the text.
- Same verdict-field test as the brief.

### 4.2 Thema codes are validated, not trusted

The model will invent plausible codes. Same principle as quote anchoring: what cannot be
verified is dropped.

- Vendor the official Thema code list from EDItEUR (pinned version, with Greek headings)
  as `src/editta/data/thema/thema-<version>.json`, with its SHA-256 in code. A test checks
  the hash, so the file cannot drift silently. Loaded once, frozen.
- `domain/thema.py`, pure: `validate_subjects(proposed, codes) -> list[ThemaSubject]`
  keeps known codes only, takes the heading from the list (never the model's wording), and
  drops a parent code when its child is present.
- Licence check before shipping (§10).

### 4.3 Output

- `CATALOGUE_ONIX`: an ONIX 3.0 fragment — `<DescriptiveDetail><Subject>` with
  `SubjectSchemeIdentifier` 93 (Thema), `<CollateralDetail><TextContent>` types 02 (short
  description) and 03 (description). Built with `lxml.etree` elements, never string
  concatenation, so model text cannot inject XML. Tested against the ONIX 3.0 XSD
  (vendored under `tests/` only).
- `CATALOGUE_DOCX`: the same content, readable, each draft marked "draft for the editor",
  for houses that fill in Biblionet's forms by hand.
- Part of the editorial profile. Cost ≈ one Haiku read of the manuscript (cents). It
  cannot share the passes' cache (different schema), like synthesis.

---

## 5. F4 — Consistency across the book

Goal: catch what a tired proofreader misses, mostly with plain code.

### 5.1 Deterministic checks (no tokens)

A new seam next to passes: `DeterministicCheck` protocol (`name`,
`run(manuscript) -> list[Finding]`), pure functions in `domain/consistency/`. Findings use
the normal `Finding` shape and become `PassResult(pass_name="consistency")`, so they flow
into the report, synthesis and F1 comments with no special path.

| Check | How | False-positive guard |
|---|---|---|
| Name variants | Capitalized tokens not at sentence start; group by accent-folded, final-sigma-folded stem; cluster with rapidfuzz distance ≤ 1 for stems of ≥ 5 characters | Greek names decline (Γιώργος / Γιώργου / Γιώργο): strip a fixed list of case endings before grouping; report only stems that differ. Test: zero declension-only clusters on the eval essays |
| Mixed typography | Counts of `« »` vs `" "`, `—` vs `–` in dialogue, Greek question mark (U+037E) vs Latin `;`, ano teleia forms, monotonic vs polytonic accents | Report only when the minority form occurs ≥ 3 times and < 20% (a deliberate mix is not a slip) |
| Number and date formats | `1922` vs `’22`, `5%` vs `5 %`, digits vs words for small numbers | Same minority rule |

- Each finding needs a quote that anchors. The check picks the shortest surrounding window
  (word, then phrase, then sentence) that occurs once and asserts `locate_quote` finds it
  before emitting. A finding that cannot be made unique is dropped.
- Checks re-run on retry (pure and cheap), so no checkpoint. Ids stay stable because the
  output order is deterministic (sorted by first offset).
- Regexes run on untrusted text: linear patterns only (no nested quantifiers), and the
  whole stage runs in `to_thread` under the parse timeout.

### 5.2 Date contradictions (code extracts, a small model judges)

A contradiction needs meaning ("born 1890" … "died 1885"), so:

1. Code extracts every date-like mention (years 1000–2099, Greek month names, ages) with
   its sentence — a few KB, not the whole book.
2. One Haiku call (`DateConsistencyCall`, `PassFindings` schema) gets only those snippets
   and returns contradictions, each quoting a snippet.
3. Quotes are anchored against the full manuscript as usual.

Cost: a few thousand tokens. Skipped when fewer than 2 date mentions are found.

---

## 6. F5 — Flags for legal and permissions review

Goal: point a human reviewer at passages that may need permission or a legal read. Editta
never decides either question.

### 6.1 Design

- `RightsReviewPass(BaseAnalysisPass)`, `name = "rights"`, model **Sonnet 5.5**,
  `PassFindings` schema, shared effort. It joins the Sonnet group with `IntegrityPass`, so
  it **reads the manuscript from the existing cache** (0.1× input) — the cheapest possible
  new pass. The prefix-invariant test in `tests/passes` covers it automatically.
- Two kinds of flag, named in the finding's `section`, so no schema change is needed:
  - `Άδειες χρήσης`: quotations from other works beyond a short quote — poems, song
    lyrics, epigraphs, long prose excerpts — attributed or clearly quoted.
  - `Πρόσωπα`: negative factual claims about identifiable living people, and private
    details about them (health, family, address).
- Prompt rules: flag for review only; never say a passage infringes, defames or is copied;
  never name a source the author did not name; never look for or guess an unattributed
  source. This keeps it clear of plagiarism detection (a PLAN.md non-goal).
- The report section opens with fixed text, not model text: "For human review — not
  legal advice. Editta does not decide whether permission is needed or whether a statement
  is lawful."
- Severity means "how soon a person should look", as everywhere else.

### 6.2 Data

These findings put statements about real people into the report. They live and die with
the report under the existing retention policy: no new store, no new field.

---

## 7. Security summary

| Risk | Where | Control |
|---|---|---|
| Malicious DOCX reopened for F1 | `annotated_docx.py` | `validate_upload` again (zip-bomb, entry count, path traversal, `.docm` refused); `to_thread` + timeout |
| Author text changed by F1 | run splitting | Text-equality golden test + package diff test (§2.2); unplaceable spans skipped, never guessed |
| Prompt injection steering output | all new calls | Existing `SYSTEM_PREAMBLE`; no tools; structured outputs; quotes anchored; Thema codes validated against the vendored list |
| XML injection via model text | F1 comments, F3 ONIX | lxml element API only; XML-illegal characters stripped; length caps |
| Verdicts creeping in | F2, F3 | Schemas without verdict fields + field-name test; fixed disclaimers; eval checklists |
| Invented books, codes, sources | F3, F5 | No fields for them; Thema list validation; prompt forbids naming unnamed sources |
| Artifact access across tenants | download route | Kind is an enum; key built from the enum; job ownership checked under RLS; short presigned URL; audit event |
| Uploaded content rendered inline | download | `Content-Disposition: attachment` |
| Manuscript text kept too long | F1, F2 artifacts, batches | Everything under `jobs/{job_id}/` is purged by retention (integration test); provider batch deleted after fetch |
| ReDoS in F4 regexes | consistency checks | Linear patterns; whole stage under a timeout |
| Double credit spend in bulk upload | F2 | One transaction under the ledger lock, all or nothing |
| Duplicate batch submission | F2 | Submission marker + `custom_id` lookup before resubmitting |

---

## 8. Tests and evals

Unit (pure, fast):
- `normalize_with_map(t)[0] == normalize_text(t)` as a property test; `locate_quote`
  agrees with the old `_anchored` on the whole existing quote test suite.
- `place_findings`: span inside one run, across runs, across paragraphs (unplaced),
  repeated short quote (unplaced).
- Thema validation; the verdict-field test for every new schema; consistency checks on
  Greek declension fixtures.

Adapter:
- F1 golden tests (§2.2) on the three eval DOCX files, plus fixtures with tracked changes,
  hyperlinks, fields, existing comments and tables.
- ONIX output against the XSD.
- A fake `BatchLLMClient` for submit → poll → results → delete, including a crash between
  submit and storing the id.

Integration (Postgres, runs on CI):
- Migration 0006 up and down; `(job_id, kind)` uniqueness; retention purges every artifact.
- Bulk upload with too few credits stores nothing.
- Claims of a job in `AWAITING_BATCH` do not increment `attempt`.

Evals (`evals/`, live API, paid — run only with the user's approval):
- F1: placed rate = placed / anchored findings; target ≥ 90% on the eval essays.
- F3: share of proposed Thema codes that survive validation; zero foreign book titles.
- F4: precision on the eval essays (each flag checked by hand once, then frozen as a
  checklist).
- F5: a seeded essay with a quoted poem, a song lyric and a claim about a living public
  figure; all three flagged, none phrased as a verdict.
- F2: no verdict language in the brief (forbidden-phrase checklist in Greek and English).

---

## 9. Order of work (one PR each)

| PR | Content | Depends on | Why this order |
|---|---|---|---|
| 1 | Foundation: artifacts, migration 0006, download route | — | Every feature exports files |
| 2 | F1 Word comments | 1 | Biggest daily value, zero tokens |
| 3 | F4 consistency (code checks, then the date call) | `locate_quote` from 2 | Cheap; feeds F1 comments |
| 4 | F5 rights pass | — | Smallest change: one pass in an existing cache group |
| 5 | F3 catalogue + Thema list + ONIX | 1 | Needs the code-list licence answer first |
| 6 | Job profiles + F2 brief (synchronous) + bulk upload | 1 | First second product |
| 7 | F2 batch mode | 6 | Needs the retention answer first; most moving parts |

PR 4 can be built in parallel with PRs 1–3.

---

## 10. Open questions for the house (answer before the PR that needs each)

1. **Credit price of a brief** (PR 6): a full credit is too much for a one-page brief.
   What fraction, or a separate brief pack?
2. **Thema code list** (PR 5): confirm EDItEUR's licence terms for shipping the list inside
   the product, and which version the house's distributors expect.
3. **Batch data retention** (PR 7): after checking the provider's current terms, is
   overnight storage at the provider acceptable for submissions?
4. **Comment language** (PR 2): Greek labels only, or follow a tenant setting?
5. **Rights threshold** (PR 4): what counts as "beyond a short quote" for the house (lines
   of verse, words of prose)? It goes into the prompt as a number.
