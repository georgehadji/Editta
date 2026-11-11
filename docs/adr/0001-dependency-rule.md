# ADR 0001: The dependency rule is checked by the build

Status: accepted, 2026-11-11

## Context

`docs/PLAN.md` §2 says the code is hexagonal: a pure domain, ports as `Protocol`s, adapters
behind them, and a few composition roots that wire concrete classes. Until now that was a
convention. The architecture audit of 2026-11-11 found it mostly true, and found the places
where it was not (services and routes naming adapters directly). Nothing stopped a new
import from breaking it.

## Decision

`import-linter` runs in CI (`lint-imports`, before pytest) with the contracts in
`pyproject.toml`:

| Contract | Rule |
|---|---|
| Domain | imports nothing above it (passes, services, adapters, api, worker, cli) |
| Passes | depend on the domain only |
| Adapters up | do not import services, api, worker or cli |
| Adapters sideways | the seven adapter packages do not import each other |
| Services | do not import adapters, api, worker or cli |
| Routes | do not import adapters |

The composition roots (`api/deps.py`, `api/main.py`, `worker/main.py`, `worker/retention.py`,
`worker/ops_check.py`, `cli.py`) are the one place allowed to name concrete adapters. No
contract covers them.

Two kinds of exception are listed in `ignore_imports`:

* **One documented sibling edge:** `adapters.export.annotated_docx` reuses the parser's
  text helpers so the exporter and the parser compute the same offsets.
* **Known violations** (services and routes naming adapters). These are fixed by
  `docs/ARCH_REMEDIATION_PLAN.md` item 6. Each fix deletes its line. `ignore_imports` fails
  on an entry that no longer matches an import, so the list can only shrink.

## Consequences

* A forbidden import fails the build with the import chain, so it is caught in review
  rather than in an audit.
* A new violation cannot hide among the known ones: it is not on the list.
* Moving or renaming a module that appears in the list means editing the list. That is
  the point: the exception is visible.
* The contracts check imports, not behaviour. A service that calls an adapter through a
  parameter it was handed is fine; that is dependency injection.
