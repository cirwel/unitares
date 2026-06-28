# BEAM Coordination Kernel Plan

> **Design record.** A planning / RFC document kept as design provenance; it captures intent at a point in time and may lag the running code. For current behavior see [`UNIFIED_ARCHITECTURE.md`](../UNIFIED_ARCHITECTURE.md) and the runtime sources it points to.

**Created:** April 30, 2026
**Last Updated:** June 28, 2026
**Status:** Placement decided; lease-plane live; Sentinel Wave 1 shipped (`com.unitares.sentinel-beam`); **Wave 3a read-only handlers DEPLOYED (:8770, first inbound BEAM listener) and dialectic-on-BEAM merged (flags off, 2026-06-28)**. Remaining = Wave 3 write-path (3b/3c `handler_dispatch`, not started).

## Related artifacts (independent convergence, 2026-04-30)

This ontology-track plan converged with a parallel proposals-track RFC on the same primitive without coordination between sessions. Both should be read by anyone executing the spike:

- **`docs/proposals/surface-lease-plane-v0.md`** — canonical lease-plane contract spec. It defines the `lease_plane.*` Postgres schema migrations, `/v1/lease/*` HTTP API, typed-absence return shapes, advisory → selective-enforcement rollout, surface-kind grammar, substrate-state extension, and Phase B gates.
- **`docs/proposals/surface-lease-plane-phase-a-plan.md`** — shipped Phase A implementation ledger. Use this to reconstruct which RFC rows landed in which PR sequence.
- **`docs/operations/lease-plane-operator-runbook.md`** — live operator surface for the running launchd service on `127.0.0.1:8788`.
- **`docs/proposals/beam-footprint-roadmap-v0.md`** — roadmap-level migration decision. Current binding destination is stateful coordination to BEAM, stateless computation in Python.
- **`docs/proposals/beam-wave-1-sentinel.md`** — Sentinel-on-BEAM Wave 1 RFC. Surface 1 cycle state, Surface 2 findings emission, and Surface 3 lease advisory are the active Sentinel scope.
- **`docs/proposals/beam-wave-3-handler-dispatch.md`** — handler dispatch, identity middleware, and dialectic resolution RFC. This is a single-writer identity/onboarding-adjacent surface; check open PRs before editing.

This plan is the **integration-into-UNITARES framing** (R7 row in `docs/ontology/plan.md`); the RFCs are the **contract specs**. Neither subsumes the other. The original implementation skeleton (`db/postgres/migrations/024_lease_plane.sql`, `src/lease_plane/`, `tests/test_lease_plane_client.py`) was captured into the repo by commit `b5364d3` after both docs landed. The current Elixir/OTP apps live under `elixir/lease_plane/` and `elixir/sentinel/`.

---

## Purpose

This document scopes the "BEAM thing": a small OTP/Elixir coordination plane beside UNITARES, not a rewrite of UNITARES, Hermes, Lumen, or TouchDesigner.

The forcing function is concrete: our recurring failures are null/absence ambiguity, stale locks, race conditions, async task leaks, parallel-agent collision, stale-present confusion, and class-basin mismatch. These are not only code-quality failures. They are failures to represent live ownership of time-varying surfaces.

Core question:

> Who owns what surface, in which temporal basin, under what proof of life, until when, and how is it handed off?

## Non-goals

- Do not rewrite UNITARES in Elixir.
- Do not replace Hermes as the agent harness.
- Do not replace Lumen/anima as the embodied creature loop.
- Do not replace TouchDesigner as the expressive visual body.
- Do not create a broad agent chat/message board.
- Do not use KG as a transient coordination bus.
- Do not start with distributed BEAM clustering.
- Do not put secrets, continuity tokens, or opaque credentials in coordination events.

## Why BEAM / OTP fits

OTP operationalizes aliveness:

- every live coordinator is a process with a mailbox;
- state has an owner;
- death is observable through links/monitors;
- restart policy is explicit;
- supervision trees encode failure domains;
- GenServer serialization prevents shared-memory races inside a surface owner;
- telemetry is native enough to become UNITARES evidence.

This aligns with the real problem class better than ad hoc Python `asyncio` tasks and lock rows with unclear death semantics.

## Architecture posture

Use OTP for hot coordination and Postgres/UNITARES for durable truth.

```text
Hermes / Claude / Codex / Lumen / TouchDesigner
        │
        ▼
BEAM Coordination Kernel
  ├─ SurfaceRegistry
  ├─ LeaseServer
  ├─ HandoffServer
  ├─ BasinRouter
  ├─ EpisodeSupervisor
  ├─ BridgeSupervisor
  └─ TelemetryForwarder
        │
        ▼
UNITARES governance + Postgres
  ├─ durable lease/handoff audit rows
  ├─ process_agent_update / outcome_event evidence
  └─ KG only for promoted durable lessons
```

## Initial wedge: surface leases, not agent chat

Start with a narrow primitive that generalizes the existing coordination-lease dialectic conclusion:

> PostgreSQL-backed TTL surface leases, supervised by a local OTP service.

A surface is any shared mutation target:

- repo file path;
- repo branch;
- TouchDesigner network path;
- capture session;
- Lumen display/action surface;
- Discord thread/locus;
- cron job identity;
- MCP server config fragment.

V1 should support only two surface classes:

1. `repo_path` — whole-file single-writer leases.
2. `td_network` — TouchDesigner network mutation leases such as `/eisv_basin_v31`.

This keeps the first version testable without pretending to solve all multi-agent coordination.

## Core data model

### Lease

A lease is a live claim with explicit expiry and proof obligations.

Fields:

- `lease_id` — UUID.
- `surface_type` — enum: `repo_path`, `td_network` initially.
- `surface_id` — path-like identifier, for example `docs/ontology/plan.md` or `/eisv_basin_v31`.
- `holder_uuid` — UNITARES UUID of claimant, if known.
- `holder_label` — display label only, never identity proof.
- `episode_id` — current harness/session/thread locus if available.
- `harness` — `hermes`, `claude_code`, `codex`, `dispatch`, `lumen`, etc.
- `intent` — concise human-readable purpose.
- `evidence_ref` — validated reference proving why the lease was acquired; may be a task id, issue id, dialectic id, user request id, or local episode id.
- `acquired_at` — timestamp.
- `expires_at` — timestamp.
- `last_heartbeat_at` — timestamp.
- `status` — `active`, `released`, `expired`, `transferred`, `revoked`.
- `handoff_to` — optional holder target.
- `release_reason` — optional.

### Handoff

A handoff is not a chat message. It is a typed transfer proposal.

Fields:

- `handoff_id` — UUID.
- `lease_id` — lease being transferred.
- `from_holder_uuid`.
- `to_holder_uuid`.
- `state_snapshot_ref` — pointer to summary/artifact, not raw context dump.
- `known_hazards` — concise list.
- `freshness_horizon` — timestamp or TTL after which recipient must revalidate.
- `status` — `offered`, `accepted`, `rejected`, `expired`, `cancelled`.

### Typed absence

Do not return undifferentiated nulls. The API returns typed absence:

- `not_found`
- `not_yet_created`
- `pending`
- `expired`
- `revoked`
- `unreachable`
- `permission_denied`
- `stale`
- `conflicted`
- `tombstoned`
- `intentionally_absent`

This is the null-pointer cure: callers must handle what kind of absence occurred.

## OTP process shape

### `Coordination.Application`

Top-level supervision tree.

Children:

- `Coordination.Repo` — Postgres access.
- `Coordination.Telemetry` — event emission.
- `Coordination.SurfaceRegistry` — maps active surfaces to owner processes.
- `Coordination.LeaseSupervisor` — DynamicSupervisor for active lease processes.
- `Coordination.BridgeSupervisor` — external bridge processes.
- `CoordinationWeb.Endpoint` — HTTP API for non-BEAM clients.

### `Coordination.LeaseProcess`

One process per active lease.

Responsibilities:

- serialize lease renewal/release/handoff messages;
- maintain current live heartbeat deadline;
- monitor local BEAM holders when applicable;
- expire lease on TTL;
- write durable status changes;
- emit telemetry for UNITARES.

### `Coordination.SurfaceRegistry`

Registry for active surfaces.

Responsibilities:

- reject conflicting active leases;
- return existing lease status;
- spawn `LeaseProcess` through `LeaseSupervisor`;
- distinguish active local process from durable stale row.

### `Coordination.BasinRouter`

Small classifier that chooses coordination rule from surface/task class.

Initial modes:

- `single_writer` — repo path edits.
- `visual_surface_builder` — TouchDesigner network mutation.
- `calibration_capture` — screenshot/capture windows.
- `durable_memory` — KG/doc writes, no hot chat.

V1 can hardcode rules. Do not add ML classification.

## HTTP API v1

Expose a small JSON API first. MCP wrapper can come later.

### `POST /v1/lease/acquire`

Request:

```json
{
  "surface_type": "repo_path",
  "surface_id": "docs/ontology/plan.md",
  "holder_uuid": "07d0f9c7-1512-4a1e-8cb1-a5225c20709f",
  "holder_label": "Mnemos",
  "episode_id": "hermes-cli-...",
  "harness": "hermes",
  "intent": "draft BEAM coordination kernel plan",
  "ttl_seconds": 900,
  "evidence_ref": "user-request:beam-coordination"
}
```

Responses:

- `201 acquired`
- `200 already_held_by_self`
- `409 conflicted` with current holder, expiry, and intent
- `422 invalid_evidence_ref`

The shipped API returns the discriminated JSON shapes from `surface-lease-plane-v0.md` instead of relying on HTTP status alone. For example: `ok: true` with `idempotent: true | false`, or `ok: false` with `error: "held_by_other" | "schema_invalid" | "service_unavailable" | ...`.

### `POST /v1/lease/renew`

Renews TTL if caller proves same holder/episode or valid handoff successor.

### `POST /v1/lease/release`

Releases with `release_reason`.

### `GET /v1/lease/status`

Returns active lease or typed absence for `surface_id`.

### `POST /v1/lease/heartbeat`

Records remote-holder proof of life.

### `POST /v1/lease/handoff/offer`

Offers typed transfer.

### `POST /v1/lease/handoff/accept`

Accepts transfer and updates lease holder.

## Postgres durable contract

The shipped schema is `lease_plane.*`, not the early ontology-draft `coordination.*` sketch. Implement from the migrations and RFC, not from historical prose:

- `db/postgres/migrations/024_lease_plane.sql` — first durable contract: `lease_plane.surface_leases`, `lease_plane.lease_plane_events`, active unique index, immutable holder/TTL checks, and event outbox shape.
- Later `lease_plane` migrations — surface-kind grammar, deprecation catalog, earned-status guard, substrate-state columns and CHECK constraints.
- `docs/proposals/surface-lease-plane-v0.md` — semantic contract for the schema and typed absence.

V1 uses application-level expiry checks plus a periodic reaper. Do not rely on partial index uniqueness alone; expired rows must transition out of the active set.

## Telemetry to UNITARES

Emit structured events for:

- lease acquired;
- lease renewed;
- lease conflict;
- lease expired;
- lease released;
- handoff offered;
- handoff accepted;
- handoff rejected;
- stale surface read;
- typed absence returned;
- bridge unreachable;
- supervisor restart.

Forwarder behavior:

- ordinary events become `process_agent_update(... recent_tool_results=[...])` only when useful;
- completed lease lifecycle can emit `outcome_event(task_completed)`;
- only durable lessons become KG notes;
- no continuity tokens or secrets enter event payloads.

## Implementation sequence

Current status as of 2026-05-21:

- `elixir/lease_plane/` is the live lease-plane OTP app.
- `elixir/sentinel/` is the Wave 1 Sentinel OTP app.
- Phase A lease-plane rollout is complete; Phase B remains selectively gated by surface kind.
- The Wave 3 handler-dispatch RFC is design-gated by measurement artifacts and single-writer identity/onboarding coordination.

### Phase 0 — repo and toolchain

1. Confirm Elixir/Mix availability.
2. If absent, install with Homebrew or asdf.
3. Use an in-repo UNITARES service boundary, not a standalone repository:
   - repo-placement decision, 2026-05-02: **inside `unitares`**;
   - current implementation path: `elixir/lease_plane/`;
   - future packaging target, if the repo adopts a generic service layout: `services/coordination_kernel/`.
4. Add CI for `mix test` and formatting.

### Phase 1 — pure in-memory lease server

Implementation note, 2026-05-02: the pure in-memory spike lives in `elixir/lease_plane/lib/unitares_lease_plane/{surface_registry,lease_process}.ex` with tests in `elixir/lease_plane/test/surface_registry_test.exs`. It intentionally avoids Postgres and the durable lease-plane contract; it proves the hot OTP primitive only.

1. Generate Mix project.
2. Implement `LeaseProcess` and `SurfaceRegistry` without Postgres.
3. Add tests for acquire/release/conflict/expiry.
4. Add typed absence return values.
5. HTTP exposure remains on the durable lease-plane router; the in-memory proof is intentionally not a second public API.

Exit criterion: two concurrent requests for the same `surface_id` deterministically produce one acquired lease and one conflict. Covered by `UnitaresLeasePlane.SurfaceRegistryTest` (50 racing holders, exactly one winner).

### Phase 2 — Postgres durability

1. Use Postgrex against the existing `lease_plane.*` schema.
2. Persist lifecycle transitions.
3. Add expiry reaper.
4. Add tests around process crash/restart restoring active leases from DB.

Exit criterion: killing the BEAM process does not lose active lease knowledge; expired leases become expired, not corpse-locks.

### Phase 3 — Hermes/agent integration

1. Add a tiny Python client or direct HTTP helper.
2. Teach Hermes workflows to acquire a `repo_path` lease before editing known shared docs.
3. Add TouchDesigner builder lease around `/eisv_basin_v31` mutation.
4. Emit UNITARES telemetry.

Exit criterion: Hermes cannot silently mutate a leased surface without seeing the conflict.

### Phase 4 — handoff

1. Implement handoff offer/accept.
2. Add freshness horizon to handoff payloads.
3. Add tests for expiry/rejection.
4. Use handoff for compaction or subagent transfer.

Exit criterion: ownership can move without waiting for TTL expiry or creating ghost claims.

## Design risks

- Too broad too early: avoid agent chat, inboxes, or global routing until leases work.
- Hidden distributed truth: local BEAM process monitoring only proves local liveness; external agents need heartbeat TTL.
- Lock theater: if evidence refs are not validated, leases become performative claims.
- KG sludge: lease lifecycle should not flood KG.
- Overcoupling: UNITARES should consume evidence; it should not depend on BEAM runtime for core identity resolution.
- Split-brain: distributed BEAM clustering is out of scope until single-node semantics are proven.

## Repo placement decision

Decision, 2026-05-02: keep the coordination kernel inside the `unitares` repo as a service boundary, not in a standalone repository.

Rationale:

- R7 is operationally separate, but its invariants are not independent: the Postgres schema, Python typed-absence contract, Elixir router, Sentinel alarms, and ontology/RFC docs have to move together.
- The lease plane already writes into a UNITARES-owned schema in the same governance database and emits evidence consumed by UNITARES; splitting the repo would create cross-repo release ordering for one runtime invariant.
- A process boundary is enough for OTP supervision and deployment isolation. A repo boundary can wait until there is a real independent release cadence or non-UNITARES consumer.
- This follows the `unitares-core` fold-back lesson: avoid two-repo coordination when the abstraction is load-bearing for the parent system.

Current physical path is `elixir/lease_plane/`. Renaming to `services/coordination_kernel/` is packaging hygiene, not an ontology gate.

### Extraction triggers (when to revisit)

The 2026-05-02 decision is correct for the current evidence base — no second lease consumer, R6/S22 dogfood for cross-harness lease semantics not yet started, BEAM-on-unitares premise unclear. The decision is also reversible: cost of extracting *during fast churn* is low (no external consumers to break, breaking-changes-events are free), and the language/tool boundary already exists at the mix/Elixir level. The right time to revisit is when one of these named triggers fires — not vague "when it makes sense":

1. **First non-unitares code path that calls into the LeasePlane API.** Even one line in another repo (Hermes, anima-mcp, an external partner, or a separate operator-managed service) makes the boundary load-bearing. At that moment the in-repo path stops being "internal abstraction" and becomes "private dependency of an external consumer" — the standard signal to extract.
2. **Phase 2 milestone (handoff semantics) requires multi-process coordination that Hermes or Pi-side actually consumes.** Phase 1 is operator-tooled and unitares-internal; if Phase 2's handoff invariants need to fire from a process that isn't unitares-mcp, the lease plane has graduated past in-repo coupling.
3. **BEAM-on-unitares premise clarifies** in a way that makes lease_plane the foundation of broader Erlang-native services rather than a single-purpose lease server. If a second OTP application materializes alongside lease_plane (router, presence, broker, etc.) and they want shared infrastructure, the kernel becomes a platform, not a feature.

Soft signals to *watch* but not act on alone: Elixir-vs-Python CI lane friction (solvable in-repo), independent mix.lock divergence, contributor parallelism causing rebase pain.

When any trigger fires, the extraction itself is small: `git mv elixir/lease_plane/ ../unitares-coordination-kernel/`, separate CI workflow, version-pin from the unitares side, document the wire-protocol contract. The mental tax of "this lives in unitares but it's actually its own thing" is the early-warning sign that the trigger is approaching, not the trigger itself.

## Immediate next action

For lease-plane changes, run the focused OTP suite first:

```bash
cd elixir/lease_plane
mix test
```

For Sentinel Wave 1 changes, run the focused Sentinel suite:

```bash
cd elixir/sentinel
mix test
```

Keep UNITARES integration through the HTTP/API contract and the existing `lease_plane` schema. Do not create `unitares-coordination-kernel` unless a future split has a concrete release-cadence or external-consumer reason.
