# Component boundaries for the federation: public contracts first, extraction second (v0)

**Status:** DRAFT proposal, 2026-10-06. Documentation only. It changes no
runtime behavior, schema, flag, default, threshold, tool, response shape or
registered protocol. It authorizes, at most, the tests-only boundary guard in
[Section 6](#6-first-implementation-the-boundary-guard). Revised the same day
after a four-seat council review; the record, with recommended dispositions
for the open decisions pending operator ratification, is
[Section 13](#13-council-record-2026-10-06).
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

This is a modularity refactor that prepares the core for federation members;
it does not serve one. No member outside the operator's fleet exists
(Section 12).

A federation member is a runtime that joins the operator's record through
public surfaces: MCP, REST, and the SDK. `PRODUCT_DEFINITION.md` states that
rule for the separate UNITARES Resident product: "ordinary userland: no direct
database access, no imports from Core internals, and no privileged measurement
or policy path." The reference residents under `agents/` are not bound by it;
CLAUDE.md calls them deliberately not agnostic. They are, though, the only
runtimes that exercise the core the way a member would, so this document uses
them as the proxy.

The core does not yet offer the contracts a member would need. Three
consequences follow.

1. **The proxy reaches inside.** Reference resident code imports Core modules
   and reads Core tables with SQL, directly and through scripts it launches
   (Section 2.3). What those residents show about a member's needs is skewed
   by access a member would not have.
2. **Components cannot be offered separately.** The review record and outcome
   binding are the parts the substitute map rates as differentiated, and the
   parts other systems could plug into. Neither has a public API: callers reach
   into handler internals (Section 2.2). Ring placement is not yet clean
   either: Core reads the knowledge graph's tables (Section 2.2), although
   this document places the knowledge graph in the subscriber ring.
3. **Nothing can be retired cleanly.** With raw SQL against other components'
   tables spread across dozens of files, removing or replacing a component means
   finding every reader by hand.

## 2. Measured coupling (2026-10-06)

Measured by parsing every `src/**/*.py` import (top-level and function-local)
and grepping `schema.table` literals. Figures are a snapshot, not a contract;
the guard in Section 6 re-measures them.

**Count caveat.** File counts in Section 2.4 are raw string matches. They
include the owning modules (five of the `core.identities` files sit under
`src/db/`) and lines that mention a table without querying it. The council's
independent recounts gave 33 rather than 32 files for `core.identities`, and
22 with a `FROM`/`JOIN`/`INTO`/`UPDATE` on the same line. Read the column as
an upper bound on callers to migrate, not as that number. The guard's
measurement (Section 6) excludes owner modules and replaces these figures.

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
  `src/mcp_handlers/updates/phases.py:2382` and
  `src/http_routes/substrate.py:404`.
- `src/mcp_handlers/lifecycle/stuck.py:72-74` constructs a `DialecticSession`
  and calls the dialectic reviewer selection and session save directly.
- The check-in path (`src/mcp_handlers/updates/phases.py`) pulls in identity
  persistence, the knowledge graph and dialectic enforcement.
- Five modules under `src/identity/` query `knowledge.discoveries` with SQL.
- `src/agent_loop_detection.py:455-460` starts dialectic recovery from inside
  the check-in path (`UNITARES_AUTO_DIALECTIC_RECOVERY`, default on).
- `src/mcp_handlers/identity/process_binding.py:91-102` uses the lease
  plane's presence lease (`agent_presence_lease.py`), which no-ops when the
  lease plane is not configured. The Elixir lease plane writes
  `core.dialectic_sessions` (`elixir/lease_plane/lib/unitares_lease_plane/dialectic_saga.ex`),
  but only when `UNITARES_DIALECTIC_BEAM_RESOLUTION` is set; it defaults off
  (`src/mcp_handlers/dialectic/beam_resolve_client.py:63`).

### 2.3 Members inside the boundary (verified by reading)

| File | Reach | Kind |
|---|---|---|
| `agents/dialectic_reviewer/host_backends.py`, `reviewer.py` | `src.*` imports | Resident userland: breach |
| `agents/local_resident/runner.py` | `src.local_inference_env` | Resident userland: breach |
| `agents/sentinel/agent.py` | `src.*` imports | Resident userland: breach |
| `agents/chronicler/scrapers.py`, `agents/sentinel/forced_release_alarm.py` | Read-only SQL against Core tables | Resident userland: breach |
| `agents/vigil/agent.py:82` → `scripts/eval/retrieval_eval.py:32` | Launches a script that imports `src.mcp_handlers.knowledge.handlers` | Resident userland: breach through a subprocess |
| `agents/sentinel/phase_b_promotion.py:32` → `scripts/lease_plane/evaluate_phase_b_promotion.py` | Launches a script that opens `psycopg2` directly | Resident userland: breach through a subprocess |
| `agents/{sentinel,vigil,watcher}/routes.py` | `src.*` imports, including a private name (`sentinel/routes.py:18` imports `_FINDING_SEVERITIES`), `lazy_mcp_server`, the broadcaster and `audit_db` | Route packs mounted in-process by `src/http_routes/packs.py`: extension code, not userland, but with unrestricted access |
| `agents/sdk/src/unitares_sdk/lease_plane/client.py:157` | `src.perf_monitor` | Optional guarded host hook, absent outside the server checkout: not a dependency |

The distinction in the last column matters: a route pack runs inside the server
and is entitled to an extension surface; a resident runner is a member and is
not. That surface does not exist yet. `packs.py` does not go through
`src/plugin_loader.py`, packs import whatever they need, and they mount
nothing unless `UNITARES_ROUTE_PACKS` is set (off by default).

The TODO at `agents/sentinel/agent.py:432` calls its `src.audit_db` import
the "last direct src/ import in agents/". It is stale: eight files under
`agents/` import `src`.

### 2.4 Components

| Component | Size (lines) | Fan-in / fan-out within `src/` | Tables read elsewhere | Infra |
|---|---|---|---|---|
| Identity and lineage | ~23.5k | 52 / 36 | `core.identities` in 32 files | Postgres, Redis, AGE edges, lease plane |
| EISV estimation and decision | ~15k + `governance_core` 4.1k | 19 / 74 | `core.agent_state` in 29 files | Postgres, Redis, LLM in enrichment |
| Knowledge graph | ~14.8k | 18 / 23 | `knowledge.discoveries` in 12 files | Postgres FTS; AGE and pgvector optional |
| Dialectic review | ~14.7k + Elixir saga | 11 / 28 | its tables also written by `elixir/lease_plane` | Postgres, LLM, BEAM |
| Audit log | ~2.75k | 36 / 4 | `audit.events` in 28 src and 19 Elixir/agent files | Postgres, Redis |
| Outcome binding | ~2.8k | 13 / 18 | `audit.outcome_events` in 5 files | Postgres only |
| Lease plane | 9.5k Elixir + ~2.9k Python | 12 / 15 | `surface_leases` in 4 src files | BEAM, Postgres; identity's presence leases and the lease identity binding (`enforce` by default in Compose) live here |
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
   queries Core tables with SQL, directly or through a script it launches.
   Route packs and plugins are in-process extensions. They should use a
   declared surface rather than arbitrary internals; for route packs that
   surface is still to be built (Section 2.3).
2. **Each core component owns its tables.** Another component reads them
   through the owner's API or mixin, never by raw SQL.
3. **No private cross-component calls.** A function another component needs is
   promoted to that component's public API.
4. **Subscribers depend on the core, never the reverse.** This is
   `eisv-core-boundary-v0`'s rule, generalised beyond EISV. The knowledge
   graph breaks it today: identity modules and the check-in path read its
   tables (Section 2.2). Placing the knowledge graph in the subscriber ring is
   the target, and inverting those dependencies is part of its disposition
   (Section 4).

The lease plane's ring is also a target rather than a fact. It is drawn as an
optional plane, but identity uses its presence leases and Compose makes the
server wait for it to be healthy (`docker-compose.yml:288`). How far it can be
optional is an open question (Section 10, D4).

Extraction to separate packages, and any retirement, comes after the boundary
holds, not before.

## 4. Component dispositions

| Component | Ring | Disposition | Why, for the federation |
|---|---|---|---|
| Identity and lineage | Core | Keep; give it read APIs so the 32 readers of `core.identities` stop using SQL | It is the spine every member binds to |
| Checkpoint | Core | Per `eisv-core-boundary-v0`, after the horizon | The unit members attach records to |
| Review (dialectic) | Core | **First public API:** open, answer, resolve, read. Before the horizon, at most an additive module that moves no callers and touches no write path; resolve, condition application and the `lifecycle/stuck.py` and `agent_loop_detection.py` migrations after it (Section 7) | The differentiated part members can use; its resolution record (`drr.v1`) already exists, dormant |
| Outcome binding | Core | Public `record_outcome()` replacing `_record_outcome_event_inline`; its own mixin. **After the horizon**: its tables feed the 2026-12-01 read | Members must report outcomes without internals |
| Audit log | Core | Keep as the leaf; add read APIs before restricting SQL | Everything writes it; moving it buys nothing |
| Knowledge graph | Subscriber (target) | Keep internal; put retrieval behind an interface so the default and any external recall can be swapped; invert the identity and check-in reads of its tables, the check-in ones after the horizon | Recall is crowded (substitute map); attribution is not |
| EISV estimation | Subscriber | Per `eisv-core-boundary-v0` stages, after the horizon | An assessor members can ignore or replace |
| Dashboard | Subscriber | Already reaches others over HTTP; separable at any time, low priority | No federation effect either way |
| Lease plane / BEAM | Optional plane (target) | First a dependency inventory, as documentation: what degrades without it. A Compose profile only after that, and no deployment change before the horizon. Any further change reopens the signed Wave 3 decision, which this does not | Coordination is a plane members may opt into |
| Reference residents | Member (proxy) | Remove `src.*` imports and direct SQL from runners and the scripts they launch (Section 2.3); keep route packs in-process behind an import allowlist | They are the closest thing to a member that exists today |

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

**Why first.** It changes no runtime code, which is consistent with the
intent of `eisv-core-boundary-v0` §12.1. That section is scoped to the EISV
stages and does not authorize this guard; Section 11 of this document does.
Every later step depends on the guard to stick.

**Shape.** A script under `scripts/dev/` plus a test, following the ratchet
pattern of `scripts/dev/check_proposals_length.py` and its baseline file.
Existing violations are listed in a committed baseline, keyed per file and
per rule. A new violation fails. Removing one shrinks the baseline, which may
not grow back. The check compares against the base branch's baseline
(`--base`), so a change cannot absorb its own new violation by editing the
baseline in the same PR. Baseline counts are telemetry: they authorize no
retirement (Section 8).

**Rules (v0):**

- **B1, member imports.** A non-test file under `agents/` that is not a route
  pack mounted by `src/http_routes/packs.py` must not import `src.*`. The one
  exception is the SDK's optional host hook: an import inside a `try` whose
  failure path returns, under `agents/sdk/` only. Residents run inside the
  checkout, where such an import always succeeds, so the exception is not
  available to them.
- **B1p, pack imports.** A route pack may import only from an allowlisted
  module set, recorded in the guard's source. The initial list is what packs
  import today, minus private names; shrinking it is the declared surface's
  work.
- **B2, member SQL.** A non-test file under `agents/` must not contain SQL
  naming a `core.`, `audit.`, `knowledge.`, `lease_plane.` or `effects.` table.
- **Subprocess targets.** B1 and B2 also apply to any repository script a
  member file launches by path, so a breach cannot move into `scripts/`.
- **B3, private reach.** A module, route packs included, must not import a
  `_`-prefixed name from a module in another component's directory.
- **B4, foreign SQL.** A file outside a component's owned modules must not carry
  raw SQL against that component's tables. The owner map starts with the
  tables named in Section 2.4 and is part of the guard's source. It can name
  a co-owner, so the Elixir lease plane's writes to dialectic tables are
  declared rather than flagged forever or missed. Elixir files are scanned
  for the same table names.

**Acceptance:** passes on `master` with the baseline as measured; fails when a
violation is added in a mutation test for each rule; adds no runtime import,
flag or behavior. Wiring it into pre-commit and the `Repo Scope Guard`
workflow is part of the same change. B1, B1p's private names and B2 cover
about ten files; the resident fixes that empty them are separate PRs (Section
7), and once a rule's baseline is empty it admits no new entry.

**Out of scope:** fixing any baselined violation, every API in Section 4, and
any package split.

## 7. Sequencing against the preservation horizon

The horizon is the 2026-12-01 registered read, final once its report is merged
and stands 14 days without a filed correction (`eisv-core-boundary-v0` §12.1,
item 6).

- **Before the horizon:** this document; the guard (Section 6); the resident
  runner fixes for B1 and B2; at most an additive review module (open,
  answer and read wrappers) that moves no callers and touches no write path;
  the knowledge graph retrieval interface; the lease-plane dependency
  inventory, as documentation.
- **After the horizon:** review resolve and condition application; moving
  `lifecycle/stuck.py` and `agent_loop_detection.py` onto the review API;
  the outcome-binding API and mixin; the checkpoint spine stages of
  `eisv-core-boundary-v0`; any change to the check-in path; any Compose or
  deployment change for the lease plane.

Why resolve waits: its `dialectic_resolved` outcome rows carry
`verification_source="server_observation"`, a tier the read excludes, so the
read's labels are not at risk. Its features are. Resolution writes
`dialectic_conditions` and `meta.status`, and the check-in path reads both
before and after the ODE (`enforce_complexity_limit`,
`enforce_post_ode_conditions`). Leaving enforcement untouched does not protect
what enforcement reads, and the preservation harness is partial: it does not
yet pin the full `process_agent_update` path or real outcome recording
(`docs/operations/eisv-instrument-preservation-harness.md`).

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
  `core.discovery_embeddings` table and the CIRS announce tools. The plugin
  `register_extra_*` hooks are KEEP-DORMANT there, not a candidate; they are
  part of the extension surface Section 3 relies on.

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
3. **Review API before the horizon.** An additive module that moves no
   callers and touches no write path, as now proposed (Section 7), or hold all
   of it until after the read.
4. **Lease-plane profile.** Whether to inventory, and later propose, a Compose
   profile that makes the lease plane optional for new installs. BEAM dialectic
   resolution is off by default, so a default install's review does not need
   it; governed effects, liveness reapers, the lease identity binding,
   identity's presence leases and governed spawn do use it.

Section 13 records the council's recommended answer to each.

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

## 13. Council record (2026-10-06)

**Question.** The operator asked whether a council could settle the four open
decisions in Section 10. Four seats reviewed this document's first draft
(commit `dbda18d`) independently, read-only, each with a fixed lens and
without sight of the others' answers:

1. **Maintainer cost:** effort and ongoing burden for a solo maintainer.
2. **Federation member:** what a runtime joining through MCP, REST and the SDK
   alone would need.
3. **Guardian of the registered read:** whether any of it could contaminate
   the 2026-12-01 read.
4. **Adversarial skeptic:** the draft's facts checked against the code, and
   each default attacked.

**Caveat.** All four seats and the author are the same model family, run in one
session. Independence was of context and lens, not of model. This record
recommends; it decides nothing. The deciding standard for each open decision
is the operator's (CLAUDE.md, *Measurement authority*).

### 13.1 Votes

| Decision | Cost | Member | Guardian | Skeptic |
|---|---|---|---|---|
| D1 guard | Ratchet B3/B4; B1/B2 strict now | Ratchet, per file and rule, shrink-only, `--base`; cover Elixir | Ratchet | Ratchet, after closing the subprocess and `try` loopholes |
| D2 route packs | In-process; B3 applies to packs | In-process behind an import allowlist; B3 applies | In-process, at least until the read | In-process; replace the blanket exemption with an allowlist |
| D3 review API | Hold until after the read | Proceed as a façade with no behavior change | Read-side wrappers only; resolve, conditions and `stuck.py` wait | Hold; at most an additive module that moves no callers |
| D4 lease plane | Draft the profile | Draft it, with a degradation list | Draft as documentation; no deployment change before the read | Inventory dependencies first |

### 13.2 Recommended dispositions (pending operator ratification)

- **D1: ratchet,** per file and per rule, shrink-only, checked against the
  base branch, with Elixir scanned and a co-owner map, subprocess targets
  followed, and the `try` exemption limited to the SDK. The B1 and B2
  baselines are expected to empty through the resident fixes, after which they
  admit nothing. Unanimous on ratchet. Dissent: the cost seat would make B1/B2
  fail-closed in the guard PR itself; that would put resident code changes into
  a PR this document authorizes as tests-only, so they are separate PRs
  instead.
- **D2: keep route packs in-process,** but replace their blanket B1 exemption
  with an import allowlist (B1p) and apply B3 to them. Unanimous on
  in-process; three seats asked for the allowlist. Every seat named the cost:
  packs keep access no outside member has.
- **D3: before the horizon, at most an additive module** (open, answer and
  read wrappers) that moves no callers and touches no write path; resolve,
  condition application and both caller migrations wait. This is the
  convergence of the guardian's and skeptic's conditions. Dissent: the cost
  seat would hold everything; the member seat would include resolve as a
  façade. The guardian offered an alternative the operator may prefer:
  resolve moves before the horizon only with a byte-identical pin of
  `dialectic_conditions` and of the `dialectic_resolved` row it emits.
- **D4: dependency inventory first, as documentation;** no Compose or
  deployment change before the read is final. Three seats favoured some
  drafting now; the skeptic's inventory-first condition is adopted because the
  draft's "optional plane" contradicted its own identity row.

### 13.3 Corrections applied to the first draft

Each was checked against the code before it was applied.

| Section | Was | Now |
|---|---|---|
| 1 | Residents bound by the userland rule; framed as federation | Rule belongs to the Resident product; reference residents are a proxy; framed as modularity that prepares for members |
| 1, 2.3 | Residents "write SQL" against Core tables | The cited files only read |
| 2.2 | — | Added `agent_loop_detection.py`, the presence-lease use in identity, BEAM's dialectic writes and their default-off flag |
| 2.3 | Subprocess routes missing; packs said to use a declared surface | Two subprocess routes added; packs have unrestricted access, off by default; stale sentinel TODO noted |
| 2.4 | Counts read as caller counts | Method and upper-bound caveat stated |
| 3 | Knowledge graph and lease plane placed without qualification | Both marked as targets, with the dependencies that contradict them today |
| 6 | Cited `eisv-core-boundary-v0` §12.1 as permission | Relies on Section 11; §12.1 only for intent |
| 6 | Ratchet without per-rule keys, Elixir, subprocesses or a narrowed `try` exemption | All four added, plus B1p |
| 7 | `agent_loop_detection.py` and resolve before the horizon; enforcement proviso | Both after the horizon; proviso replaced by the write-path rule and its reason |
| 8 | `register_extra_*` listed as CUT or DECIDE | KEEP-DORMANT, per the registry |
| 10.4 | "Dialectic sagas … run there today" | BEAM resolution is off by default |

Two seat claims were not applied. The cost seat said `governance-mcp` has no
`depends_on: lease-plane`; `docker-compose.yml:288` shows that it does. The
skeptic said `dialectic_resolved` rows feed the read; they are
`server_observation` rows, which the read excludes, as the guardian found.
The skeptic's underlying sequencing point stands on the features channel
(Section 7).
