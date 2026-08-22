# Incremental `src/` and `tests/` package-seam migration map

**Status:** Proposed execution map for [issue #1606](https://github.com/CIRWEL/unitares/issues/1606); no module move is authorized by this document alone.  
**Snapshot:** `origin/master` at `0632fa93` on 2026-08-22.  
**Scope:** Organize the existing Python package without changing runtime behavior, public imports, CLI entry points, or the top-level package name `src`.

## Outcome

Do not perform a tree-wide rename. Land one domain seam per PR, leave the old
import path as a compatibility surface, and move only that domain's tests with
it. The first two implementation seams should be resident validation and EISV
formatting/validation. The monitor components are a useful third seam after the
first two establish the compatibility-shim pattern.

Renaming the top-level `src` package into a publishable `unitares` namespace is
a separate distribution migration. `pyproject.toml` deliberately packages
`src`, `src.*`, `config`, and `governance_core`; changing that contract while
also sorting modules would combine two unrelated failure modes.

## Current layout and coupling

The issue was filed with 123 files directly under `src/` and 617 directly under
`tests/`. On this snapshot there are:

- 125 direct files under `src/`: 123 Python modules plus two data files;
- 244 Python files below existing `src/` subpackages;
- 679 Python files directly under `tests/`, versus 33 below test subdirectories.

The repo already has useful package anchors. The largest are
`src/mcp_handlers/` (131 Python files), `src/db/` (18), `src/identity/` (16),
`src/services/` (15), `src/http_routes/` (14), and `src/grounding/` (10).
New seams should join or complement these packages, not create competing
taxonomies for code that is already organized.

The remaining top-level modules cluster as follows. Counts are an inventory,
not an ownership declaration: a file is assigned to one family for counting,
while cross-family imports remain visible in the client columns.

| Flat family | Modules | LOC | `src` clients outside family | Test clients | Script clients |
|---|---:|---:|---:|---:|---:|
| Identity, lifecycle, residents | 19 | 7,181 | 44 | 50 | 5 |
| Behavioral EISV, monitor, calibration | 43 | 16,117 | 33 | 109 | 11 |
| Knowledge and retrieval | 8 | 2,301 | 16 | 25 | 9 |
| Dialectic protocol/storage | 3 | 2,232 | 8 | 26 | 0 |
| Transport, tool, and API edges | 22 | 7,333 | 35 | 84 | 7 |
| Platform, operations, cross-cutting | 27 | 9,333 | 171 | 70 | 2 |

The platform count is intentionally unattractive as a first extraction:
`logging_utils.py`, persistence, background tasks, configuration, process
control, and audit code are cross-cutting rather than a coherent package seam.
Likewise, the identity and dialectic families are live authority/state surfaces;
their directory shape is less urgent than their correctness and should not be
the proving ground for compatibility moves.

### Method

File and LOC counts come from `find` plus line counts over `*.py`. Coupling is a
Python AST import scan over `src/`, `tests/`, and `scripts/`, counting files that
import each top-level `src.<module>`. A separate `rg` scan covered string-based
patch targets, documentation references, and script entry points that an AST
import scan does not see. Dynamic plugin imports and external repositories are
not proven absent; compatibility shims remain required for that reason.

## Compatibility contract for every extraction

Each extraction PR must satisfy all of these conditions:

1. **Canonical implementation lives only at the new path.** The old module is
   a thin compatibility shim; do not keep two editable copies.
2. **Existing imports keep working.** Add an explicit old-path/new-path import
   contract test. For pure APIs, exported objects must be identical (`old.X is
   new.X`) and `__all__` must be explicit.
3. **Mutable module state is not copied.** If callers patch or mutate module
   globals, use a module-alias shim or another tested mechanism that makes both
   paths reach the same module state. A plain `from new import *` is not enough.
4. **Internal imports move to the canonical path in the same PR.** The shim is
   for compatibility, not a permanent source of new internal dependencies.
5. **CLI and file-relative behavior stays stable.** Preserve supported
   `python -m ...` behavior, diagnostic-script imports, package data paths, and
   `Path(__file__)` semantics, or document evidence that a surface is unused
   before changing it.
6. **Tests move with the domain.** Move only matching tests into the parallel
   test package and add the import-contract test there. Pytest collection count
   must not fall.
7. **Coverage does not regress.** Run the focused domain tests and the normal
   repository gate. Record total collected tests and coverage before/after;
   both the coverage percentage and the moved modules' executed lines must be
   no lower after the extraction.
8. **One seam per PR.** No behavior cleanup, API deprecation, broad formatting,
   or unrelated test relocation rides with a package move.

Compatibility paths have no automatic removal date. Removing one is a separate
API decision supported by measured consumers, not cleanup bundled into issue
#1606.

## Seam 1 — resident validation

**Why first:** Three modules, 457 LOC, no production `src` client outside the
group, three direct test files, and three diagnostic-script clients. The group
has a clear dependency direction: invocation → runner → envelope/model.

Proposed canonical layout:

```text
src/evaluation/
  __init__.py
  resident_validation/
    __init__.py
    model.py          # ResidentProfile, tick envelope, process-update kwargs
    runner.py         # JSONL state and canary tick sequencing
    invocation.py     # local lock, bounded supervised invocation, audit row

tests/evaluation/resident_validation/
  test_model.py
  test_runner.py
  test_invocation.py
  test_import_contract.py
```

Compatibility mapping:

| Existing import | Canonical import |
|---|---|
| `src.resident_validation` | `src.evaluation.resident_validation.model` |
| `src.resident_validation_runner` | `src.evaluation.resident_validation.runner` |
| `src.resident_validation_invocation` | `src.evaluation.resident_validation.invocation` |

PR boundary:

- move implementation and matching tests only;
- update the three `scripts/diagnostics/resident_validation_*.py` clients to
  canonical imports;
- keep the three old modules as explicit re-export shims;
- keep default state/audit paths and JSON wire shapes byte-identical;
- update the fleet-identity leak guard's source-path inventory so moving the
  model cannot silently remove that enforcement coverage;
- run the three resident-validation test modules, diagnostic `--help` smoke
  tests, package/wheel import checks, and the repository gate.

This PR should not change resident authority, cadence, state files, or launchd
configuration.

## Seam 2 — EISV formatting and validation

**Why second:** Two modules, 524 LOC, one runtime importer of the validator,
one glossary dependency, and a small direct test surface. They are an I/O
contract edge, not the behavioral or ODE decision engine.

Proposed canonical layout:

```text
src/eisv/
  __init__.py
  formatting.py
  validation.py

tests/eisv/
  test_formatting.py
  test_validation.py
  test_completeness.py
  test_import_contract.py
```

Compatibility mapping:

| Existing import | Canonical import |
|---|---|
| `src.eisv_format` | `src.eisv.formatting` |
| `src.eisv_validator` | `src.eisv.validation` |

PR boundary:

- move only formatting/types and response validators;
- keep `governance_core`, behavioral state, monitor math, telemetry, and policy
  code out of this package move;
- update `src/mcp_handlers/updates/enrichments.py` to the canonical validator;
- preserve old-path imports and the `IncompleteEISVError` class identity;
- audit `scripts/diagnostics/check_eisv_completeness.py`, which currently names
  the old filenames as enforcement-infrastructure exclusions;
- preserve or explicitly delegate the existing `python -m src.eisv_validator`
  example path;
- run formatting, validation, completeness, utility, and core-update tests,
  then wheel import and repository gates.

This PR should not change metric ranges, glossary wording, response shapes, or
which responses require validation.

## Seam 3 — monitor components, after shim proof

**Why third, not first:** The 13 `monitor_*.py` modules form a recognizable
2,269-LOC family. `governance_monitor.py` is the main owner, with three handler
clients of result/prediction constants. The production boundary is narrow, but
tests patch module globals such as
`src.monitor_calibration.calibration_checker`. A naive re-export shim would
make those patches hit the shim while the moved function reads globals from the
canonical module.

Target `src/monitor/` with one canonical module per current suffix, while
leaving `src/governance_monitor.py` as the orchestrating public facade. Before
moving code, add a compatibility proof that:

- old and new imports share state and exported object identity;
- the existing old-path monkeypatches still affect execution, or all in-repo
  patch clients move to the canonical path with an explicit decision that
  external monkeypatch paths are not API;
- prediction-registry and monitor state are never initialized twice;
- decision, result, risk, void, and calibration focused tests remain green with
  unchanged fixtures.

Because this is policy-adjacent code, do not combine its extraction with EISV
semantic changes. If the shim proof is awkward, stop after seams 1 and 2 and
revisit the package boundary instead of weakening the compatibility contract.

## Deferred families

- **Knowledge/retrieval:** coherent, but active search work can touch handlers,
  storage, embeddings, and retrieval together. Start only when no in-flight KG
  PR owns that surface.
- **Identity/lifecycle:** already has `src/identity/` and
  `src/mcp_handlers/identity/`; the remaining flat modules include compatibility
  and shared-state surfaces. Treat docs and implementation as the repo's
  single-writer identity surface before any move.
- **Dialectic:** only three flat modules, but 2,232 LOC and active PostgreSQL,
  protocol, and authorization semantics. Navigation value does not justify a
  move-only PR while those semantics are evolving.
- **Transport/tool/API:** existing `services`, `http_routes`, and `gateway`
  packages are the anchors. Audit entry points and deployment imports before
  assigning the remaining top-level modules; do not invent a second transport
  hierarchy.
- **Platform/operations:** split by a real consumer boundary first. A package
  named `utils` or `operations` containing all 27 cross-cutting modules would
  recreate the flat directory one level lower.

## Per-PR evidence template

Every extraction PR should include:

```text
Seam:
Old imports preserved:
Canonical imports introduced:
Tests moved / collection before → after:
Focused tests:
Full gate and coverage before → after:
Wheel-standalone import check:
String/dynamic import audit:
Behavior changes: none
Compatibility shims retained:
```

## Issue close condition

Issue #1606 can close when this map is accepted and at least the first two seams
have landed as separate PRs with import-contract tests, stable collection, and
no coverage regression. The monitor seam may remain a follow-up if its
module-global compatibility cost outweighs the navigation benefit. Broadly
moving the rest of `src/` or `tests/` is not a close condition.
