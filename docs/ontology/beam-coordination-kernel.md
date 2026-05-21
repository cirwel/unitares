# BEAM Coordination Kernel Plan

**Created:** April 30, 2026  
**Last Updated:** May 2, 2026  
**Status:** Placement decided; Phase 1 in-memory spike implemented

## Related artifacts (independent convergence, 2026-04-30)

This ontology-track plan converged with a parallel proposals-track RFC on the same primitive without coordination between sessions. Both should be read by anyone executing the spike:

- **`docs/proposals/surface-lease-plane-v0.md` (v0.4)** — proposals-track contract spec. Council-pass-1 + ack-pass complete; status: implementation-gate ready. Defines the Postgres schema (matches `db/postgres/migrations/024_lease_plane.sql` verbatim), the `/v1/lease/*` HTTP API, typed-absence return shapes (matches `src/lease_plane/models.py`), the Phase A advisory → Phase B selective-enforcement rollout, and the §7 open questions.

This plan is the **integration-into-UNITARES framing** (R7 row in `docs/ontology/plan.md`); the RFC is the **contract spec**. Neither subsumes the other. Implementation skeleton (`db/postgres/migrations/024_lease_plane.sql`, `src/lease_plane/`, `tests/test_lease_plane_client.py`) was captured into the repo by commit `b5364d3` after both docs landed; the migration is already applied to the live `governance` database. The Elixir/OTP implementation now lives in this repo under `elixir/lease_plane/`.

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

### `POST /leases/acquire`

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

### `POST /leases/:lease_id/renew`

Renews TTL if caller proves same holder/episode or valid handoff successor.

### `POST /leases/:lease_id/release`

Releases with `release_reason`.

### `GET /surfaces/:surface_type/:surface_id`

Returns active lease or typed absence.

### `POST /handoffs/offer`

Offers typed transfer.

### `POST /handoffs/:handoff_id/accept`

Accepts transfer and updates lease holder.

## Postgres schema sketch

```sql
CREATE TABLE coordination.surface_leases (
    lease_id UUID PRIMARY KEY,
    surface_type TEXT NOT NULL,
    surface_id TEXT NOT NULL,
    holder_uuid UUID,
    holder_label TEXT,
    episode_id TEXT,
    harness TEXT,
    intent TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL,
    handoff_to UUID,
    release_reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX surface_leases_one_active_surface
ON coordination.surface_leases (surface_type, surface_id)
WHERE status = 'active';
```

V1 can use application-level expiry checks plus a periodic reaper. Do not rely on partial index magic alone; expired rows must transition out of `active`.

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

1. Add Ecto.
2. Add migration for `coordination.surface_leases`.
3. Persist lifecycle transitions.
4. Add expiry reaper.
5. Add tests around process crash/restart restoring active leases from DB.

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

Run the Phase 1 proof in the existing in-repo OTP app:

```bash
cd elixir/lease_plane
mix test test/surface_registry_test.exs
```

Keep UNITARES integration through the HTTP/API contract and the existing `lease_plane` schema. Do not create `unitares-coordination-kernel` unless a future split has a concrete release-cadence or external-consumer reason.
