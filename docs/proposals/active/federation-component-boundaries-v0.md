# Component boundaries for the federation: public contracts first, extraction second (v0)

**Status:** DRAFT proposal, 2026-10-06. Documentation only. It changes no
runtime behavior, schema, flag, default, threshold, tool, response shape or
registered protocol. It authorizes, at most, the tests-only boundary guard in
[Section 6](#6-first-implementation-the-boundary-guard).
**Basis.** Operator direction, 2026-10-06: "proceed best for federation",
given after a coupling map of the codebase and a component-by-component
narrowing outline in the same session. As in the
[2026-09-25 federation trust record](federation-trust-decisions-2026-09-25.md),
that direction is a delegation with one criterion: the working agent selected
the options below under it, and nothing here claims the operator weighed each
one. Merging is the operator's ratification and keeps every choice reversible
until then.
**Scope of "federation".** As in
[`PRODUCT_DEFINITION.md`](../../PRODUCT_DEFINITION.md): many runtimes sharing
one operator-controlled server and authority domain. Trust across
administrative roots stays unscheduled
([D3a](federation-trust-decisions-2026-09-25.md#d3a-multi-principal-trust-is-not-scheduled));
nothing here wakes it.
**Inputs:** the survival audit's keep/interface/freeze list
([`../../ontology/competitive-survival-audit-2026-09.md`](../../ontology/competitive-survival-audit-2026-09.md));
the EISV/core boundary proposal ([`eisv-core-boundary-v0.md`](eisv-core-boundary-v0.md));
the dormant-capability registry
([`../../operations/dormant-capability-registry.md`](../../operations/dormant-capability-registry.md));
the substitute map and its 2026-10 component comparisons
([`../../ontology/competitive-analysis-2026-09.md`](../../ontology/competitive-analysis-2026-09.md));
and a coupling map measured 2026-10-06 (Section 2).

---

## 1. Problem

A federation member is a runtime that joins the operator's record through
public surfaces: MCP, REST, and the SDK. `PRODUCT_DEFINITION.md` states the
same rule for residents: "ordinary userland: no direct database access, no
imports from Core internals, and no privileged measurement or policy path."

The rule is not enforced, and the core does not yet offer the contracts a
member would need instead. Three consequences follow.

1. **Members reach inside.** Resident code imports Core modules and writes SQL
   against Core tables (Section 2.3). Anything learned from those residents
   about what a federation member needs is contaminated by access a real
   member would not have.
2. **Components cannot be offered separately.** The review record and outcome
   binding are the parts the substitute map rates as differentiated, and the
   parts other systems could plug into. Neither has a public API: callers reach
   into handler internals (Section 2.2).
3. **Nothing can be retired cleanly.** With raw SQL against other components'
   tables spread across dozens of files, removing or replacing a component means
   finding every reader by hand.

## 2. Measured coupling (2026-10-06)

Measured by parsing every `src/**/*.py` import (top-level and function-local)
and grepping `schema.table` literals. Figures are a snapshot, not a contract;
the guard in Section 6 re-measures them.

### 2.1 Overall

- `src/` is about 169k lines; `src/mcp_handlers/` alone about 75k.
- A repository layer exists (`src/db/postgres_backend.py` composes eleven
  mixins), but 43 files outside `src/db/` carry raw SQL against named tables
  and 48 acquire the pool directly. Dialectic has no mixin and owns its pool
  (`src/dialectic_db.py`). Outcome-binding persistence lives inside
  `ToolUsageMixin` (`src/db/mixins/tool_usage.py`).
- There are about 1,250 function-local `from src…` imports against about 850
  top-level ones. Many of the local ones defer what would otherwise be import
  cycles.

### 2.2 Cross-component reach that blocks extraction (verified by reading)

- `_record_outcome_event_inline`, a private function in
  `src/mcp_handlers/observability/outcome_events.py`, is imported by
  `src/mcp_handlers/dialectic/resolution.py:15`,
  `src/mcp_handlers/updates/phases.py` and `src/http_routes/substrate.py:404`.
- `src/mcp_handlers/lifecycle/stuck.py:72-74` constructs a `DialecticSession`
  and calls the dialectic reviewer selection and session save directly.
- The check-in path (`src/mcp_handlers/updates/phases.py`) pulls in identity
  persistence, the knowledge graph and dialectic enforcement.
- Five modules under `src/identity/` query `knowledge.discoveries` with SQL.

### 2.3 Members inside the boundary (verified by reading)

| File | Reach | Kind |
|---|---|---|
| `agents/dialectic_reviewer/host_backends.py`, `reviewer.py` | `src.*` imports | Resident userland: breach |
| `agents/local_resident/runner.py` | `src.local_inference_env` | Resident userland: breach |
| `agents/sentinel/agent.py` | `src.*` imports | Resident userland: breach |
| `agents/chronicler/scrapers.py`, `agents/sentinel/forced_release_alarm.py` | SQL against Core tables | Resident userland: breach |
| `agents/{sentinel,vigil,watcher}/routes.py` | `src.*` imports | Route packs mounted in-process by `src/http_routes/packs.py`: plugin territory, not userland |
| `agents/sdk/src/unitares_sdk/lease_plane/client.py:157` | `src.perf_monitor` | Optional guarded host hook, absent outside the server checkout: not a dependency |

The distinction in the last column matters: a route pack runs inside the server
and is entitled to a declared extension surface; a resident runner is a member
and is not.

### 2.4 Components

| Component | Size (lines) | Fan-in / fan-out within `src/` | Tables read elsewhere | Infra |
|---|---|---|---|---|
| Identity and lineage | ~23.5k | 52 / 36 | `core.identities` in 32 files | Postgres, Redis, AGE edges, lease plane |
| EISV estimation and decision | ~15k + `governance_core` 4.1k | 19 / 74 | `core.agent_state` in 29 files | Postgres, Redis, LLM in enrichment |
| Knowledge graph | ~14.8k | 18 / 23 | `knowledge.discoveries` in 12 files | Postgres FTS; AGE and pgvector optional |
| Dialectic review | ~14.7k + Elixir saga | 11 / 28 | its tables also written by `elixir/lease_plane` | Postgres, LLM, BEAM |
| Audit log | ~2.75k | 36 / 4 | `audit.events` in 28 src and 19 Elixir/agent files | Postgres, Redis |
| Outcome binding | ~2.8k | 13 / 18 | `audit.outcome_events` in 5 files | Postgres only |
| Lease plane | 9.5k Elixir + ~2.9k Python | 12 / 15 | `surface_leases` in 4 src files | BEAM, Postgres |
| Dashboard | ~1.9k Python + ~8.4k JS | 5 / 15 | own tables only | Postgres; reaches others over HTTP |

## 3. Decision proposed

Draw the federation boundary in three rings and make the inner ring offer the
contracts a member needs.

```
members (runtimes, residents, SDK users)          ── public surfaces only
   │  MCP · REST · SDK
   ▼
┌──────────────────────────── core record ─────────────────────────────┐
│ identity + lineage · checkpoint (eisv-core-boundary-v0)               │
│ claims + evidence · review (objection, conditions, resolution)       │
│ outcomes bound to checkpoints/predictions · audit                     │
└───────────────┬───────────────────────────────────────┬──────────────┘
                │ public read/subscribe contract          │ export
                ▼                                          ▼
   subscribers: EISV assessors · KG retrieval      exporters: bundle,
   · dashboard · in-process route packs (plugin)   drr.v1 (dormant)
                │
   optional planes: lease plane / BEAM coordination (Compose profile)
```

Rules, in priority order for the federation:

1. **Members use public surfaces only.** No resident runner imports `src.*` or
   writes SQL against Core tables. Route packs and plugins are in-process
   extensions and use a declared surface (`src/plugin_loader.py`,
   `src/http_routes/packs.py`), not arbitrary internals.
2. **Each core component owns its tables.** Another component reads them
   through the owner's API or mixin, never by raw SQL.
3. **No private cross-component calls.** A function another component needs is
   promoted to that component's public API.
4. **Subscribers depend on the core, never the reverse.** This is
   `eisv-core-boundary-v0`'s rule, generalised beyond EISV.

Extraction to separate packages, and any retirement, comes after the boundary
holds, not before.

## 4. Component dispositions

| Component | Ring | Disposition | Why, for the federation |
|---|---|---|---|
| Identity and lineage | Core | Keep; give it read APIs so the 32 readers of `core.identities` stop using SQL | It is the spine every member binds to |
| Checkpoint | Core | Per `eisv-core-boundary-v0`, after the horizon | The unit members attach records to |
| Review (dialectic) | Core | **First public API:** open, answer, resolve, read; move `lifecycle/stuck.py` and `agent_loop_detection.py` onto it | The differentiated part members can use; its resolution record (`drr.v1`) already exists, dormant |
| Outcome binding | Core | Public `record_outcome()` replacing `_record_outcome_event_inline`; its own mixin. **After the horizon**: its tables feed the 2026-12-01 read | Members must report outcomes without internals |
| Audit log | Core | Keep as the leaf; add read APIs before restricting SQL | Everything writes it; moving it buys nothing |
| Knowledge graph | Subscriber | Keep internal; put retrieval behind an interface so the default and any external recall can be swapped | Recall is crowded (substitute map); attribution is not |
| EISV estimation | Subscriber | Per `eisv-core-boundary-v0` stages, after the horizon | An assessor members can ignore or replace |
| Dashboard | Subscriber | Already reaches others over HTTP; separable at any time, low priority | No federation effect either way |
| Lease plane / BEAM | Optional plane | Propose a Compose profile so a member's install does not require it; any further change reopens the signed Wave 3 decision, which this does not | Coordination is a plane members may opt into |
| Reference residents | Member | Remove `src.*` imports and direct SQL from runners (Section 2.3); keep route packs as plugins | They are the only members that exist today |

## 5. Method

Each component moves through the same four steps, one at a time:

1. **Public API inside the repository:** one module per component, callers
   moved onto it, no behavior change.
2. **Table ownership:** the owner's mixin or API is the only SQL path to its
   tables.
3. **Guard:** the boundary guard (Section 6) records the component's rules, so
   a later change cannot quietly reverse them.
4. **Separation:** only then a package split, plugin entry point or Compose
   profile. The SDK's lease-plane client move is the precedent.

## 6. First implementation: the boundary guard

**Why first.** It changes no runtime code, so it is permitted before the
preservation horizon under the strict sequencing recorded in
`eisv-core-boundary-v0` §12.1. Every later step depends on it to stick.

**Shape.** A script under `scripts/dev/` plus a test, following the ratchet
pattern of `scripts/dev/check_proposals_length.py` and its baseline file:
existing violations are listed in a committed baseline; a new violation fails;
removing one shrinks the baseline and may not grow it back.

**Rules (v0):**

- **B1, member imports.** A non-test file under `agents/` that is not a route
  pack mounted by `src/http_routes/packs.py` must not import `src.*`, except an
  optional import inside a `try` whose failure path returns (the SDK host-hook
  pattern).
- **B2, member SQL.** A non-test file under `agents/` must not contain SQL
  naming a `core.`, `audit.`, `knowledge.`, `lease_plane.` or `effects.` table.
- **B3, private reach.** A module must not import a `_`-prefixed name from a
  module in another component's directory.
- **B4, foreign SQL.** A file outside a component's owned modules must not carry
  raw SQL against that component's tables. The owner map starts with the
  tables named in Section 2.4 and is part of the guard's source.

**Acceptance:** passes on `master` with the baseline as measured; fails when a
violation is added in a mutation test for each rule; adds no runtime import,
flag or behavior. Wiring it into pre-commit and the `Repo Scope Guard`
workflow is part of the same change.

**Out of scope:** fixing any baselined violation, every API in Section 4, and
any package split.

## 7. Sequencing against the preservation horizon

The horizon is the 2026-12-01 registered read, final once its report is merged
and stands 14 days without a filed correction (`eisv-core-boundary-v0` §12.1,
item 6).

- **Before the horizon:** this document; the guard (Section 6); the resident
  runner fixes for B1 and B2; the review API, provided it leaves
  `dialectic.enforcement`'s use in the check-in path untouched; the knowledge
  graph retrieval interface; the lease-plane Compose profile proposal.
- **After the horizon:** the outcome-binding API and mixin; the checkpoint
  spine stages of `eisv-core-boundary-v0`; any change to the check-in path.

Each pre-horizon item is still its own PR with its own review; listing it here
does not authorize it (Section 11).

## 8. Retirement

A usage count does not retire a capability (CLAUDE.md, *Measurement authority*),
and the dormant-capability registry records several "dead" calls that were
wrong. Retirement candidates therefore come only from portfolio decisions:

- **Already frozen by the survival audit:** the in-house agent-message
  transport and wake gate (overlapped by A2A), the governed-effect execute
  half, the dormant agent orchestrator, general model routing and inference
  hosting.
- **Already marked CUT or DECIDE in the registry:** the legacy 384-dimension
  `core.discovery_embeddings` table, the CIRS announce tools, and the plugin
  `register_extra_*` hooks.

Each would move through the same ladder: frozen, then default-off behind a
flag, then removed from the tool surface, then deleted. Every step is a
recorded operator decision. This document moves nothing up the ladder.

## 9. Relation to existing records

- **`eisv-core-boundary-v0`** defines the checkpoint as the core's unit and EISV
  as a subscriber. This document generalises its dependency rule to the other
  components and leaves its stages, gates and dispositions unchanged.
- **`dialectic-resolution-receipt-v0` (`drr.v1`)** is the review record's
  export, wired and dormant. Enabling it, and any alignment with an external
  receipt format such as the Agent Passport System's contest receipts (open
  question 5 of the substitute map), stays behind D3a.
- **`relay-substrate-relayering-v0`** keeps enforcement as an optional adapter;
  the subscriber ring here is where such adapters sit.

## 10. Open operator decisions

1. **Guard strictness.** Ratchet (baseline existing violations, fail new ones),
   as proposed, or fail-closed on all violations from the start.
2. **Route packs.** Keep the reference residents' route packs as in-process
   plugins, as proposed, or move them out of process so every resident is a
   member.
3. **Review API before the horizon.** Proceed with it pre-horizon, provided it
   leaves the check-in path's dialectic enforcement untouched, as proposed, or
   hold it until after the read.
4. **Lease-plane profile.** Whether a Compose profile that makes the lease plane
   optional for new installs should be drafted, given that dialectic sagas and
   governed effects run there today.

## 11. Authorization

Merging this document authorizes the boundary guard in Section 6 only:
tests, a script and a baseline file, with no runtime import, flag or behavior.
Every other item in Sections 4, 7 and 8 needs its own PR and review, and the
items listed as after the horizon wait for it. Nothing here retires,
deprecates or disables a capability.

## 12. What this document does not claim

- It does not establish that a federation member exists outside the operator's
  own fleet; D3a's wake condition is unchanged.
- It does not claim the coupling figures are complete. They are one
  measurement; the guard is what keeps them honest.
- It does not rank components by value. Dispositions follow from where each sits
  relative to the federation boundary, not from usage.
