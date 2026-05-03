---
status: DRAFT-v0.8 (§7.11 + §7.12 resolved; v1 forward-compat for content-addressing left Open by design; pre-existing v0.7 implementation drift surfaced as named §9 gates)
authored: 2026-04-30
amended: 2026-04-30 (v0.1–v0.8 same session)
council_pass_1: 2026-04-30
ack_pass_1: 2026-04-30
author_session: agent-68437d77-65c (claude_code-claude_68437d77)
review_target: |
  Council pass 1 complete (parallel agents, 2026-04-30):
    - dialectic-knowledge-architect: 4 BLOCKs, 5 CONCERNs, 1 NIT — all addressed in v0.2
    - feature-dev:code-reviewer: 4 BLOCKs, 3 CONCERNs, 1 NIT — all addressed in v0.2
    - live-verifier: 9 CONFIRMED, 4 DRIFT — all 4 drifts corrected in v0.2

  Ack-pass complete (parallel agents, 2026-04-30; precedent: onboard-bootstrap-checkin.md v2.1):
    - dialectic-knowledge-architect: 1 new BLOCK, 2 new CONCERNs, 2 NIT-clean — addressed in v0.3
    - feature-dev:code-reviewer: 2 new BLOCKs, 4 new CONCERNs — addressed in v0.3
    - live-verifier: 6 VERIFIED, 3 DRIFT (number-conflation, "Lumen-class", token-naming) — corrected in v0.3

  Per the v2.1 precedent, no further ack-pass required after v0.3 unless v0.4+ amendments
  themselves introduce new gaps. Current state: implementation-gate ready.
provenance: |
  This proposal emerged from a three-voice synthesis (claude_code, codex, gpt-5.5)
  on 2026-04-30 during a discussion of OTP/Elixir fit for UNITARES. Three independent
  paths converged on the same first wedge: a lease/ownership service for shared
  mutable surfaces, advisory-mode-first, with the IDENTITY_STRICT log->strict
  rollout pattern.
related:
  - KG 2026-04-30T10:22:54.383330+00:00 (agent UUID 07d0f9c7, claimed display name "Mnemos": dialectic 95c9ddfd6bb09308 RFC for TTL surface leases. Note: "Mnemos" is the agent's display claim; the KG record carries the UUID, not the name. Lookups should use UUID.)
  - KG 2026-04-30T10:21:17.486053+00:00 (claude_code-claude_9f60251c: dialectic state-machine bug, resolved by PR #247)
  - KG 2026-04-30T10:20:26.584546+00:00 (claude_code-claude_9f60251c: two-mode dialectic topology, operator-flagged-rebuild)
  - KG 2026-04-14T23:37:36.237416+00:00 (multi-agent git reset destroyed ~400 lines of WIP — surface collision incident)
  - KG 2026-04-27T08:50:43.113176+00:00 (Watcher _reload_if_stale ↔ save_state race, no flock)
  - KG 2026-04-25T20:07:01.505701+00:00 (ship.sh / Watcher --resolve race orphans P005 fingerprint)
  - PR #218 (`feat(db): ExecutorPool — asyncpg loop-isolation wrapper for anyio (P2 full)` — the Python-side concurrency tax that motivates substrate questioning)
  - PR #247 (`fix(dialectic): accept conditions alias + early-fail on agrees+empty` — closes the immediate dialectic state-machine class)
  - docs/proposals/path1-sync-fingerprint-check.md (precedent for log->strict phased rollout)
  - **docs/ontology/beam-coordination-kernel.md** (parallel ontology-track spec converged independently on the same primitive 2026-04-30; framed as UNITARES R7 row in `docs/ontology/plan.md`. This proposals-track RFC is the contract spec; the ontology-track plan is the integration-into-UNITARES framing. Neither subsumes the other; both should be read by anyone executing the spike. See v0.4 changelog for the convergence story.)
  - **db/postgres/migrations/024_lease_plane.sql** (live in `governance` DB as of 2026-04-30 ~13:05 local; implements §4.4 schema verbatim — the schema is no longer a proposal, it's deployed)
  - **src/lease_plane/** (Python contract anchor implementing §4.5 typed-absence shapes; `LeasePlaneDisabledClient` is the advisory-mode escape valve. Closes RFC §9 checklist item "Shelf-Python sketch checked in alongside the Elixir spec.")
  - **docs/proposals/plexus-scope.md** (`Plexus` is the product/system boundary name for this RFC's coordination layer. Defines v1 ownership, non-goals, stop signs, and the manual `Plexus Zero` bootstrap protocol used until this RFC's service ships. Does not redefine schema, API, or `surface_id` semantics — those remain here.)
unblocks: |
  - Recurring "auto-recovered stuck agent" KG entries (12+ in the last 5 days)
  - Multi-agent surface collisions on shared file paths, TD networks, dialectic sessions
  - The TTL-surface-leases primitive that dialectic 95c9ddfd6bb09308 produced as v1 RFC output
out_of_scope_explicit: |
  Hard line — load-bearing substrate boundaries:
  - Distributed Erlang clustering across Mac<->Pi (cross-host coordination uses Postgres heartbeat-TTL, never Erlang clustering)
  - Horde / libcluster (violate single-node rule by design)
  - EISV / calibration / KG / identity issuance — these stay in Python, hard line

  Deferred to subsequent RFCs (each merits its own scope):
  - Per-agent runtime state ownership in BEAM
  - Resident supervision tree
  - Phoenix LiveView migration of existing dashboard (LiveView+PubSub is genuinely better than current Chart.js+WS plumbing; deferred for scope, not refused)
  - Phoenix PubSub migration of existing broadcaster/Discord bridge/dashboard WS
---

# Proposal: Surface Lease Plane v0 (Elixir/OTP coordination kernel)

> **Status: DRAFT-v0, pre-council.** This document captures the first concrete coordination-substrate wedge converged on by three independent reviewers (claude_code, codex, gpt-5.5) on 2026-04-30. It is intentionally a **decision document with open questions**, not an implementation spec. Sections 7-8 list the questions that must be answered (council-reviewed) before any `.ex` file is written.

## 1. Problem

UNITARES has been paying a steady concurrency tax. The git trail since 2026-01 shows ~17 concurrency-class *code* commits in `unitares` and ~13 more in `anima-mcp`, plus several documentation/incident-tracker closures (e.g., PR #198 `docs: close S17 Redis pin deadlock row` is a tracker-row closure, not a code fix; the underlying redis pin work landed elsewhere). Code-level fixes include TOCTOU remediation, anyio<->asyncpg loop-isolation (`ExecutorPool` PR #218 — `feat(db): ExecutorPool — asyncpg loop-isolation wrapper for anyio (P2 full)`), pool-recovery work (PR #228 `fix(db): dedupe pool-recovery destroy log via in-lock identity re-check`, PR #230 `fix(db): structural pool wedge fix + council adversarial review`), dialectic deadlock guards (PR #50 ships within `feat(watcher): region-aware hook + dialectic deadlock guards & synthesis participant gate`), and recurring stuck-agent recovery — two distinct measures of the same incident class:

- **14 KG entries** tagged `auto-recovery` in `knowledge.discoveries` over 5 days ending 2026-04-30 (live-verified via `mcp__unitares-governance__knowledge` action=search). Summary text uniformly cites `critical_margin_timeout`.
- **12 distinct agent UUIDs** in `audit.events` `stuck_detected` payloads with structured `reason=critical_margin_timeout` over the same window (1,261 total payload entries; live-verified 2026-04-30 via direct `governance` DB query).

Both measures count the same recurring class from different observation surfaces (KG-write side vs. audit-event side); both are non-trivial.

Bucketed by Codex's diagnostic (concurrent mutable state | async runtime coupling | fanout/backpressure | authority/stale truth), the historical incident class lands ~31 of ~36 in buckets 1-3 — the class OTP was built to make boring. ExecutorPool especially is a hand-rolled fragment of the BEAM scheduler, written because the Python async ecosystem's anyio<->asyncpg seam keeps leaking. Each new coordination seam adds a Python wrapper that has its own bug class.

**Specific recurring papercuts this proposal addresses:**

1. **Multi-agent surface collisions.** The 2026-04-14 incident where another agent ran `git reset --hard HEAD` while UUID `7187b1c0` had ~400 lines of uncommitted WIP, recovered only via transcript replay. Class: two agents holding implicit mutable claims on the same surface (the working tree) with no negotiation primitive.

2. **Corpse locks.** Auto-recovered-stuck-agent KG entries firing repeatedly because there is no `:DOWN`-shaped notification when a holder process dies. The current recovery is timeout-based, post-hoc, and observable only as `critical_margin_timeout`.

3. **Watcher state-file races.** `_reload_if_stale ↔ save_state`: two concurrent writers can silently overwrite. Narrow window, no flock, latent.

4. **Dialectic session output without a runtime.** Session `95c9ddfd6bb09308` (2026-04-30) converged on a *TTL-surface-leases-with-holder-UUID-metadata* primitive as the v1 inter-agent coordination spec. The dialectic produced the leases-as-primitive direction; nothing has built it.

**Two-thread reading of `operator-flagged-rebuild` (council finding 2.2):** the same session also produced an insight (KG 2026-04-30T10:20:26+00:00) about dialectic having two distinct modes — Mode 1 pause-recovery (original design, 2-party, status-quo state machine correct) vs Mode 2 design-review (current dominant use; 2-party fails badly under low convergence; needs third-party-facilitator topology). Both threads carry the `operator-flagged-rebuild` tag. PR #247 closed the immediate state-machine bug class; **this RFC handles thread A only** (the leases-as-primitive that dialectic *produced as output*) and is **explicitly silent on thread B** (the Mode-2 dialectic-topology rebuild). Thread B is its own RFC and its own scope. Reviewers of *this* RFC should not assume thread B is being addressed here; if it isn't surfaced as a separate work item by the time this RFC reaches Phase B promotion, that's a sequencing failure, not a scope inclusion.

## 2. Decision

Build a **single-node Elixir/OTP service** that owns live coordination state for shared mutable surfaces, backed by Postgres for durable truth. Ship in **advisory mode first** (callers report intent, conflicts are logged but not blocked), promote to **enforcement mode** per-surface only after the log shows enforcement is safe.

**Explicit invariant** (from Codex synthesis, 2026-04-30):

> BEAM owns live coordination.
> Python owns governance truth.
> Postgres owns durable truth.
> No BEAM component may silently become source of truth for identity, EISV, KG, or calibration.

This invariant is non-negotiable. Any future RFC that proposes moving identity issuance, EISV math, KG writes, or calibration into the BEAM node must reopen the threat model and re-justify the polyglot tax.

## 3. Scope (in / out)

### 3.1 In scope (v0)

- A new Elixir application running on the governance MCP host (Mac), separate process from `unitares` Python.
- HTTP API surface (MCP exposure deferred to v1).
- `lease_acquire` / `lease_renew` / `lease_release` / `lease_status` / `handoff_offer` / `handoff_accept` / `heartbeat`.
- Postgres durable storage for lease records.
- BEAM-process monitoring for *local* holders (BEAM-resident processes).
- Heartbeat-TTL tracking for *remote* holders (Pi-side, Discord bot, TouchDesigner, any external worker).
- Telemetry forwarder emitting structured events to UNITARES `audit.tool_usage` (or equivalent — RFC question §7.6).
- Python client library (typed-absence return shape) for callers in the existing fleet.

### 3.2 Out of scope (v0)

Listed in the frontmatter `out_of_scope_explicit` field. Each is its own RFC.

### 3.3 Surfaces enumerated for v0 (advisory)

Initial advisory-only registrations. None are enforced in v0; the service logs intent, conflicts, and outcomes for the rollout-pattern §6 promotion gate.

| Surface kind | Example ID | Holder model | Notes |
|---|---|---|---|
| Repo file path | `file:///Users/cirwel/projects/unitares/src/x.py` | Agent UUID | Hermes / Claude / Codex worker writes |
| Dialectic session | `dialectic:/95c9ddfd6bb09308` | Agent UUID | Avoids reviewer assignment races |
| Resident lifecycle | `resident:/sentinel` | Process PID + UUID | Coordinates restart/upgrade windows |
| Capture window | `capture:/A,B,C` | Agent UUID | Calibration capture session ownership |
| TouchDesigner network region | `td:/op_path` | Process PID + UUID | Future, not v0 — listed for design fit |

The TouchDesigner row is illustrative; v0 does not implement TD bridge integration. It's named here so the surface ID schema (§7.2) is designed wide enough to cover the cases we anticipate without retrofit.

### 3.4 Provenance attribution: dialectic output vs RFC-side additions

Council finding 2.1 (dialectic-knowledge-architect): the v0 RFC framed itself as "shipping what dialectic produced," but actually shipped a superset. To keep the audit trail honest, here is the explicit attribution:

**From dialectic session 95c9ddfd6bb09308 directly (Mnemos KG 2026-04-30T10:22:54+00:00):**
- TTL-bound leases as the primitive (vs locks, vs file-path mutexes, vs KG-tag stigmergy)
- Postgres-backed durable storage
- Holder UUID metadata
- Validated `evidence_ref` on lease records
- Explicit fork/compaction handoff
- No Redis board, no agent chat, no KG promotion in v1
- Scope: whole-file repo paths

**RFC-side additions (claude_code/codex/gpt-5.5/grok/council convergence, 2026-04-30):**
- Elixir/OTP as the substrate (dialectic said "Postgres-backed"; substrate choice is *additional*)
- Surface taxonomy expansion: `dialectic:/`, `resident:/`, `capture:/`, `td:/` (dialectic said "whole-file repo paths" only)
- Single-node BEAM hard line; no distributed Erlang
- Dual-mode storage (BEAM-monitor for local, heartbeat-TTL for remote)
- Advisory-mode rollout pattern (mirrors IDENTITY_STRICT log→strict precedent, not from dialectic)
- Oban for durable jobs, PromEx for live metrics
- holder_class field per identity.md v2 alignment (council finding 1.1, post-dialectic)
- Co-location of outbox in same Postgres DB as audit.tool_usage (council finding 1.2, post-dialectic)
- §6.1 testable promotion gate (council finding §6.1, post-dialectic)

**Why this matters:** future readers, especially anyone reviewing whether dialectic "got built," should understand that this RFC is a substrate-choice-and-implementation-design that *takes the dialectic output as its primitive spec*. The dialectic's authority extends to the primitive; the substrate and architecture decisions are this RFC's authority.

### 3.5 Substrate side-benefits beyond the bug-class diagnostic

The bug-class diagnostic (§1) is the primary justification. Two substrate side-benefits worth naming explicitly because they address recurring operator pain that wasn't in the original framing:

1. **Hot code reload.** BEAM supports module-level swap on a running node. This directly dissolves `feedback_running-process-vs-master-commit.md` — the long-lived-resident-vs-master-commit drift that has cost real debugging time (~15min lost 2026-04-26 alone, plus repeated `ps -o etime` + `git log --since=` checks before scoping any resident-side fix). Out of scope to *automate* hot-reload deploys in v0, but the capability is a default, not a feature.

   **Caveat (ack-pass CONCERN §3.5):** the "module swap without lease loss" claim holds for **stateless modules** (HTTP plug handlers, SQL query helpers, telemetry forwarders). For stateful `LeaseHolder` GenServers under `DynamicSupervisor` with `one_for_one`, a hot-reload that triggers a `code_change/3` callback failure causes the process to restart, the lease supervisor's `Process.monitor/1` sees `:DOWN`, and the lease is released with `release_reason='down_local'` — spurious lease loss during hot reload. v0 discipline: hot-reload of stateful holder modules requires a `code_change/3` test pass before deploy; if uncertain, do a clean restart instead. Document in operator runbook.

2. **Native introspection.** `:observer.start()` against a running BEAM node shows the full supervision tree, mailbox depths, ETS tables, message rates, and process state — live, no instrumentation. Combined with PromEx (§7.6), this is more observability per dollar of engineering than Python+Sentinel currently delivers, and it lands without writing a single dashboard panel. Operator runbook (`docs/operations/lease-plane-operator-runbook.md`) names the specific incantations.

These are not the *reason* to spike Elixir, but they're real and worth the page they're written on.

## 4. Architecture

### 4.1 Storage modes (dual-mode)

The lease plane has two consistency stories, intentionally:

- **Local holder:** holder is a BEAM process on the same node. The supervisor monitors it (`Process.monitor/1`). On `:DOWN`, the lease is released or transferred per policy. Postgres lease row is the durable mirror, written on acquire and on release. Local-holder leases get the cleanest semantics — process death = lease death = `:DOWN` notification.

- **Remote holder:** holder is off-node (Pi, Discord bot, TD instance, any HTTP/RPC caller). The lease lives only in Postgres. The holder must heartbeat; if heartbeats stop for `>TTL`, a reaper releases the lease. No process monitoring; no `:DOWN`. This is the cross-host coordination primitive.

This is the answer to the Mac<->Pi distribution question: **no distributed Erlang clustering, ever.** Cross-host holders use the heartbeat-TTL path. The BEAM-monitor path is a Mac-only optimization.

### 4.2 Supervision tree (Mac-side, Elixir app)

```
UnitaresLeasePlane.Application
└── UnitaresLeasePlane.Supervisor (one_for_one)
    ├── UnitaresLeasePlane.Repo (Ecto)
    ├── UnitaresLeasePlane.Registry (process registry for local holders)
    ├── UnitaresLeasePlane.LeaseSupervisor (DynamicSupervisor)
    │   └── UnitaresLeasePlane.LeaseHolder (one per local lease)
    ├── Oban (durable job queue — reaper sweeps, handoff timeouts, audit-outbox drains)
    ├── UnitaresLeasePlane.HandoffServer (GenServer; proposal/accept/reject flow)
    ├── UnitaresLeasePlane.PromEx (Prometheus metrics for Sentinel/dashboard integration)
    └── UnitaresLeasePlane.HTTPEndpoint (Plug or Phoenix.Endpoint)
```

### 4.3 Python<->Elixir wire

Local HTTP only in v0. Bound to `127.0.0.1:<port>`, shared bearer token sourced from `~/.config/cirwel/secrets.env` (per memory `project_secrets-location.md`). MCP exposure deferred — the lease plane is internal infrastructure, not an agent-facing tool surface in v0.

Python clients import a small SDK module (`unitares.lease_client`) that returns typed-absence shapes (§4.5). No agent should be calling raw HTTP.

### 4.4 Postgres schema (sketch)

**DB co-location requirement (council finding 1.2):** the lease plane uses the **same physical Postgres database as UNITARES governance** (the `governance` DB on Homebrew PostgreSQL@17 port 5432). Tables live in a UNITARES-owned `lease_plane` schema. The lease plane's Elixir Ecto config holds a database role with `INSERT/UPDATE/SELECT` on `lease_plane.*` and **`INSERT-only` on `lease_plane.lease_plane_events`** (audit outbox). The §2 invariant ("Python owns governance truth, no BEAM component silently becomes source of truth for identity-bound records") requires that BEAM cannot drift away from the audit canon — co-location plus role-scoped privileges enforces that structurally.

```sql
CREATE SCHEMA IF NOT EXISTS lease_plane;

CREATE TABLE lease_plane.surface_leases (
  lease_id           uuid PRIMARY KEY,
  surface_id         text NOT NULL,           -- typed scheme: 'file:///...', 'dialectic:/...', 'resident:/...', etc.
  surface_kind       text NOT NULL,           -- parsed scheme prefix; 'file' | 'dialectic' | 'resident' | 'capture'
  holder_agent_uuid  uuid NOT NULL,
  holder_class       text NOT NULL,           -- 'process_instance' | 'substrate_earned' | 'role' (per identity.md v2)
  holder_kind        text NOT NULL,           -- 'local_beam' | 'remote_heartbeat' (IMMUTABLE per lease_id)
  holder_pid         text,                    -- for local_beam only: stringified PID; nullable for remote_heartbeat
  heartbeat_required boolean NOT NULL,        -- true => remote_heartbeat; false => local_beam (uses :DOWN as primary release)
  intent             text,                    -- caller-supplied free-text scope hint
  acquired_at        timestamptz NOT NULL DEFAULT now(),
  expires_at         timestamptz NOT NULL,    -- absolute expiry; renew/heartbeat extends this
  last_heartbeat_at  timestamptz,             -- only meaningful when heartbeat_required = true; null otherwise
  released_at        timestamptz,
  release_reason     text,                    -- see §4.4.1 below
  audit_session      text,                    -- writer's UNITARES session_id, for join into audit.tool_usage
  original_ttl_s     int NOT NULL,           -- ttl agreed at acquire-time; renew/heartbeat extends expires_at by this fixed value, NOT by caller-supplied (closes ack-pass BLOCK 2: indefinite-extension via malicious renew)
  earned_status      text NOT NULL DEFAULT 'provisional',  -- §7.8 substrate-earned-class flag; v0 acquisitions default 'provisional', promotion to 'earned' is a future migration after ≥30d stable operation
  CHECK (
    (heartbeat_required = true  AND holder_kind = 'remote_heartbeat') OR
    (heartbeat_required = false AND holder_kind = 'local_beam')
  ),
  CHECK (holder_class IN ('process_instance','substrate_earned')),  -- 'role' is rejected at the storage layer (closes ack-pass BLOCK 1: §7.8 role-rejection was application-layer only)
  CHECK (holder_kind  IN ('local_beam','remote_heartbeat')),
  CHECK (earned_status IN ('provisional','earned')),                  -- §7.8 visibility at the schema boundary
  CHECK (original_ttl_s > 0 AND original_ttl_s <= 3600)              -- hard cap: max 1h ttl, no indefinite leases
);

-- Lease-row field immutability enforced at UPDATE level (closes ack-pass BLOCK 1:
-- row-level CHECK is bypass-able by UPDATE). The trigger guards every field that
-- defines lease identity, holder identity, or TTL contract — release_reason,
-- released_at, last_heartbeat_at, expires_at remain mutable because the
-- legitimate state-machine transitions (release, heartbeat, renew) write them.
-- Migration 025 confirmed via direct UPDATE against the live governance DB
-- that surface_id, surface_kind, holder_agent_uuid, acquired_at, and
-- earned_status were silently mutable under migration 024's narrower trigger;
-- that gap is now closed.
CREATE OR REPLACE FUNCTION lease_plane.enforce_immutable_lease_fields()
RETURNS trigger AS $$
BEGIN
  IF NEW.holder_kind IS DISTINCT FROM OLD.holder_kind THEN
    RAISE EXCEPTION 'holder_kind is immutable per lease_id; release+reacquire to change';
  END IF;
  IF NEW.holder_class IS DISTINCT FROM OLD.holder_class THEN
    RAISE EXCEPTION 'holder_class is immutable per lease_id';
  END IF;
  IF NEW.original_ttl_s IS DISTINCT FROM OLD.original_ttl_s THEN
    RAISE EXCEPTION 'original_ttl_s is immutable per lease_id; renew uses this fixed value';
  END IF;
  IF NEW.surface_id IS DISTINCT FROM OLD.surface_id THEN
    RAISE EXCEPTION 'surface_id is immutable per lease_id; lease identity is bound to (surface_id, holder)';
  END IF;
  IF NEW.surface_kind IS DISTINCT FROM OLD.surface_kind THEN
    RAISE EXCEPTION 'surface_kind is immutable per lease_id';
  END IF;
  IF NEW.holder_agent_uuid IS DISTINCT FROM OLD.holder_agent_uuid THEN
    RAISE EXCEPTION 'holder_agent_uuid is immutable per lease_id; handoff uses release+reacquire, not in-place update';
  END IF;
  IF NEW.acquired_at IS DISTINCT FROM OLD.acquired_at THEN
    RAISE EXCEPTION 'acquired_at is immutable per lease_id';
  END IF;
  IF NEW.earned_status IS DISTINCT FROM OLD.earned_status THEN
    RAISE EXCEPTION 'earned_status is immutable per lease_id; promote new acquisitions, not historical rows';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER surface_leases_immutable_fields
  BEFORE UPDATE ON lease_plane.surface_leases
  FOR EACH ROW
  EXECUTE FUNCTION lease_plane.enforce_immutable_lease_fields();

-- One active lease per surface (the load-bearing invariant)
CREATE UNIQUE INDEX surface_leases_active_unique
  ON lease_plane.surface_leases (surface_id)
  WHERE released_at IS NULL;

-- Idempotency anchor for retry-safe acquire (see §4.5):
-- duplicate acquires from the same (surface_id, holder_agent_uuid) return the existing lease,
-- not a held_by_other error. The unique index above already implies this; we name it explicitly
-- because the contract depends on it.

CREATE INDEX surface_leases_holder ON lease_plane.surface_leases (holder_agent_uuid) WHERE released_at IS NULL;
CREATE INDEX surface_leases_active_expiry ON lease_plane.surface_leases (expires_at)  WHERE released_at IS NULL;

-- Audit outbox (UNITARES drainer reads from here; INSERT-only privilege for the BEAM role)
CREATE TABLE lease_plane.lease_plane_events (
  event_id        uuid PRIMARY KEY,
  ts              timestamptz NOT NULL DEFAULT now(),
  event_type      text NOT NULL,             -- 'acquire' | 'renew' | 'release' | 'conflict_held_by_other' | 'reaped_remote_ttl' | 'down_local' | 'forced'
  lease_id        uuid,                       -- nullable for conflict events that didn't land
  surface_id      text NOT NULL,
  surface_kind    text NOT NULL,
  holder_agent_uuid uuid,
  holder_class    text,
  advisory_mode   boolean NOT NULL,           -- true if caller was in advisory mode for this surface_kind at the time
  payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
  forwarded_at    timestamptz,                -- set by UNITARES drainer when projected into audit.tool_usage
  forward_attempts int NOT NULL DEFAULT 0
);

CREATE INDEX lease_plane_events_unforwarded
  ON lease_plane.lease_plane_events (ts)
  WHERE forwarded_at IS NULL;
```

The unique-active-per-surface partial index is the Postgres invariant. Even in advisory mode it guards against double-acquire at the storage layer; advisory means callers aren't *blocked from skipping the lease entirely*, not that the storage has no integrity.

#### 4.4.1 release_reason vocabulary

Distinct values exist so telemetry can distinguish *which path* released the lease:

| Value | Meaning |
|---|---|
| `normal` | Caller called `lease_release` while holder still alive |
| `down_local` | Local-BEAM supervisor saw `:DOWN`, wrote release synchronously |
| `reaped_after_supervisor_failed` | Reaper found a local_beam lease whose supervisor write never landed (process actually dead) |
| `reaped_local_ttl` | Reaper found a local_beam lease past `expires_at` whose holder process is *still alive but stuck* — in-process timer stopped firing but supervisor `:DOWN` never fired (closes ack-pass CONCERN: previously this case got `reaped_remote_ttl` which misclassified the holder kind) |
| `reaped_remote_ttl` | Reaper found a remote_heartbeat lease past `expires_at` |
| `handoff` | Lease transferred via `handoff_accept`; new lease_id created for the new holder |
| `forced` | Operator-issued force-release (see §7.10) |

#### 4.4.2 Heartbeat / renew semantics

`lease_renew` and `lease_heartbeat` are aliased: **both update `expires_at = now() + original_ttl_s` and (if heartbeat_required) `last_heartbeat_at = now()`** in a single atomic UPDATE. The TTL applied is **always the immutable `original_ttl_s` stored at acquire time**, never a caller-supplied value (closes ack-pass BLOCK 2: malicious or buggy renew with `ttl_s=86400` cannot indefinitely extend a lease). The `/v1/lease/renew` endpoint accepts no `ttl_s` parameter; if a caller wants a longer lease they must release and re-acquire.

This closes the in-flight-heartbeat-vs-reaper-sweep race (council finding 4): the reaper's sole predicate is `expires_at < now() AND released_at IS NULL`, applied to all holder kinds uniformly. There is no separate `last_heartbeat_at < threshold` predicate.

Local-BEAM holders also call `lease_renew` from an in-process timer at cadence `original_ttl_s/3` so that a supervisor crash (rare; the supervisor itself is supervised) doesn't leave a corpse lease for longer than `original_ttl_s`. Default local_beam `original_ttl_s` is **30s** (council finding 2 — the prior `90s` window was the ghost-lease problem). Remote_heartbeat defaults: **Mac 90s (provisional, unmeasured); Pi 1000s (measured §7.5, v0.9)**. Hard maximum `original_ttl_s = 3600` enforced by Postgres CHECK constraint.

### 4.5 Typed-absence protocol

All RPC return shapes are discriminated unions. Python side: Pydantic discriminated unions (this is the *zero-cost win* §9.2 — adopt across the codebase regardless of whether Elixir ships).

```
Acquire result:
  {ok: true,  lease: {...}, idempotent: false}     -- new lease created
  {ok: true,  lease: {...}, idempotent: true}      -- (surface_id, holder_agent_uuid) already had an active lease;
                                                     existing lease returned (retry-on-lost-response safe)
  {ok: false, error: "held_by_other",       held_by_uuid, expires_at}
  {ok: false, error: "permission_denied",   reason}
  {ok: false, error: "schema_invalid",      detail}
  {ok: false, error: "service_unavailable"} -- fall through to advisory-skip on caller side

Status result:
  {ok: true, lease: {...}}                  -- active lease
  {ok: true, lease: null}                   -- no active lease
  {ok: false, error: "service_unavailable"}

Release / Renew / Heartbeat / Handoff:
  {ok: true}
  {ok: false, error: "not_found" | "expired" | "not_holder" | "already_released" | "service_unavailable"}
```

**Acquire idempotency contract (council finding 3):** the acquire endpoint is idempotent on `(surface_id, holder_agent_uuid)`. Implementation: the SQL acquire path is a single transaction that either INSERTs a new row (returning `idempotent: false`) or, on unique-index violation where the existing active row's `holder_agent_uuid` matches the requester, returns that row with `idempotent: true`. **Only when the existing holder is a different UUID** is `held_by_other` returned. This eliminates the retry-on-lost-response bug class where a caller's TCP-dropped acquire response causes them to receive `held_by_other` against their own already-acquired lease.

**Param drift on idempotent retry (closes ack-pass CONCERN):** when the original acquire's `intent`, `ttl_s`, or `holder_pid` differs from the retry's, the **stored values from the original acquire win** — the lease row is unchanged, the existing lease is returned with `idempotent: true`. The response includes a `drift_warning` field listing any parameters whose new value was discarded:

```
{ok: true, lease: {...}, idempotent: true, drift_warning: ["ttl_s", "intent"]}
```

If the caller wanted a longer TTL or different intent, they must release and re-acquire. This makes the silent-discard explicit at the contract layer rather than hiding it.

`{ok: false, error: "service_unavailable"}` is the **advisory-mode escape valve**: if the lease plane is down, the Python caller logs the absence and proceeds. v0 enforcement does not block on lease-plane availability. This is the same shape as the IDENTITY_STRICT='log' rollout.

## 5. API surface (v0)

```
POST /v1/lease/acquire
  body: { surface_id, surface_kind, holder_agent_uuid, holder_kind, ttl_s, intent? }
  returns: typed-absence (acquire result above)

POST /v1/lease/renew
  body: { lease_id }                          -- no ttl_s: renew extends by the immutable original_ttl_s; see §4.4.2
  returns: typed-absence

POST /v1/lease/release
  body: { lease_id, release_reason }
  returns: typed-absence

GET  /v1/lease/status
  query: surface_id
  returns: typed-absence (status result above)

POST /v1/lease/heartbeat
  body: { lease_id }
  returns: typed-absence

POST /v1/lease/handoff/offer
  body: { lease_id, to_holder_agent_uuid, ttl_s }   -- ttl_s here is the NEW lease's original_ttl_s after accept (handoff is release-and-reacquire, not in-place update); offer-pending timeout is server-internal
  returns: typed-absence with handoff_id

POST /v1/lease/handoff/accept
  body: { handoff_id }
  returns: typed-absence
```

All endpoints accept and return JSON. Bearer-auth (matches existing UNITARES governance MCP pattern).

## 6. Rollout (advisory -> selective enforcement)

Mirror the precedent set by `path1-sync-fingerprint-check.md` and the IDENTITY_STRICT promotion (Phase A 10-day observation reported 0 hijack events on 2026-04-30, recommending Phase B).

### 6.1 Phase A — Advisory (week 1-3)

- Lease plane is up.
- Hermes / Claude Code workers / Codex / dispatch / ship.sh are *integrated as callers* but **never blocked** on conflicts.
- Every conflict (`held_by_other`, `expired`, `not_holder`) is recorded in `lease_plane.lease_plane_events` (with `event_type = 'conflict_held_by_other'`, `advisory_mode = true`) and emits a PromEx counter.
- Postgres unique-active index is the only enforcement layer; a caller that *skips* lease acquisition entirely is not blocked but also not visible to the conflict log.

**Why this is hard to gate honestly (council finding §6.1):** the advisory log only sees callers who registered. A 2026-04-14-style git-reset incident from an *unintegrated* caller produces zero conflict signal. The promotion gate must therefore distinguish two failure modes:

- **Type A — observed conflicts among integrated callers** (queryable from `lease_plane_events`)
- **Type B — surface collisions from unintegrated callers** (not queryable; requires post-hoc cross-check against external evidence)

**Promotion gate to Phase B per surface_kind requires ALL of:**

1. ≥14 days of advisory-mode telemetry for that surface_kind.
2. 0 service-availability incidents on the lease plane itself during the window (Sentinel `/v1/lease/status?surface_id=__healthcheck__` uptime ≥ 99.5%).
3. **Type A signal**: at least one observable conflict event:
   ```sql
   SELECT count(DISTINCT surface_id) FROM lease_plane.lease_plane_events
    WHERE event_type = 'conflict_held_by_other'
      AND advisory_mode = true
      AND surface_kind = $1
      AND ts > now() - interval '14 days';
   ```
   Count ≥ 3 distinct surface_id values is necessary; sufficiency requires criterion (4).
4. **Type A→incident linkage**: at least one conflict in (3) where the *blocked caller's* `audit_session` joins to a UNITARES KG entry from within ±1 hour describing a concrete surface-collision symptom (file overwrite, lost work, etc.). This is the operational rephrasing of "we would have prevented a real bug" — observable via SQL join, not counterfactual reasoning.
5. **Type B audit**: a fleet-wide caller-registration audit shows ≥ 95% of callers that mutated this surface_kind during the window passed through the lease plane. This is the unintegrated-caller-bypass detector. If <0.95, the integration story has gaps and Phase B promotion would block the wrong population.

   **SQL prerequisite (closes ack-pass CONCERN):** `audit.tool_usage.payload jsonb` does not currently have a standardized key for surface targets — different callers emit `payload->>'path'`, `payload->>'file'`, etc. Before criterion 5 is evaluable, a **payload-shape standardization pass** must establish a canonical `payload->>'surface_id'` key for write-class events on each surface_kind that's a Phase B candidate. Once standardized, the query is:
   ```sql
   WITH writes AS (
     SELECT count(*) AS n FROM audit.tool_usage
      WHERE tool_name LIKE 'write.%'
        AND payload->>'surface_id' LIKE $1 || ':%'   -- e.g. 'file:%'
        AND ts > now() - interval '14 days'
   ), acquires AS (
     SELECT count(*) AS n FROM lease_plane.lease_plane_events
      WHERE event_type = 'acquire'
        AND surface_kind = $1
        AND ts > now() - interval '14 days'
   )
   SELECT (acquires.n::float / NULLIF(writes.n, 0)) AS coverage_ratio FROM writes, acquires;
   ```
   Promotion requires `coverage_ratio >= 0.95`. The payload-standardization is itself a small spec task that must precede Phase B promotion of any write-heavy surface_kind. For non-write surface_kinds (`dialectic:/`, `resident:/`), criterion 5 is N/A — those don't have a write-side audit signal.
6. **Adversarial-bypass cross-check**: telemetry includes a write-side after-the-fact check (file-mtime delta vs. lease-acquired window) showing no detectable un-acquired writes during the window. Council finding 3.3.

If criteria (1)-(6) are met for a surface_kind, that surface_kind is eligible for Phase B promotion. Each promotion is a single config flag flip; no code change. Demotion back to advisory is the same flag, reversible at any time.

### 6.2 Phase B — Selective enforcement (week 4+)

Per-surface-kind enforcement flags. Each surface kind promoted independently.

- `dialectic:/...` likely first (lowest external blast radius, dialectic infra already has manual fallbacks).
- `resident:/...` second (already has launchd-level protection, lease layer adds restart-window safety).
- `file:/...` last (highest blast radius, every code-edit caller path must be integrated).

Reverting a surface kind from enforcement back to advisory must be a single config flag, not a code change.

### 6.3 Phase C (deferred) — additional substrates

Basin coordinator, dialectic GenServer, resident supervision: each gets its own RFC that re-references this one as the substrate-fit precedent.

## 7. Open RFC questions (council MUST answer)

These are the ExecutorPool-shaped questions — get them wrong and you reinvent its bug class one altitude up.

### 7.1 Holder identity model — RESOLVED in v0.2

Council finding 1.1 (dialectic-knowledge-architect) flagged that keying leases on naked `holder_agent_uuid` conflicts with `docs/ontology/identity.md` v2: UUID is performative for ephemeral process-instance agents (Hermes/Claude/Codex tabs), and a held lease "owned by UUID X" can outlive the process whose claim it actually represents.

**Resolution (see §4.4 schema):** lease rows now carry `holder_class` (`process_instance` | `substrate_earned` | `role`) alongside `holder_agent_uuid`, `holder_kind`, and `holder_pid`. Process-instance holders MUST present a process-instance ownership token (continuity_token or its successor) at acquire time; substrate-earned holders (Lumen, hardcoded-UUID residents) acquire on UUID alone. Roles cannot hold leases (rejected at acquire with `permission_denied`, reason `role_holders_unsupported`). The `holder_class` field lets the audit trail distinguish phenomenological-continuous claims from performative ones.

`holder_kind` is **immutable per `lease_id`** — switching from `remote_heartbeat` to `local_beam` mid-life requires release+reacquire (council finding 3.1). Postgres CHECK constraint enforces the (heartbeat_required, holder_kind) pair coherence.

### 7.2 Surface ID schema — RESOLVED in v0.7

Opaque string, or typed scheme? Council pass v0.7 (parallel: dialectic-knowledge-architect, feature-dev:code-reviewer, live-verifier) found three-voice convergence on "no storage-layer CHECK on `surface_id` despite RFC framing it as 'validatable'", plus a self-contradiction between §3.3's enumerated surfaces (5 schemes including `capture:/`) and §7.2's tentative grammar (4 schemes, no `capture:`).

**Resolution: typed scheme, defense-in-depth, with canonical scheme list.**

#### 7.2.1 Canonical scheme list (v0)

Authored once here; §3.3 surface enumeration MUST stay consistent with this list.

| Scheme | Surface_kind | Status v0 | Notes |
|--------|--------------|-----------|-------|
| `file://` | `file` | active | repo file paths; canonicalization rules in §7.2.4 |
| `dialectic:/` | `dialectic` | active | dialectic session IDs |
| `resident:/` | `resident` | active | resident lifecycle handles |
| `capture:/` | `capture` | active | calibration capture windows |
| `td:/` | `td` | reserved | TouchDesigner regions; not implemented v0 |

Single-slash form (`scheme:/path`) is canonical for all schemes *except* `file://` (kept double-slash for `file://` filesystem-URI tradition; the trailing `/` of an absolute path provides the third slash, e.g., `file:///Users/cirwel/...`).

#### 7.2.2 Grammar enforcement (defense-in-depth)

Three layers, each named:

1. **Postgres CHECK constraint** (migration 026 — required before Phase A): `CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)')`. Storage-layer rejection of malformed values. Live-verifier DRIFT-A confirmed migration 024 has no such CHECK today; this closes the gap.
2. **Pydantic field_validator** on `AcquireRequest.surface_id` and `LeaseRecord.surface_id`: regex match against the canonical scheme list. Caller-side rejection before HTTP. Live-verifier confirmed today's `Field(min_length=1)` is the only enforcement on the Python side.
3. **Elixir Ecto changeset validation**: enum-based scheme parser, compile-time exhaustive over the `@surface_schemes` module attribute. Server-side rejection before transaction starts.

Layers (1) and (2) are required Phase A gates (§9 checklist). Layer (3) is Elixir-side and lands with the BEAM service implementation.

#### 7.2.3 surface_kind ↔ surface_id consistency — DB-enforced via generated column

surface_kind MUST NOT be application-only. Three options were considered:

- (a) **Generated column** (chosen v0.7): `surface_kind text GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED`. Single source of truth, derived from surface_id at storage time, impossible to drift. Caller-supplied `surface_kind` becomes redundant and SHALL be removed from `AcquireRequest` and the §5 endpoint body. Live-verifier confirmed `surface_leases` is empty in production, so the migration-026 conversion (DROP COLUMN + ADD COLUMN ... GENERATED) is safe.
- (b) **DB CHECK pair** (fallback if (a) is too disruptive at migration time): keep `surface_kind` as a regular column, add a CHECK constraint in migration 026 binding scheme prefix to surface_kind. Caller still supplies both; server-side enforcement is at the storage layer, not the application layer. Mismatch rejected with `schema_invalid`.
- (c) ~~Application-only~~ — REJECTED. surface_kind enforcement at the application layer alone is insufficient because any direct SQL writer (governance-side projection, future operational scripts) bypasses it.

**Adopted (v0.7): option (a) generated column.** Migration 026:

```sql
ALTER TABLE lease_plane.surface_leases DROP COLUMN surface_kind;
ALTER TABLE lease_plane.surface_leases
  ADD COLUMN surface_kind text
  GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED;

-- The grammar CHECK on surface_id (§7.2.2) keeps surface_kind in the canonical
-- vocabulary; no separate surface_kind CHECK needed since the value is derived.
ALTER TABLE lease_plane.surface_leases
  ADD CONSTRAINT surface_id_grammar
  CHECK (surface_id ~ '^(file://|dialectic:/|resident:/|capture:/|td:/)');
```

If the empty-table assumption changes between now and migration 026 ship time (i.e., Phase A advisory traffic lands first), fall back to option (b): keep the column, add the CHECK-pair binding scheme→kind, and treat caller-supplied surface_kind as a redundant input that the server validates against the parsed prefix. Either way, **the storage layer is authoritative** — application-only enforcement is not on the table.

Cross-API impact (option a): `AcquireRequest.surface_kind` is removed from §5 `/v1/lease/acquire` body. `LeaseRecord.surface_kind` remains in responses (read-only echo of the stored generated value).

#### 7.2.4 file:// canonicalization

See **§7.12** for the v0 canonicalization rule and the v1 forward-compat path with §7.9 content-addressing. §7.2 commits that callers MUST use the canonicalization helper before acquire/status/release; §7.12 specifies the helper.

#### 7.2.5 Cardinality bounds (closes dialectic CONCERN-4)

- **Per surface:** exactly 1 active lease, enforced by the partial unique index `surface_leases_active_unique`. Live-verifier confirmed.
- **Per holder:** unbounded by design. Hermes-style multi-file edits need it; capping per-holder would block a real workload.

**Threat model (v0):** callers are authenticated holders; runaway-acquisition is a holder-bug class, not an external-attacker class. Mitigation:

```sql
-- Sentinel alarm threshold: 100 concurrent leases per holder
SELECT count(*) FROM lease_plane.surface_leases
WHERE holder_agent_uuid = $1 AND released_at IS NULL > 100
```

Threshold of 100 is initial; tunable via Sentinel config. The alert is **reactive** — it fires after a holder is already at threshold. Per-holder rate-limiting (acquires/sec) is **deferred to v1** and is the correct response if telemetry shows real-world holders crossing 100 concurrent leases routinely. v0 ships the alert without the throttle, with explicit acknowledgement that it is reactive — not a defense against adversarial fan-out, only an early-warning for holder-bug fan-out.

#### 7.2.6 Pruning policy for `surface_leases` (closes code-reviewer CONCERN-3)

Released rows accumulate indefinitely without explicit pruning. Migration 024 has no DELETE trigger. The partial unique index continues to work efficiently (released rows are excluded from the index), but the table itself grows unbounded.

**v0 pruning:** Oban-scheduled job runs daily and DELETEs `surface_leases` rows where `released_at < now() - interval '30 days'`. Audit history is preserved via `lease_plane_events` (which has its own 30-day-after-`forwarded_at` pruning, §7.6) and via the projection into `audit.tool_usage` (canonical, never pruned by lease-plane code).

#### 7.2.7 Status/release path normalization (closes code-reviewer CONCERN-4)

`/v1/lease/status?surface_id=...` and `/v1/lease/release` body must apply the same canonicalization rules as acquire. The Python client helper SHALL apply normalization at the `LeasePlaneClient` method boundary, not at the transport boundary, so all three call paths (acquire/status/release) share the same normalization.

#### 7.2.8 §6.1 promotion-gate criterion 5 cross-reference

§6.1 criterion 5's `payload->>'surface_id' LIKE $1 || ':%'` predicate assumes un-encoded `surface_id` in the audit payload. The payload-shape standardization pass (named in §6.1 as a Phase B prerequisite) MUST commit to writing canonicalized `surface_id` (per §7.2.4) into `audit.tool_usage.payload`, with no percent-encoding. Cross-tracked in §9 checklist.

#### 7.2.9 Forward-compat for unknown schemes

Per dialectic CONCERN-3, scheme additions are migrations: a new `surface_kind` requires (a) Postgres migration extending the CHECK constraint, deployed before (b) the BEAM module rollout that begins INSERTing the new scheme. The `unitares_doctor.py` script SHALL be extended to lint that no Elixir source mentions a scheme not in the live CHECK. Tracked in §9 checklist as a Phase B prerequisite, not Phase A.

Scheme deprecation/migration: see §7.11.

### 7.3 Conflict semantics on `held_by_other` — RESOLVED in v0.7

What's the caller default behavior? Council pass v0.7 reframed this from a single global default to a default-with-per-surface-kind-override slot. Three voices converged that "abort everywhere" is the *safe* default but not universally correct: file edits want loud-fail-on-collision, dialectic-reviewer assignment is friendlier with bounded queueing, resident lifecycle wants loud-fail, capture is too long to wait for. Council also flagged that abort-by-default with no backoff guidance ships a thundering-herd vector at the Postgres index-contention layer, and that the current `AcquireHeldByOther` shape gives callers insufficient information for sane retry logic.

**Resolution: abort by default, with per-surface-kind override slot for Phase B promotion. Extended `held_by_other` shape. Mandatory backoff guidance for callers.**

#### 7.3.1 Global default and override slot

**v0 ships abort-only globally.** All surface_kinds default to `conflict_default = "abort"`. Per-surface-kind override is a Phase B promotion-time configuration, not a v0 deployment-time flag.

| surface_kind | v0 default | Anticipated Phase B target | Rationale |
|--------------|------------|----------------------------|-----------|
| `file` | `abort` | `abort` | Concurrent edit → merge conflict → loud failure is correct semantic |
| `dialectic` | `abort` | `wait_with_deadline=2s` | Reviewer assignment is fast; queueing friendlier than retry-loop |
| `resident` | `abort` | `abort` | Only one restart at a time; loud-fail correct |
| `capture` | `abort` | `abort` | Capture is long (~minutes); wait blocks calibration substrate |
| `td` | `abort` | TBD | Reserved; not implemented v0 |

Override values for Phase B: `{abort, wait_with_deadline=Nms, handoff_offer}`. The Phase B promotion config (§6.2) gains a `conflict_default` field per surface_kind. Promoting `dialectic:/` to `wait_with_deadline=2000` is anticipated but not committed in v0.

**Out of scope for v0:** queue-with-bounded-depth and speculative-wait-with-deadline-per-call (per dialectic CONCERN-7). Both are coherent extensions; deferred to v1 if real-world telemetry indicates the abort+per-kind-override matrix is insufficient.

#### 7.3.2 Extended `AcquireHeldByOther` typed-absence shape

Council code-reviewer BLOCK-3 + CONCERN-6: callers currently cannot (a) distinguish "same stuck holder across retries" from "rotating cast of short-lived holders", or (b) correlate concurrent multi-surface acquires with which surface is blocked. v0.7 extends the §4.5 shape:

```
{ok: false, error: "held_by_other",
 surface_id,                  -- echo of the requested surface (multi-acquire correlation)
 blocking_lease_id,           -- which lease is blocking (retry-discrimination)
 held_by_uuid,
 expires_at,
 retry_after_hint_ms}         -- min(remaining_ttl_ms, 5000); advisory, not enforced
```

`retry_after_hint_ms` is server-computed at conflict time. Callers MAY ignore it. The hint exists so well-behaved callers can rate-limit themselves without parsing `expires_at` and computing the delta.

§4.5 typed-absence spec, §5 endpoint table, and `src/lease_plane/models.py` `AcquireHeldByOther` Pydantic model all need updating to match. Tracked in §9 checklist.

#### 7.3.3 Backoff guidance (closes dialectic BLOCK-6 — thundering-herd)

"Caller decides whether to retry" with no rate-limit guidance is a thundering-herd vector. Concrete data path: ship.sh fans out N parallel session worktrees, all attempting `lease_acquire('file:///<path>')`. One wins; N-1 receive `held_by_other`. If they retry immediately, they convoy on `surface_leases_active_unique` — O(N) acquires per contention window, each one a row INSERT-or-409 against the same partial unique index.

**Caller library contract (v0.7):**

- The `LeasePlaneClient` Python helper SHALL implement jittered exponential backoff if its `acquire_with_retry()` convenience method is used: floor 100ms, ceiling 5s, full jitter (per AWS Architecture Blog convention).
- If the caller wraps `acquire()` with custom retry, they SHOULD honor `retry_after_hint_ms` as a lower bound, then add their own jitter.
- The lease plane itself does NOT enforce backoff server-side; this is a contract on caller libraries.

The bare `acquire()` method remains single-shot (no built-in retry) so callers who genuinely want immediate-fail-on-conflict (e.g., interactive operator commands) keep that semantic.

#### 7.3.4 Handoff opt-in semantics

Handoff is opt-in per-surface-kind, used for specific surface kinds where the holder participates in lifecycle coordination. Anticipated v1 wiring:

- `resident:/` during planned restart: outgoing resident issues handoff_offer to incoming resident before exiting.
- `dialectic:/` during reviewer reassignment: facilitator issues handoff_offer to new reviewer before tearing down the dialectic session.

Hermes, Claude Code, Codex sessions, and most ephemeral holders do NOT respond to handoff offers — for them, handoff is undefined behavior. v0 ships handoff endpoints (per §5) but no holder classes wire handoff acceptance; first wiring lands with the first resident-class promotion to enforcement.

#### 7.3.5 HTTP status convention (closes code-reviewer NIT-1)

Live-verifier confirmed Elixir router returns HTTP 409 on `held_by_other` with the typed-absence body. v0.7 commits this:

- `/v1/lease/acquire` returns HTTP 409 with the §7.3.2 body on `held_by_other`.
- All other typed-absence error shapes return HTTP 200 with `ok: false` in the body (transport-level success, application-level failure).
- HTTP 409 is reserved for `held_by_other`; HTTP 4xx other than 409 indicates transport-level failure (auth, malformed request, route not found).

The Python `_urllib_transport` already handles HTTP 409 by parsing the body as JSON and returning it; this convention is now a contract requirement, not an implementation detail. §5 endpoint table updated to note "409 on held_by_other; 200 + ok:false otherwise".

### 7.4 Reaper authority on local-holder death — RESOLVED in v0.2

Council finding 2 (code-reviewer) flagged that supervisor-retry-exhausted plus reaper-not-yet-swept produces a 90s ghost-lease window where a dead holder still blocks fresh acquires.

**Resolution (see §4.4.2):** local_beam leases default to **30s TTL** (not 90s) and the holder process refreshes via in-process timer at TTL/3 (10s). Single reaper predicate `expires_at < now() AND released_at IS NULL` covers both holder kinds. If supervisor `:DOWN` write succeeds: lease released within ~ms via `release_reason='down_local'`. If supervisor write fails (Postgres flapping) and the in-process timer also stops (process is dead): lease auto-expires within 30s and the reaper sweep at next tick (≤30s cadence) writes `release_reason='reaped_after_supervisor_failed'`. Worst-case ghost window is 60s, not 90s; routine case is sub-second.

Reaper sweep cadence: 30s. Tunable via env, alarmed via Sentinel if reaper falls behind (`SELECT count(*) FROM lease_plane.surface_leases WHERE expires_at < now() - interval '60s' AND released_at IS NULL` should equal 0 in steady state).

### 7.5 Heartbeat cadence + TTL math — RESOLVED in v0.9 (Pi measured)

Council finding 4 (code-reviewer): heartbeat must extend `expires_at`, not just touch `last_heartbeat_at`, otherwise the reaper races in-flight heartbeats. **Resolved in §4.4.2:** `lease_renew` and `lease_heartbeat` are aliased and both update `expires_at = now() + ttl` atomically.

Council NIT 4.4 (dialectic-knowledge-architect): defaults of `90s/25s` (Mac) and `180s/60s` (Pi) were hand-tuned guesses, not derived from observed heartbeat-loss telemetry. **Resolved in v0.9 for the Pi remote_heartbeat path** by mining 48 days of `audit.events WHERE event_type='eisv_sync'` rows (Steward Pi→Mac sync, n=8452 since 2026-03-15) instead of waiting for purpose-built instrumentation; the existing Steward audit trail already characterizes the Pi↔Mac heartbeat path the same way new instrumentation would.

**Measurement window:** 2026-04-22 → 2026-04-29 04:00 UTC-6, the most recent continuous-cadence stretch (n=1839 successful syncs, before the 2026-05-01 operator-induced Steward pause). Wider windows pulling in post-pause data inflate p99 with the pause itself; the chosen window is steady-state.

**Measured gap distribution (Pi → Mac):**

| stat | value |
|---|---|
| p50 | 302s (matches Steward's 300s nominal cadence) |
| p95 | 310s |
| p99 | **621s** |
| max (in-window) | 34700s (~9.6h — single tail event, indicates rare extended-loss scenarios that cannot be designed away with TTL alone) |

**Recommended Pi `original_ttl_s` = ⌈p99 × 1.5⌉, rounded for operator legibility = 1000s** (~16.7min). Heartbeat cadence remains `original_ttl_s/3 ≈ 333s`, which is comfortably above Steward's natural 300s cadence (every Steward sync refreshes well within TTL).

**Mac path remains provisional** (90s/25s), unmeasured. Mac residents (Watcher/Sentinel/Vigil) hit the lease plane over loopback rather than via the Pi↔Mac sync path, and their gap distribution is governed by local async-loop scheduling, not Steward telemetry. Phase B promotion of any Mac-resident `remote_heartbeat` surface should similarly mine `audit.events` for the relevant resident's natural cadence before locking defaults — the §7.5 methodology (use existing audit traces; ≥7d window; p99 × 1.5 = TTL; keep heartbeat cadence at TTL/3) is now the standing rule for any remote_heartbeat caller.

**Why audit-mining beats fresh instrumentation here:** the §7.5 carve-out originally said "instrument the existing Pi↔Mac heartbeat path (Steward sync, anima-mcp HTTP heartbeats) and measure the actual gap distribution over ≥7 days." Steward already writes one `event_type='eisv_sync'` audit row per successful sync — that *is* the instrumented heartbeat trace, retroactively. The 7-day waiting period was a measurement-collection assumption that turned out to be unnecessary; we have ~7 weeks of data already.

### 7.6 Audit wire to UNITARES

How do lease events land in UNITARES `audit.tool_usage`?

- **Synchronous RPC** from Elixir to a UNITARES MCP endpoint per lease event. Cleanest semantics; couples lease-plane availability to UNITARES MCP availability.
- **Fire-and-forget telemetry** to a queue; UNITARES drains. Decoupled; introduces a "did the audit row ever land?" question.
- **Both:** Elixir writes its own audit-mirror table in Postgres synchronously; a UNITARES-side process drains into the canonical audit table. Decoupled and durable.

**Resolved in v0.2** (council findings 1.2, 3.4, 5):

**DB co-location** (see §4.4): `lease_plane.lease_plane_events` lives in the *same physical Postgres database* as `audit.tool_usage` (the `governance` DB), in a UNITARES-owned schema, with the BEAM role holding INSERT-only on the outbox. This structurally enforces the §2 invariant that BEAM cannot become a silent source of truth for identity-bound audit records.

**Transactional coherence** (council finding 5): the lease state change and the outbox row write occur in the same SQL transaction. If the outbox write fails (constraint, disk, etc.), the lease state change rolls back and the caller receives an error — this is the correct behavior, the alternative would split the truth. There is no path where a lease persists without its corresponding event row.

**Drainer:** an Oban-scheduled worker (running in the BEAM node, but using only INSERT-only privileges on the outbox to read; the actual projection into `audit.tool_usage` happens via a Python-side UNITARES drainer that has SELECT on the outbox and INSERT on `audit.tool_usage`). This split keeps the BEAM<->governance-truth boundary clean: BEAM writes the outbox; UNITARES (Python) projects forward.

**Unbounded growth guard** (council finding 5): Sentinel alarm fires if `SELECT count(*) FROM lease_plane.lease_plane_events WHERE forwarded_at IS NULL` exceeds 10,000 rows or the oldest unforwarded row is >1h old. Outbox rows are pruned 30 days after `forwarded_at` (configurable).

**PromEx alongside:** PromEx exports live operational metrics (acquire/release/conflict rates, holder counts, reaper actions). PromEx is **not canonical** — it is operational alerting only. The outbox (and downstream `audit.tool_usage`) is canonical for any decision that affects governance state, especially the §6.1 promotion-gate criteria. PromEx counter divergence from outbox count under load (BEAM hot-reload zeros gauges, dropped Prom scrapes, etc.) is expected and does not invalidate the outbox truth.

The **§6.1 promotion-gate denominator is the outbox**, never PromEx.

`audit.tool_usage` schema note (live-verifier finding): the existing `audit.tool_usage` table has columns `(ts, usage_id, agent_id, session_id, tool_name, latency_ms, success, error_type, payload jsonb)` and is RANGE-partitioned by `ts`. Lease events project into this shape via `tool_name='lease.{event_type}'` with surface_id, surface_kind, holder_class, lease_id, advisory_mode stuffed into `payload`. No schema migration to `audit.tool_usage` is required.

### 7.7 What if the lease plane itself is down?

Already covered in §4.5: callers receive `{ok: false, error: "service_unavailable"}` and proceed advisory-style. v0 enforcement does not block on lease-plane availability. Phase B selective-enforcement does — a `file:/` surface that's been promoted to enforce *will* block writes if the lease plane is unreachable. That's an operational hazard worth naming.

**Mitigation:** the lease plane itself is supervised by launchd (or homebrew services); restart on crash; alarm on prolonged downtime. A specific Sentinel check should monitor `/v1/lease/status?surface_id=__healthcheck__` and alert if unreachable for >5min. Document in operational runbook before any surface kind reaches enforcement.

### 7.8 Lease plane's own identity in UNITARES (council finding 4.1)

The Elixir application is itself a process making writes that land in `audit.tool_usage`. What is its identity in UNITARES governance?

Three options:

- **Self-onboard as a regular agent.** Lease plane calls `onboard(force_new=true)` at startup, gets an agent_uuid, runs check-ins. Subjects the lease plane to its own EISV trajectory and verdict pipeline. Recursion risk: the lease plane needs leases for its own audit-outbox writes? (No — its writes to `lease_plane.lease_plane_events` are INSERT-only on a single-purpose table; no lease required because it owns the surface by privilege, not coordination.)

- **Privileged service identity (BypassAgent class).** New identity class in `docs/ontology/identity.md` for system-level services that issue audits but are not subject to verdict-gating. The lease plane, the audit drainer, the schema migrator would all be this class. Cleaner separation but requires an ontology amendment.

- **Substrate-earned (Lumen-class) identity.** Reuses an existing class. Hardcoded UUID for the lease plane, audit lineage clear, no ontology change. Closest to status quo.

**Tentative:** option 3 (substrate-earned, per `docs/ontology/identity.md` Pattern — Substrate-Earned Identity appendix; "Lumen-class" was an earlier informal phrasing — the canonical term is just **substrate-earned**, with Lumen as the canonical instance). The lease plane is genuinely a substrate, not a participating agent — its UUID is hardcoded, its audit rows are tagged with that UUID, and it does not run governance check-ins. This minimizes ontology churn. The ontology amendment for option 2 (BypassAgent) can come later if multiple system-level services emerge that genuinely need to be a class together.

**Important caveat (ack-pass CONCERN):** per the appendix's own rules, substrate-earned identity is *earned* through N≥threshold restarts of behavioral consistency on dedicated substrate. **At v0 ship the lease plane has zero restarts and zero accumulated behavioral history — the pattern is NOT yet earned.** The hardcoded UUID is **provisional**, treated as substrate-earned in code path but flagged in audit rows as `holder_class='substrate_earned', earned_status='provisional'` (or equivalent telemetry) until ≥30 days of stable operation accumulate. After the earned threshold is met, the provisional flag drops and the identity is fully canonical. If the lease plane is ever rebuilt on day 1 with new substrate (e.g., migration to a new Mac), the provisional clock restarts. This makes the ontology check honest rather than papered-over.

**Anti-recursion guard:** the lease plane MUST NOT acquire leases for its own outbox writes (would be a self-deadlock at startup). This is enforced by code: the lease-plane-internal Postgres role does not have `INSERT` privilege on `surface_leases` for `surface_kind='lease_plane'`. Belt-and-braces.

### 7.9 Surface_id renames / re-keying (council finding 4.2) — RESOLVED in v0.6

A file rename, dialectic-session ID rotation, or resident relabel changes a surface's canonical ID. Active leases keyed on the old ID become orphans; new acquires on the new ID succeed; the "same surface" is double-leased semantically while the index thinks each entry is unique.

**Resolution:** v0 explicitly does not handle rename-aware relocation. Active leases on a renamed surface must be explicitly released by the holder and re-acquired against the new ID. If the holder is unaware of the rename (e.g., another agent renamed the file out from under it), the orphan lease ages out via TTL on its `original_ttl_s` clock — at most 1h per the §4.4 hard cap. The "double-leased semantically" window is bounded by `original_ttl_s` and not surfaced as a caller-facing error.

**Operator-runbook commitment:** `docs/operations/lease-plane-operator-runbook.md` MUST document the rename-orphan failure mode and the manual-release procedure before Phase A ships. Tracked in §9 checklist.

**Deferred to v1 (not v0):** content-derived `surface_id` (e.g., file inode + ctime, dialectic-session content-hash) to make renames invisible to the lease layer. Trades simplicity for robustness; warrants its own RFC. Until then, the rename gap is a *known and bounded* operational hazard, not an unresolved design question.

### 7.10 Force-release authority (council finding 4.3) — RESOLVED in v0.6

`release_reason='forced'` exists in §4.4.1 vocabulary but the RFC didn't specify *who can issue it*. Force-release is a privilege-escalation surface: any caller who can force-release can free another agent's lease and acquire it themselves.

**Resolution:** force-release requires a separate elevated bearer token. Token name conforms to the existing `~/.config/cirwel/secrets.env` convention (noun-first, `_TOKEN` suffix; cf. `ZENODO_TOKEN`, `CLOUDFLARE_API_TOKEN`, `WORKERS_API_TOKEN`): **`LEASE_FORCE_RELEASE_TOKEN`**. Operator-only. Logged to `lease_plane_events` with `event_type='forced'` and the operator's session_id, projected to `audit.tool_usage` like any other event. Sentinel alarm fires on every force-release event regardless of context — this is rare enough that an alarm-on-every-event is appropriate, not noisy.

**Scope (v0): force-release is local-Mac-only, by design.** The token lives at `~/.config/cirwel/secrets.env` mode 600 on the governance MCP host. Off-host force-release (laptop while traveling, remote `:observer` session) is *not supported in v0*; the operator either SSHes to the Mac or waits for the lease's TTL. v1 may revisit this with the Cloudflare-tunnel + `X-Anima-Admin` pattern (cf. `anima-admin-gate.md`) if real-world incidents justify the token-distribution complexity.

**Anti-pattern (test-gated):** the standard MCP bearer token (`GOVERNANCE_TOKEN`) must NOT permit force-release. **Phase A ships only after** an integration test confirms that a force-release request authenticated with `GOVERNANCE_TOKEN` (and any token *other than* `LEASE_FORCE_RELEASE_TOKEN`) is rejected at the contract layer, not just the application layer. This test is a §9 checklist gate.

**Token-rotation note:** `LEASE_FORCE_RELEASE_TOKEN` follows the same rotation cadence as other operator-scoped tokens at `~/.config/cirwel/secrets.env`. No special rotation infrastructure required for v0; rotation is manual operator action followed by `launchctl kickstart` of the lease-plane LaunchAgent to reload the secrets.env.

### 7.11 Surface-kind deprecation / migration — RESOLVED in v0.8

**Question:** how does the lease plane retire a `surface_kind` (e.g., `td:/` deprecated in v3 because the TouchDesigner integration was abandoned)? What about migrating semantics within a kind (e.g., `resident:/` v0 = process PID, v1 = systemd unit name)?

Council pass v0.8 (parallel: dialectic-knowledge-architect, feature-dev:code-reviewer, live-verifier) found three-voice convergence on `'forced_deprecation'` violating the deployed `release_reason` CHECK; two-voice convergence on Phase 2/3 race window leaving a Layer-1 enforcement gap, on the Sentinel alarm-storm collision with §7.10, and on the missing persistence substrate; plus four single-voice findings on operator-confirmation, sweep idempotency, 30-day window justification, and primitive-scheme evolution foreclosure.

**Resolution: 4-phase operator-driven procedure with `deprecated_schemes` table, CHECK-migration-before-sweep ordering, batch-suppressed Sentinel alarms, primitive-scheme strictly-stronger carve-out.**

#### 7.11.1 Persistence substrate — `deprecated_schemes` table

The "deprecated" flag MUST be a first-class schema object, not application config. Migration 027:

```sql
CREATE TABLE lease_plane.deprecated_schemes (
  surface_kind        text PRIMARY KEY REFERENCES lease_plane.surface_kind_catalog(surface_kind),
  deprecation_id      uuid NOT NULL DEFAULT gen_random_uuid(),
  marked_deprecated_at timestamptz NOT NULL DEFAULT now(),
  marked_by_session_id text NOT NULL,
  drain_window_days   int NOT NULL DEFAULT 30 CHECK (drain_window_days > 0 AND drain_window_days <= 90),
  sweep_started_at    timestamptz,
  sweep_completed_at  timestamptz,
  check_migrated_at   timestamptz
);
```

`surface_kind_catalog` is the canonical scheme registry (also added in migration 027); foreign-key-referenced so deprecation can only target a registered kind. `deprecation_id` is the audit-correlation key linking `marked` → `swept` → `migrated` events. Resumability invariant: a sweep is resumable if `sweep_started_at IS NOT NULL AND sweep_completed_at IS NULL`; idempotent re-run reaches fixpoint via the predicate in §7.11.4.

The acquire path consults `deprecated_schemes` at the Elixir router layer (and the Pydantic field_validator on the Python side, fed via the lease-plane health endpoint's deprecated-kind list). T+0 acquire-block is a query against this table, not application config.

#### 7.11.2 4-phase operator-driven procedure

`# OPERATOR_NOTE 1`: Phase ordering reverses the v0.7 tentative — CHECK migration lands BEFORE sweep, not after, to preserve §7.2.2 three-layer enforcement throughout the drain window. Each phase is operator-typed (not Oban-scheduled); operator presence at Phase 0 mark and Phase 2 sweep is required, matching §7.10's operator-only force-release semantic.

| Phase | Time | Operator action | Effect |
|-------|------|-----------------|--------|
| 0 | T+0 | `lease-plane deprecate <kind>` CLI | INSERT row into `deprecated_schemes`. From this moment, acquire on the kind returns `permission_denied` reason `surface_kind_deprecated` (Elixir router gate + Pydantic field_validator). Existing leases continue to renew/release normally. |
| 1 | T+1 day | (automatic verification) | `unitares_doctor.py` runs nightly, confirms no Elixir source mentions the deprecated scheme. If lint fails, operator alerted; deprecation pauses. |
| 2 | T+`drain_window_days` (default T+30) | `lease-plane deprecation-sweep <kind>` CLI | Operator-issued; requires `LEASE_FORCE_RELEASE_TOKEN` (per §7.10). Sweeps surviving leases (idempotent — see §7.11.4). Records `sweep_started_at` then `sweep_completed_at` on the `deprecated_schemes` row. |
| 3 | T+`drain_window_days` + 0 (same maintenance window) | `lease-plane deprecation-finalize <kind>` CLI | Migration extends `surface_id_grammar` CHECK to remove the scheme. Records `check_migrated_at`. After this point, INSERTs of the deprecated scheme fail at the storage layer. |

Phase 2 and Phase 3 land in the **same operator session** to close the v0.7 1-day Layer-1 enforcement gap (closes dialectic BLOCK-E + code-reviewer BLOCK-3). The `deprecated_schemes` Phase 0 entry continues to gate the application layer regardless, but having both layers up simultaneously is the v0.8 commitment.

#### 7.11.3 Audit event vocabulary

`# OPERATOR_NOTE 2`: Council DRIFT-1 (live-verified) — `'forced_deprecation'` is not in the deployed `release_reason` CHECK. Two corrective options were considered:

- (i) Add `'forced_deprecation'` to migration 026/027 CHECK + `models.py ReleaseReason` TypeAlias + `extract_release_params` in `http_router.ex`. Schema churn but explicit semantics.
- (ii) **Adopted:** Use existing `release_reason='forced'` (already in CHECK) with semantic distinction in `lease_plane_events.event_type`. Phase 0 marks emit `event_type='lease.deprecation_marked'`; Phase 2 sweep events emit `event_type='lease.deprecation_swept'` with `release_reason='forced'`; Phase 3 emits `event_type='lease.deprecation_migrated'`. No `release_reason` schema change required. Audit consumers discriminate on `event_type`, not `release_reason`.

Adopted option (ii) on the basis that (a) `release_reason` already has natural-language overflow into `event_type`, (b) preserves §7.10 Sentinel alarm-on-`release_reason='forced'` semantic without re-wiring (deprecation events ARE forced events; they just carry a discriminator), and (c) avoids a 4-site schema change for a vocabulary expansion. Cost: downstream consumers who want to filter "non-deprecation forced releases" must filter on `event_type NOT LIKE 'lease.deprecation_%'` rather than `release_reason != 'forced_deprecation'`.

`tool_name` projection into `audit.tool_usage`: `'lease.deprecation_marked'`, `'lease.deprecation_swept'`, `'lease.deprecation_migrated'` respectively. Dashboard/KG consumers see deprecation as a first-class event class via `tool_name` discriminator.

#### 7.11.4 Sweep predicate (idempotent, no timestamp filter)

The Phase 2 sweep query is canonical — implementations MUST use exactly this predicate:

```sql
SELECT lease_id FROM lease_plane.surface_leases
WHERE released_at IS NULL AND surface_kind = $1
ORDER BY acquired_at
FOR UPDATE SKIP LOCKED;
```

No timestamp filter (no `acquired_at < $deprecation_start`). Re-running on partial failure reaches fixpoint because already-released leases are excluded by `released_at IS NULL`. `FOR UPDATE SKIP LOCKED` lets multiple sweep workers (if ever needed) parallelize without conflict. `ORDER BY acquired_at` provides deterministic sweep order for audit reconstruction.

The Oban implementation wraps this in a job that records `deprecation_id` on each emitted event so the entire sweep is reconstructable from `lease_plane_events` after the fact.

#### 7.11.5 Sentinel batch suppression

§7.10's "alarm-on-every-force-release" rule was justified for *rare* operator-typed force-release. A deprecation sweep over a high-cardinality kind could fire hundreds of alarms in minutes, training operators to mute the channel. v0.8 amendment to §7.10:

> Sentinel alarm-on-every-event applies to `event_type='forced'`. Bulk deprecation sweeps emit `event_type='lease.deprecation_swept'` and are excluded from per-event alarming. Instead, Sentinel emits **one** alarm per `deprecation_id` summarizing `(kind, count_swept, started_at, completed_at)` after `sweep_completed_at` is set.

This preserves the §7.10 design intent (every individual force-release is auditable and visible) while keeping the deprecation case from drowning the channel. The audit trail is fully preserved in `lease_plane_events` — the suppression is alarm-only.

#### 7.11.6 Within-kind semantics evolution — strictly-stronger carve-out for primitives

v0.7 tentative said within-kind semantics migration is *forbidden*. v0.8 relaxes for primitive schemes:

- **Composite/owned schemes** (`dialectic:/`, `resident:/`, `capture:/`, `td:/`): semantics-migration-within-kind is forbidden; introduce a new kind (`resident_v2:/`) and dual-run during a 30-day drain.
- **Primitive schemes** (`file://`): semantics evolution is allowed iff the new canonicalization rule is *strictly stronger* than the old (i.e., the new canonical form is a subset-of relation with the old form — every old canonical form maps deterministically to a new canonical form, and no two distinct old forms collide at the new form). Such migrations announce via the same 30-day drain (Phase 0 marks the *old* canonicalization deprecated; Phase 2 sweeps leases keyed on the old form; Phase 3 deploys the new canonicalization rule).

The strictly-stronger condition is the safety invariant: it forecloses split-brain where an old-canonical and new-canonical key for the same physical surface coexist as two distinct leases. For `file://`, examples of strictly-stronger evolution include adding Unicode NFC normalization (NFC is canonical, NFD inputs map deterministically to NFC) or adding additional symlink-resolution depth. Examples that are NOT strictly stronger (and therefore require new-kind migration): inode-addressing (different identity model entirely) — handled per §7.12.

#### 7.11.7 Adversarial-input — Phase 0 race window

Council code-reviewer BLOCK-2 second-issue: a holder racing the Phase 0 mark transaction grabs a fresh 1h-TTL lease just before T+0. v0.8 mitigation: Phase 0 INSERT into `deprecated_schemes` is wrapped in a serializable transaction that ALSO sets a session-level advisory lock blocking new acquires on the kind for the transaction duration. The race window is reduced from "between operator command and Elixir router cache refresh" to "single Postgres transaction" (~ms).

Belt-and-braces for very-long-TTL leases: the Phase 2 sweep, by §7.11.4 predicate, captures *all* unreleased leases regardless of when they were acquired. So a racer's lease is swept at T+30 like any other.

#### 7.11.8 unitares_doctor lint polarity

During T+0..T+30 (drain window), the deprecated scheme IS in the live grammar CHECK but SHOULD NOT be in active Elixir source (per §7.2.9). The `unitares_doctor.py` lint rule MUST be polarity-aware:

- Schemes in `deprecated_schemes` table: lint REQUIRES that no Elixir source mentions them (warns operator if they do).
- Schemes in grammar CHECK but NOT in `deprecated_schemes`: standard rule (Elixir source MAY reference; doctor doesn't lint).

After Phase 3 (CHECK migration), the deprecated scheme falls out of the CHECK; standard polarity resumes (Elixir source mentioning the now-removed scheme would fail compile or lint).

### 7.12 Surface_id canonicalization / content-addressing forward-compat — RESOLVED in v0.8 (v1 forward-compat remains Open)

Council pass v0.8 found three-voice ground-truth findings that the v0.7 tentative was wrong about its Python stdlib API (`pathconf(_PC_CASE_SENSITIVE)` raises `ValueError` on macOS — REFUTED) and silently broken on `/var → /private/var` symlink resolution (live evidence). Plus two-voice convergence on the missing cross-platform/server-vs-target-filesystem authority commitment, and a code-reviewer-flagged `?`-ban decision deferral that blocked the §9 Phase A test gate.

**Resolution: server-side canonicalization authority. Tmpfile probe (not pathconf). Double-realpath for /var. Symlink behavior surfaced as contract. v1 forward-compat downgraded from Tentative-option-(a) to Open. v0 explicitly does NOT add `?`-banning CHECK.**

#### 7.12.0 Vocabulary disambiguation

"Canonicalize" (verb) and "canonical form" (noun) in this section refer to the per-scheme string-normalization procedure below. This is **distinct** from §7.2.1's "canonical scheme list" (the vocabulary of allowed scheme prefixes). The two share the word "canonical" but operate at different levels: §7.2.1 enumerates allowed schemes; §7.12 normalizes the path within a scheme.

#### 7.12.1 v0 canonicalization rule (committed via §7.2 cross-reference)

**Authority: server-side.** The lease plane re-canonicalizes on receipt against its own filesystem semantics. Caller-side canonicalization (via the helper) is a perf optimization, not load-bearing. This commits to (i) per the council BLOCK-G options.

Multi-host implication: if a caller on Linux (case-sensitive FS) sends `file:///Users/cirwel/Foo.py` and the Mac-hosted server canonicalizes against APFS (case-insensitive), the server-side lowercase produces `file:///users/cirwel/foo.py`. Both Linux and Mac callers see the same canonical form. The cost: on a future v1 with multi-server deployment, server-side authority requires all servers to share the same canonicalization rules; this is acceptable for v0 (single Mac BEAM node per §2 invariant).

For `file://` surfaces, the server-side canonicalization steps (in order):

1. **Strip `file://` prefix** to get the raw path.
2. **Resolve symlinks twice** (closes live-verifier DRIFT-2): `os.path.realpath(os.path.realpath(path))`. The double-realpath is required on macOS because `os.path.realpath` resolves `/var` → `/private/var` (system symlink) but does NOT idempotently re-resolve. Running realpath twice catches `/var/folders/.../tmpfile`-style paths that agents (Watcher, ship.sh, capture) use heavily and would otherwise split-brain.
3. **Eliminate `.`, `..`, double-slashes** (handled implicitly by realpath on existing paths; if path doesn't exist, fall through to `os.path.normpath`).
4. **Lowercase on case-insensitive filesystems.** Detection: tmpfile probe at startup (closes live-verifier DRIFT-3 — `pathconf(_PC_CASE_SENSITIVE)` is REFUTED on macOS Python). Implementation:
   ```python
   def _detect_case_insensitive() -> bool:
       with tempfile.TemporaryDirectory() as d:
           upper = os.path.join(d, "PROBE")
           lower = os.path.join(d, "probe")
           open(upper, 'w').close()
           return os.path.exists(lower)
   ```
   Cached per-startup. The lease-plane server runs this at boot; the cached answer applies to all incoming `file://` canonicalization.
5. **Strip trailing `/`** unless the path is exactly `/`.
6. **Re-prefix with `file://`**.

For non-`file://` schemes, the v0 per-scheme rules are:

- **`dialectic:/`** — opaque hash. No normalization. Path portion is the dialectic-session UUID; case-sensitive (UUIDs are hex, lowercase-only by convention; reject mixed case at the field_validator).
- **`resident:/`** — opaque resident-name. Case-sensitive. Reject whitespace, `?`, `#`, `&`. Trailing `/` stripped.
- **`capture:/`** — composite member list of form `capture:/<id1>,<id2>,...,<idN>`. Members MUST be sorted lexically before canonicalization (closes dialectic missing-from-§7.12 finding: `capture:/A,B,C` and `capture:/B,A,C` would otherwise split-brain on the same calibration window). Helper sorts.
- **`td:/`** — reserved; no canonicalization rule (not implemented v0).

#### 7.12.2 Helper error semantics

The `src/lease_plane/canonicalize.py` helper is the single point of truth for split-brain prevention. Error semantics MUST be unambiguous:

- **Path doesn't exist** (no symlink target, no file): `os.path.realpath` returns the un-resolved input on macOS/Linux. Helper does not raise; canonicalization proceeds on the un-resolved form. Caller's responsibility to know whether the surface is supposed to exist; lease plane does not validate file existence.
- **Symlink loop**: `os.path.realpath` raises `OSError` (ELOOP). Helper propagates as `CanonicalizeError` with `reason="symlink_loop"`. Caller MUST catch and either retry (after fixing the loop) or fall through to `service_unavailable`.
- **NUL byte in path**: helper rejects at field_validator level with `ValidationError`; the underlying `os.path.realpath` would raise `ValueError` if reached. Caller-side rejection is preferred — fail at the model boundary, not deep in the canonicalize call.
- **Path too long** (`PATH_MAX` exceeded): helper raises `CanonicalizeError(reason="path_too_long")`. Pre-Phase-A gate: ensure `surface_id` Pydantic field has `max_length` consistent with caller-side path-length expectations.

#### 7.12.3 Symlink behavioral commitment (closes code-reviewer CONCERN-3)

`os.path.realpath` resolves symlinks to physical paths. **Behavioral commitment:** two callers using different symlink-paths to the same physical file will produce the same canonical `surface_id` IFF both use the helper. A caller bypassing the helper and passing a symlink path directly creates a lease whose `surface_id` is the symlink form; a second caller using the helper creates a lease on the physical form. The partial unique index sees them as distinct surfaces.

**Worktree implication:** UNITARES uses `git worktree`s heavily. Worktrees are regular directories (not symlinks), so `realpath` does NOT collapse worktree paths to a canonical "main repo" path. A lease on `file:///.../unitares/src/x.py` and a lease on `file:///.../unitares/.worktrees/foo/src/x.py` are distinct leases by design — different physical files even though they're "the same logical source." This is correct and intended.

#### 7.12.4 v1 forward-compat for content-addressing — Open (downgraded from v0.7 Tentative)

v0.7 tentatively chose option (a) (new scheme `file-inode://`). v0.8 council CONCERN-H surfaced that option (a) commits §7.11 to fire on `file://` someday — running a 30-day drain across every Hermes/Claude/Codex/capture caller in the fleet, requiring fleet-wide caller-library upgrade as a precondition. This was not weighed in v0.7's choice.

**v0.8 reframe — Open:** v1 RFC must explicitly weigh the asymmetric costs. Both options remain viable:

- **Option (a) new scheme `file-inode://`.** Cleaner separation. Forces §7.11 drain on `file://`. Acceptable iff operator accepts fleet-wide caller migration as v1 precondition.
- **Option (b) modifier on existing scheme `file:///x.py?canon=inode`.** Keeps `file://` permanent. Costs in-scheme normalization complexity (the partial unique index must be aware of the modifier; `?canon=inode` and the unmodified form are semantically the same surface but textually distinct).

**v0 invariant preserved (closes code-reviewer CONCERN-4):** the open subquestion of "do we add `CHECK (surface_id !~ '\\?')` to migration 026 to force option (a) by construction" is **resolved as NO**. We explicitly do NOT add the `?`-banning CHECK. Doing so would foreclose option (b) and lock in option (a)'s fleet-upgrade cost without v1 having weighed it. v0 stores `?`-bearing `surface_id` values verbatim (live-verifier Finding 13 confirmed); v1 RFC chooses how to interpret them.

`# OPERATOR_NOTE 3`: This means v0 callers MAY (accidentally or intentionally) write `?`-bearing `surface_id` values today, and the lease plane will accept them. The §7.12.1 helper SHOULD reject them via field_validator with `ValidationError("query string in surface_id reserved for v1; use plain canonical form for v0")` to keep v0 traffic clean. This is caller-side rejection only; the storage layer accepts.

#### 7.12.5 Pydantic field_validator commitment (closes code-reviewer CONCERN-4)

`AcquireRequest.surface_id` MUST gain a `field_validator` before Phase A ships. The validator:

1. Matches against the canonical scheme list regex (§7.2.1).
2. Calls the canonicalize helper on `file://` paths (so the model boundary enforces canonicalization).
3. Rejects `?`-bearing values per §7.12.4.
4. Rejects NUL bytes per §7.12.2.
5. Returns the canonical form (Pydantic auto-canonicalizes; this trades visibility-of-drift for caller convenience). Operator note: this hides bugs where callers pass non-canonical and don't realize. v0.8 picks auto-canonicalize for UX; if drift becomes a debug problem, switch to validate-only-and-reject in v1.

## 8. Concerns / counter-arguments / minority views

### 8.1 "BEAM has nil too. Null bugs aren't OTP-shaped."

Correct. Pattern matching makes null fail more loudly (function clause error -> process crash -> supervised restart) but doesn't reduce the rate. The typed-absence protocol (§4.5) is the part that addresses null-class bugs, and **it's adoptable in Python today** without any Elixir migration. Adopt it now; don't gate it on the lease-plane spike.

### 8.2 "This is migration scope creep dressed as architectural completeness."

The bridge-supervisor list (Hermes / Discord / TD / Lumen / Claude / Codex / Capture / Telemetry) that GPT-5.5 sketched is explicitly out of v0 scope. v0 is one Elixir application with one HTTP surface and one Postgres-backed lease abstraction. Bridge supervisors are step N, not step 1.

### 8.3 "You're paying the polyglot tax for what?"

Concede this is a real cost. The tax comes due in: deploy story, log/observability story, secret distribution, Python<->Elixir version skew, and operator on-call cognitive load. The bet is that the lease plane eliminates more concurrency-class bugs than the polyglot boundary introduces. Phase A telemetry is the empirical test: count *new* bugs introduced by the boundary vs. *old* bugs prevented. Promotion to Phase B requires that ratio to be favorable.

### 8.4 "Why not just use Postgres advisory locks / row locks for the same thing?"

Two reasons:
- Local holder semantics: BEAM `:DOWN` notification is structurally cleaner than "did the holding connection drop, did the row TTL expire, did pg_try_advisory_lock fail." OTP is the fit-for-purpose substrate for "process death = claim death."
- Handoff and intent metadata: advisory locks are integers with no semantics; this proposal needs typed surface IDs, holder UUIDs, intent strings, and audit joins. That's a domain-data layer.

A pure Postgres-only design would work for cross-host (heartbeat-TTL leases) but loses the local-holder `:DOWN` win. The dual-mode design (§4.1) takes the best of both.

### 8.5 "Why not just fix it in Python?"

The recurring concurrency commits (PR #218 ExecutorPool, #228, #230, dialectic deadlock guards #50, etc.) are the empirical answer. Each Python fix is good and ships, and yet new seams keep appearing. The hypothesis is that the Python async ecosystem is the wrong substrate for a coordination problem of this shape. Phase A is the test of that hypothesis on one concrete subsystem.

A Python reference implementation of every endpoint in §5 (`asyncio.Lock` per-surface + a Postgres `surface_leases` table with the same schema) **is sketched before the Elixir code starts** — not as retreat insurance, but as the *contract anchor*. The Elixir version is the substrate-validated implementation of the same contract. Two benefits: (a) the Postgres schema and typed-absence shapes are language-agnostic and proven on both sides, (b) any caller that needs to talk to leases without crossing the polyglot boundary in a pinch has a path. This is strategic optionality, not hedging.

**One caveat (council NIT 8):** Python has no equivalent of BEAM's `Process.monitor/1` + `:DOWN` notification. The shelf-Python implementation covers the `holder_kind = 'remote_heartbeat'` path fully (heartbeat-TTL semantics translate directly), but the `holder_kind = 'local_beam'` path degrades to "TTL-only" — local Python holders behave like remote holders to the lease plane: they must heartbeat, they expire on TTL, there is no process-death signal. For most callers this is fine (Hermes/Claude/Codex workers heartbeat anyway). For residents that genuinely benefit from `:DOWN` (lifecycle coordination during planned restart), only the Elixir version delivers the cleaner semantics. The shelf-Python is contract-complete, not feature-complete on `:DOWN`.

## 9. Pre-implementation checklist

Required before any `.ex` file is written:

- [x] Council pass: dialectic-knowledge-architect, feature-dev:code-reviewer, live-verifier (parallel) — v0.2/v0.3/v0.5/v0.7/v0.8
- [x] §7 open questions all answered (RFC tentative -> RFC committed) — §7.1/7.4/7.6/7.8 resolved v0.2; §7.7 resolved v0.2 with operator-action carve-out; §7.9/7.10 resolved v0.6; §7.2/7.3 resolved v0.7; §7.11/7.12 resolved v0.8; **§7.5 Pi path resolved v0.9 (measured 2026-05-03; Mac path remains provisional pending Mac-resident measurement)** (v0.8 §7.12.4 leaves v1 forward-compat option (a) vs (b) Open, by design)
- [x] Shelf-Python sketch checked in alongside the Elixir spec — same schema, same API, same return shapes (v0.4)
- [x] Operational runbook draft: `docs/operations/lease-plane-operator-runbook.md`. **v0.6 commitment:** runbook MUST cover (a) §7.9 rename-orphan manual-release procedure ✓, and (b) §7.10 `LEASE_FORCE_RELEASE_TOKEN` provisioning + rotation steps ✓. **v0.7 addition:** (c) §7.11 scheme-deprecation 30-day drain procedure ✓ (extensive coverage including SIGKILL recovery + idempotent-rerun guidance). Concrete bash commands and SQL audit queries shipped; "Common operations" section retains TBD entries for post-Phase-A operational lore.
- [x] Sentinel monitoring spec for `/v1/lease/status?surface_id=__healthcheck__` — committed in `docs/operations/lease-plane-operator-runbook.md` "Health check" section. Probe cadence 30s, 5-min sliding-window alarm thresholds, four typed alarm rules (`lease_plane.unreachable`, `lease_plane.auth_drift`, `lease_plane.db_degraded`, `lease_plane.slow`), explicit non-coverage list (reaper liveness + audit-outbox + per-kind acquire rate are separate signals).
- [x] Decision: which exact surface_kind goes first into advisory — **`dialectic:/`**. Rationale: (a) lowest blast-radius surface (dialectic sessions are short-lived, single-writer by nature, and the existing dialectic flow can fall through to advisory-skip on lease unavailability without losing work); (b) the dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier council pattern naturally exercises lease handoff and held_by_other paths during normal use, surfacing real conflict telemetry quickly; (c) operator (Kenny) interacts with dialectic surfaces directly and can recognize anomalies without instrumentation overhead. Contrast: `file://` would be higher-risk (every code edit hits it; one bug locks the workspace) and `capture://` is broader but lower-frequency. Promotion to enforcement gated on §6.1 conflict-log evidence (still open as a separate operator decision — see remaining unchecked rows).
- [ ] Decision on §6.1 promotion-gate criteria — what specifically counts as "the conflict log says 'we would have prevented a real bug here'". **Deferred until conflict-log data exists.** Phase A ships with `dialectic:/` in advisory; promotion-to-enforcement is the next gate, and locking a threshold on zero advisory-mode evidence would be arbitrary. Reopen when the conflict log has accumulated entries against `dialectic:/` traffic.

#### Phase A test gates (v0.7 — bundles council BLOCKs)

Each row below is a Phase A blocker. All tests live under `tests/test_lease_plane_*.py` (Python) and `elixir/lease_plane/test/` (Elixir).

- [x] **Migration 026 ships and is verified** — generated `surface_kind` column (§7.2.3) + `surface_id_grammar` CHECK constraint (§7.2.2) live in the `governance` DB. Verified via `\d lease_plane.surface_leases` showing both.
- [x] **§7.2 — invalid scheme rejected at storage layer** — INSERT with `surface_id='not_a_scheme:foo'` raises CHECK violation. Test name: `test_invalid_uri_scheme_rejected_at_storage` (`tests/test_lease_plane_section_9_gaps.py`).
- [x] **§7.2 — Pydantic field_validator rejects invalid scheme** — `AcquireRequest(surface_id='potato:foo', ...)` raises ValidationError. Test name: `test_acquire_request_rejects_invalid_scheme` (`tests/test_lease_plane_section_9_gaps.py`).
- [x] **§7.2.3 — surface_kind drift impossible** — INSERT with `surface_id='file:///x.py'` produces `surface_kind='file'` automatically (generated column); caller cannot supply a conflicting value. Test names: `test_surface_kind_derived_from_scheme`, `test_acquire_request_has_no_surface_kind_field` (post-removal). Both in `tests/test_lease_plane_section_9_gaps.py`.
- [x] **§7.12.1 — case-insensitive file:// canonicalization** — `acquire(file:///Users/cirwel/X.py)` and `acquire(file:///Users/cirwel/x.py)` produce identical canonical form on case-insensitive APFS; second acquire returns `held_by_other` (or `idempotent: true` if same holder). Test name: `test_file_canonicalization_case_insensitive_apfs` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.1 — `..`-path canonicalization** — `acquire(file:///x/../y/z.py)` canonicalizes to `file:///y/z.py`. Test name: `test_file_canonicalization_relative_components` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.3.2 — extended `held_by_other` shape** — response includes `surface_id`, `blocking_lease_id`, `retry_after_hint_ms`. Test names: `test_held_by_other_echoes_surface_id`, `test_held_by_other_returns_blocking_lease_id`, `test_held_by_other_includes_retry_hint` (all in `tests/test_lease_plane_held_by_other_v0_8.py`).
- [x] **§7.3.3 — `acquire_with_retry()` honors backoff** — jittered exponential, floor 100ms, ceiling 5s. Test name: `test_acquire_with_retry_jittered_backoff` (`tests/test_lease_plane_retry_and_transport.py`).
- [x] **§7.3.5 — HTTP 409 on `held_by_other`; 200 + ok:false otherwise** — Elixir router behavior. Test names (Elixir-side): `test http_router returns 409 on held_by_other` (`elixir/lease_plane/test/http_router_test.exs:230`), `test http_router returns 200 on permission_denied` (`elixir/lease_plane/test/http_router_test.exs:215`). Canonical 200-trigger is the §4.4 role-holder rejection (line 154); §7.10 forced-release-on-/release rejection (line 670) is a second path. `:not_found` → 404 on /release and /handoff_accept is permitted by §7.3.5 text ("HTTP 4xx other than 409 indicates transport-level failure (auth, malformed request, route not found)") and is not in scope of this row.
- [x] **§7.3.5 — `_urllib_transport` HTTP-error body-parse path** — Test name: `test_urllib_transport_parses_409_body` (`tests/test_lease_plane_retry_and_transport.py`).
- [x] **§7.10 — `GOVERNANCE_TOKEN` cannot force-release** (already gated v0.6; restated): only `LEASE_FORCE_RELEASE_TOKEN` succeeds; rejection at contract layer. Test name: `test_force_release_rejects_governance_token`. (Python contract layer closed in `tests/test_lease_plane_client.py`; Elixir router-side rejection tracked separately as Phase A Elixir gate.)

#### Phase A test gates (v0.8 — bundles §7.11 + §7.12 council BLOCKs)

- [x] **Migration 027 ships and is verified** — `lease_plane.deprecated_schemes` table created; `surface_kind_catalog` registry created. Verified via `\d lease_plane.deprecated_schemes`.
- [x] **§7.11.3 — `release_reason='forced'` reused for deprecation events; vocabulary unchanged** — Phase 2 sweep events emit with existing `'forced'` value, distinguished by `event_type='lease.deprecation_swept'`. Closes live-verifier DRIFT-1. Test name (renamed during §9 gap-fill): `test_deprecation_sweep_requires_force_release_token` (`tests/test_lease_plane_deprecate_cli.py`); the `'forced'` value is structurally enforced by the DB CHECK on `release_reason`.
- [x] **§7.11.2 — Phase 2 + Phase 3 land in same operator session** — Layer-1 enforcement gap closed. Test name: `test_deprecation_sweep_and_check_migration_atomic_session` (`tests/test_lease_plane_deprecate_cli.py`).
- [x] **§7.11.4 — sweep predicate idempotent on partial-failure re-run** — operator interrupts mid-sweep, re-runs, completes without double-emitting events. Test name: `test_deprecation_sweep_idempotent_on_partial_failure`.
- [x] **§7.11.5 — Sentinel batch suppression** — bulk deprecation sweep emits one summary alarm per `deprecation_id`, not N per-lease alarms. Closes Sentinel alarm-storm CONCERN. Test name: `test_sentinel_batch_alarm_for_deprecation_sweep` (`tests/test_sentinel_forced_release_alarm.py`).
- [x] **§7.11.7 — Phase 0 race window** — concurrent acquire racing the Phase 0 mark transaction is blocked by serializable-tx + advisory-lock. Test name: `test_phase_zero_acquire_race_blocked` (`tests/test_sentinel_forced_release_alarm.py`).
- [x] **§7.12.1 — tmpfile probe replaces `pathconf(_PC_CASE_SENSITIVE)`** — startup detection works on macOS Python (live-verifier REFUTED pathconf). Test name: `test_canonicalize_case_detection_uses_tmpfile_probe` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.1 — `/var → /private/var` double-realpath** — `/var/folders/.../tmpfile` and `/private/var/folders/.../tmpfile` produce same canonical form. Closes live-verifier DRIFT-2. Test name: `test_canonicalize_resolves_var_to_private_var_on_macos` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.1 — `capture:/` member ordering** — `capture:/A,B,C` and `capture:/B,A,C` canonicalize to same `surface_id`. Test name: `test_capture_canonicalizes_member_ordering` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.2 — helper error semantics** — symlink loop raises `CanonicalizeError(reason="symlink_loop")`; NUL byte rejected at field_validator; nonexistent path falls through cleanly. Test name: `test_canonicalize_error_semantics` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.4 — `?`-bearing `surface_id` rejected by Pydantic field_validator** — caller-side rejection; storage layer remains permissive (v1 option-(b) keep-open). Test name: `test_acquire_request_rejects_query_string_in_surface_id` (`tests/test_lease_plane_canonicalize.py`).
- [x] **§7.12.5 — `AcquireRequest.surface_id` field_validator wired** — closes the v0.7 implementation gap (currently only `min_length=1`). Test name: `test_acquire_request_surface_id_field_validator_wired` (`tests/test_lease_plane_canonicalize.py`).

#### Pre-existing v0.7 implementation drift (surfaced by v0.8 council; needs code, not RFC)

- [x] **`models.py AcquireHeldByOther`** — extended with `surface_id`, `blocking_lease_id`, `retry_after_hint_ms` per §7.3.2. Test name: `test_held_by_other_includes_v0_7_extended_fields` (`tests/test_lease_plane_held_by_other_v0_8.py`).
- [x] **`http_router.ex extract_acquire_params`** — acquire bodies containing `surface_kind` are **rejected at the HTTP contract boundary** (HTTP 400 with typed-absence error), because the DB-generated column is the sole source of truth. Strict contract chosen over silent-strip: callers learn immediately that `surface_kind` is no longer part of acquire after migration 026. Test name (Elixir-side): `test http_router rejects surface_kind in acquire body after migration 026` (`elixir/lease_plane/test/http_router_test.exs`).
- [x] **`agents/sentinel/agent.py`** — alarm rule keyed on `event_type='forced'` from `lease_plane_events`; rule lives in `agents/sentinel/forced_release_alarm.py`. Per §7.10 + §7.11.5, the rule fires per-event for ad-hoc force-release, batched for deprecation sweeps. Test name: `test_sentinel_force_release_alarm_wired` (`tests/test_sentinel_forced_release_alarm.py`).

#### Phase B prerequisites (v0.7 — non-blocking for Phase A)

- [ ] **§7.2.8** — payload-shape standardization pass spec authored before any surface_kind reaches Phase B candidate status; commits to writing canonicalized `surface_id` (per §7.12.1) into `audit.tool_usage.payload`, no percent-encoding.
- [ ] **§7.2.9** — `unitares_doctor.py` extended to lint that no Elixir source mentions a scheme not in the live DB CHECK.
- [ ] **§7.11** council pass on the 30-day-drain tentative before any production scheme is deprecated.
- [ ] **§7.12.4** v1 RFC opening: weigh option (a) new scheme `file-inode://` vs option (b) modifier `?canon=inode` explicitly with the asymmetric-cost framing v0.8 surfaces. v0.8 explicitly does NOT add `?`-banning CHECK in migration 026 — both options remain viable for v1.

## 10. Runway tradeoff (operator decision, not technical)

This is a 4-8 week spike. It trades against:

- Fellowship deadline / Anthropic application
- Paper v6.9.x polish and v7 corpus-maturity work
- Public plugin / discord-bridge community work
- KG/UX maintenance and dogfood-driven fixes

The technical case is strong (three independent reviewers converged). The strategic case is the operator's call. If shelved, file this RFC as captured-decision so the next session doesn't re-litigate the substrate question from scratch.

## 11. Versions / changelog

- **v0.9 (2026-05-03):** §7.5 (heartbeat cadence + TTL math) promoted from `PARTIALLY RESOLVED` (operator-action carve-out) → `RESOLVED` for the Pi `remote_heartbeat` path. Resolved by mining the existing `audit.events WHERE event_type='eisv_sync'` audit trail (Steward Pi→Mac sync, n=8452 since 2026-03-15) instead of building purpose-built instrumentation — the audit log already characterizes the Pi↔Mac heartbeat path the same way the §7.5 carve-out's "instrument and wait 7 days" plan would. Material changes:

  - **Pi `remote_heartbeat original_ttl_s`: 180s → 1000s** (~16.7min). Derived from p99 = 621s on n=1839 healthy-window syncs (2026-04-22 → 2026-04-29 04:00 UTC-6, the last continuous-cadence window before the operator-induced Steward pause on 2026-05-01). p99 × 1.5 = 931s, rounded to 1000s for operator legibility. Old 180s default was ~5.4× too tight relative to measured behavior.
  - **Pi heartbeat cadence: 60s → 333s** (= `original_ttl_s/3`). Comfortably above Steward's natural 300s sync cadence; every Steward sync refreshes well within TTL.
  - **Mac `remote_heartbeat` defaults unchanged** (90s/25s, marked provisional). Mac residents reach the lease plane over loopback rather than via the Steward sync path, so the Pi measurement does not transfer. Mac promotion to a `remote_heartbeat` Phase B surface should mine the relevant Mac resident's natural audit cadence first; the §7.5 methodology (use existing audit traces; ≥7d window; p99 × 1.5 = TTL; heartbeat = TTL/3) is now the standing rule.
  - §9 checklist row for §7.5 status updated; Phase A plan §7.5 Phase-B-prereq row marked DONE.
  - **Methodology lesson recorded:** when an "operator action" RFC carve-out asks for instrumentation + measurement window, check whether the audit log is already the instrument before building anything new.

- **v0.8 (2026-04-30, same session):** §7.11 (deprecation procedure) and §7.12 (canonicalization + v1 content-addressing forward-compat) promoted Tentative → Resolved. Council pass run in parallel (dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier; adversarial framing per `feedback_council-adversarial-prompt.md`). Three-voice convergence on three top issues; multiple two-voice and single-voice findings folded in. Material changes:

  *§7.11 — Resolved (4-phase operator-driven, with persistence substrate):*
  - **`deprecated_schemes` table** added (§7.11.1, migration 027) — first-class schema object, not application config. Includes `deprecation_id` for audit-correlation across mark/sweep/migrate events; `surface_kind_catalog` registry referenced via FK. Closes code-reviewer BLOCK-2 + NIT-1 (the v0.7 open subquestion is load-bearing, not deferrable).
  - **Phase ordering reversed** (§7.11.2): CHECK migration lands BEFORE sweep, both in same operator session. Closes dialectic BLOCK-E + code-reviewer BLOCK-3 — the v0.7 1-day Layer-1 enforcement gap is gone.
  - **`'forced_deprecation'` REJECTED in favor of `release_reason='forced'` + `event_type='lease.deprecation_*'`** (§7.11.3): three-voice convergence (dialectic BLOCK-A + code-reviewer BLOCK-1 + live-verifier DRIFT-1) confirmed `'forced_deprecation'` is not in deployed CHECK. Avoids 4-site schema change; preserves §7.10 Sentinel `release_reason='forced'` alarm wiring.
  - **Idempotent sweep predicate explicit** (§7.11.4): `WHERE released_at IS NULL AND surface_kind = $1`, no timestamp filter, `FOR UPDATE SKIP LOCKED`. Closes code-reviewer BLOCK-4.
  - **Sentinel batch suppression** (§7.11.5): bulk deprecation sweeps emit one summary alarm per `deprecation_id` rather than N per-event alarms. §7.10 alarm-on-every-event semantic preserved for ad-hoc force-release. Closes dialectic BLOCK-D + code-reviewer CONCERN-1.
  - **Within-kind primitive-scheme evolution carve-out** (§7.11.6): `file://` may evolve via "strictly stronger" canonicalization (subset-of relation) without forcing a new-kind migration. `dialectic:/`, `resident:/`, `capture:/`, `td:/` remain forbidden-within-kind. Closes dialectic CONCERN-C — primitive-scheme foreclosure resolved.
  - **Phase 0 race window** mitigated (§7.11.7): serializable transaction + session-level advisory lock during the mark-deprecated INSERT. Belt-and-braces: §7.11.4 sweep predicate captures all unreleased leases regardless of acquire timestamp.
  - **`unitares_doctor.py` lint polarity** (§7.11.8): polarity-aware during T+0..T+30 drain window — deprecated schemes REQUIRE no Elixir source mention; non-deprecated schemes are unconstrained.
  - **Audit signal contract**: `tool_name` in `audit.tool_usage` projects as `'lease.deprecation_marked'`/`'lease.deprecation_swept'`/`'lease.deprecation_migrated'`. Dashboard/KG consumers see deprecation as first-class event class.
  - **30-day default constant**: `drain_window_days` is a column on `deprecated_schemes` (default 30, max 90); per-deprecation override possible. Cross-references §7.6 outbox-prune and §7.2.6 lease-prune as collinear operational windows.

  *§7.12 — Resolved (with v1 forward-compat explicitly Open):*
  - **Server-side canonicalization authority** (§7.12.1, option (i)): lease plane re-canonicalizes on receipt against its own filesystem semantics; caller-side helper is a perf optimization, not load-bearing. Closes dialectic BLOCK-G + code-reviewer CONCERN-6 — cross-platform/multi-host split-brain hazard resolved for v0 (single Mac BEAM node per §2 invariant).
  - **Tmpfile probe REPLACES `pathconf(_PC_CASE_SENSITIVE)`** (§7.12.1 step 4): three-voice ground-truth — live-verifier REFUTED `PC_CASE_SENSITIVE` availability on macOS Python; calling it raises `ValueError`. v0.7 spec was wrong. Helper code sample provided; cached per-startup.
  - **Double-realpath for `/var → /private/var`** (§7.12.1 step 2): closes live-verifier DRIFT-2. `os.path.realpath(os.path.realpath(path))` catches the macOS system-symlink hop that single-realpath misses; matters for `/var/folders/.../tmpfile`-style paths heavily used by Watcher, ship.sh, capture.
  - **Per-scheme canonicalization rules** (§7.12.1 for non-`file://` schemes): explicit per-kind handling. Notably **`capture:/A,B,C` member ordering** — sorted lexically before canonicalization to prevent split-brain on the same calibration window with reordered members. Closes dialectic missing-from-§7.12 finding.
  - **Helper error semantics** (§7.12.2): symlink loop, NUL byte, path-too-long, nonexistent-path all named with explicit error/no-error contracts. The helper is the single point of truth for split-brain prevention; ambiguous errors are themselves a split-brain risk.
  - **Symlink + worktree behavioral commitment** (§7.12.3): explicit contract that `os.path.realpath` resolves symlinks to physical paths; bypassing the helper produces split-brain. Worktree paths are NOT collapsed (worktrees are regular directories, not symlinks) — leases on a file via main-repo path vs `.worktrees/foo/...` path are distinct by design.
  - **v1 forward-compat downgraded from Tentative-(a) to Open** (§7.12.4): closes dialectic CONCERN-H — option (a) commits §7.11 to fire on `file://` (fleet-wide caller migration as v1 precondition). Both options remain viable; v1 RFC must explicitly weigh asymmetric costs. **v0 explicitly does NOT add `?`-banning CHECK in migration 026** — preserves option (b) viability.
  - **Vocabulary disambiguation** (§7.12.0): "canonicalize"/"canonical form" (§7.12 string normalization) distinguished from "canonical scheme list" (§7.2.1 vocabulary). Closes dialectic NIT-I.
  - **Pydantic field_validator wired** (§7.12.5): closes code-reviewer CONCERN-4 — auto-canonicalize at the model boundary (UX over visibility-of-drift; flagged for revisit if drift becomes a debug problem in v1).

  *§9 checklist — three new categories:*
  - 6 §7.11 Phase A test gates (deprecated_schemes migration, sweep predicate, idempotency, batch alarm, race window).
  - 6 §7.12 Phase A test gates (tmpfile probe, /var double-realpath, capture: member ordering, error semantics, ?-rejection, field_validator wired).
  - **3 pre-existing v0.7 implementation drift items surfaced as named §9 gates**: `models.py AcquireHeldByOther` extended fields missing (closes v0.7 commitment §7.3.2 vs reality); `http_router.ex extract_acquire_params` still requires `surface_kind` (will hard-fail against migration 026); `agents/sentinel/agent.py` has no `event_type='forced'` alarm rule (live-verifier Finding 5 SOURCE_ONLY). These are runtime-code work, not RFC-text changes.

  *Council reports archived in session transcript.* Three-voice convergence pattern (3+ voices agree → unconditional fix) handled separately from two-voice (most CONCERNs) and single-voice (additions). Three findings reframed as v0.7 implementation gaps to be tracked-not-litigated. Two operator decisions surfaced inline as `OPERATOR_NOTE` markers (Phase ordering reversal; `release_reason='forced'` reuse).

- **v0.7 (2026-04-30, same session):** §7.2 (Surface ID schema) and §7.3 (Conflict semantics on `held_by_other`) promoted Tentative → Resolved. Council pass run in parallel (dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier; adversarial framing per `feedback_council-adversarial-prompt.md`). Operator-decision pass landed before commit; four operator-choice points resolved per codex direction. Material changes:

  *§7.2 — Resolved with defense-in-depth:*
  - Canonical scheme list authored once in §7.2.1 (5 schemes: `file://`, `dialectic:/`, `resident:/`, `capture:/`, `td:/`); §3.3/§7.2 self-contradiction (dialectic BLOCK-1) resolved by adding `capture:` to §7.2 grammar.
  - Three-layer enforcement (§7.2.2): Postgres CHECK on scheme grammar (migration 026), Pydantic field_validator, Elixir Ecto enum. Closes three-voice consensus on missing storage-layer CHECK (dialectic BLOCK-2 + code-reviewer BLOCK-1 + live-verifier DRIFT-A).
  - **DB-enforced surface_kind via generated column** (§7.2.3, operator decision per codex): `surface_kind text GENERATED ALWAYS AS (split_part(surface_id, ':', 1)) STORED`. Caller-supplied `surface_kind` removed from `AcquireRequest`. Fallback to CHECK-pair documented if generated-column conversion is too disruptive at migration time. Application-only enforcement explicitly REJECTED. Closes code-reviewer CONCERN-2 + live-verifier DRIFT-B.
  - Per-holder cardinality bound: unbounded by design with named soft-threshold alert at 100 concurrent leases (§7.2.5). Threat model explicitly stated: caller-bug class, not external-attacker class; alert is reactive. Closes dialectic CONCERN-4.
  - 30-day pruning policy for `surface_leases.released_at` via Oban (§7.2.6). Closes code-reviewer CONCERN-3.
  - Status/release path normalization commitment (§7.2.7). Closes code-reviewer CONCERN-4.
  - §6.1 criterion-5 percent-encoding gotcha cross-tracked into payload-shape standardization pass (§7.2.8). Closes code-reviewer CONCERN-5.
  - Forward-compat for unknown schemes deferred to §7.11 deprecation procedure (§7.2.9).
  - file:// canonicalization details delegated to **new §7.12** rather than buried in §7.2.

  *§7.3 — Resolved with per-surface-kind override slot:*
  - Global default remains `abort`; per-surface-kind `conflict_default` override slot added for Phase B promotion (§7.3.1). Anticipated targets named per surface_kind. Closes dialectic CONCERN-8.
  - Extended `AcquireHeldByOther` typed-absence shape (§7.3.2): adds `surface_id`, `blocking_lease_id`, `retry_after_hint_ms`. Closes code-reviewer BLOCK-3 + CONCERN-6.
  - Mandatory backoff guidance for caller libraries (§7.3.3): `acquire_with_retry()` convenience method implements jittered exponential (floor 100ms, ceiling 5s, full jitter). Bare `acquire()` remains single-shot. Closes dialectic BLOCK-6 (thundering-herd).
  - Handoff opt-in semantics clarified (§7.3.4): v0 ships endpoints, no holder classes wire acceptance until first resident-class enforcement promotion.
  - HTTP 409 on `held_by_other`; HTTP 200 + `ok:false` on all other typed-absence errors (§7.3.5). Live-verifier confirmed Elixir router already implements this; v0.7 promotes from implementation detail to contract requirement.

  *New §7.11 — Tentative:* Surface-kind deprecation/migration procedure. 30-day drain (mark deprecated → existing leases age out → force-release survivors → migrate CHECK constraint). Semantics-migration-within-a-kind forbidden; introduce a new kind and dual-run instead. Council pass before any production scheme is deprecated.

  *New §7.12 — Tentative:* Surface_id canonicalization and v1 content-addressing forward-compat. v0 canonicalization rule committed via §7.2 cross-reference (case-insensitive APFS lowercase, realpath, trailing-slash strip). v1 forward-compat path: tentative is option (a) new scheme `file-inode://`, with option (b) query-string modifier as alternative. v0 invariant explicitly stated: query-string in `surface_id` is NOT stripped before indexing, foreclosing the v1 split-brain bug class. Council pass before v1 RFC opens.

  *§9 checklist — Phase A test gates bundled* (per codex direction): 11 named test gates covering migration 026 verification, scheme rejection at storage + Pydantic layers, surface_kind generated-column behavior, file canonicalization (case-insensitive + relative components), extended `held_by_other` shape (3 sub-tests for the 3 new fields), `acquire_with_retry()` backoff, HTTP 409 convention, `_urllib_transport` HTTP-error path coverage (closes live-verifier test-coverage gap), and the v0.6 §7.10 force-release test (restated). 4 Phase B prerequisites separated as non-blocking.

  *Council reports archived in session transcript.* Three-voice convergence pattern (BLOCKs found by 2+ voices) handled separately from single-voice findings. No findings rejected; one (Elixir GenServer start-after-commit invariant, code-reviewer CONCERN-1) tracked as Elixir-implementation contract rather than RFC-text amendment, since it lands with the BEAM service code.

- **v0.6 (2026-04-30, same session):** Promoted §7.9 (surface_id renames) and §7.10 (force-release authority) from *Tentative* to *Resolved*, locking in the v0.2/v0.3 text as committed contract. Council pass queued in parallel on the two remaining open questions (§7.2 surface ID schema; §7.3 conflict semantics on `held_by_other`). Material changes:

  *§7.9 — Resolved:*
  - "v0 does not handle this" promoted from tentative to *committed scope*. The orphan window is now characterized as bounded (≤ `original_ttl_s`, hard-capped at 1h per §4.4); it is a *known and bounded* operational hazard, not an unresolved design question.
  - Operator-runbook commitment surfaced explicitly: rename-orphan failure mode + manual-release procedure must land before Phase A. Tracked in §9 checklist.
  - Content-derived `surface_id` (inode + ctime, dialectic-session content-hash) deferred to v1 RFC; called out by name so future readers don't re-litigate.

  *§7.10 — Resolved:*
  - `LEASE_FORCE_RELEASE_TOKEN` at `~/.config/cirwel/secrets.env` (mode 600, local-Mac-only, Sentinel-alarm-on-every-event) is the committed mechanism.
  - Anti-pattern test promoted from "Confirmed via integration test before Phase A ships" prose to a §9 checklist gate: `GOVERNANCE_TOKEN` cannot force-release; only `LEASE_FORCE_RELEASE_TOKEN` succeeds; rejection at the contract layer, not application layer. Phase A blocks on this test passing.
  - Token-rotation note added: manual operator action + `launchctl kickstart` of the lease-plane LaunchAgent. No special rotation infrastructure for v0.

  *§9 checklist:*
  - Council-pass and shelf-Python items marked complete with backreferences.
  - §7-open-questions item annotated with per-§ resolution status; §7.2/§7.3 explicitly named as the remaining queue.
  - New checklist row added for the §7.10 integration-test gate.

  *Council queue (not yet executed):*
  - §7.2 (Surface ID schema — typed scheme `file:///`, `dialectic:/`, `td:/`, `resident:/` vs opaque; per-holder cardinality)
  - §7.3 (Conflict semantics on `held_by_other` — abort default vs wait-with-timeout vs auto-handoff)
  - Council to be dispatched in parallel: `dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`, adversarial framing per `feedback_council-adversarial-prompt.md`.

  *No schema, contract, or implementation changes.* This version is purely status promotion and council scheduling; no new technical claims beyond what was already in v0.2/v0.3.

- **v0.5 (2026-04-30, same session):** Implementation council on the captured `src/lease_plane/` skeleton (parallel: dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier; adversarial framing per `feedback_council-adversarial-prompt.md`). One critical bug + four contract-staleness items found and addressed in this revision. Material changes:

  *Schema (§4.4) — closes live-verifier-confirmed gap:*
  - Migration 024's `enforce_immutable_lease_fields` trigger guarded only `holder_kind`, `holder_class`, `original_ttl_s`. live-verifier ran direct UPDATEs against the live `governance` DB and confirmed `surface_id`, `surface_kind`, `holder_agent_uuid`, `acquired_at` were silently mutable on a non-released lease row. The active-unique partial index on `(surface_id) WHERE released_at IS NULL` does not substitute for trigger protection on UPDATE. Migration 025 (`db/postgres/migrations/025_lease_plane_invariants.sql`) replaces the trigger function via `CREATE OR REPLACE` with all eight immutability guards. RFC §4.4 trigger sketch updated to match.
  - Added `earned_status` column on `surface_leases` (NOT NULL DEFAULT 'provisional', CHECK in {provisional, earned}) and on `lease_plane_events` (nullable for non-lease-creation event types). RFC §7.8 had committed to flagging v0 leases provisional but the flag was invisible at the contract boundary; this surfaces it as a first-class field.
  - The RFC's prior trigger function name `enforce_immutable_holder_kind` (and trigger name `surface_leases_immutable_holder`) were renamed to `enforce_immutable_lease_fields` / `surface_leases_immutable_fields` to match what migration 024 actually shipped (drift surfaced by this council pass).

  *Contract (§5):*
  - `/v1/lease/renew` body row corrected to `{ lease_id }` only — §4.4.2 already spec'd "no ttl_s parameter" but the §5 endpoint table still showed the stale shape. Elixir builders read §5.
  - `/v1/lease/handoff/offer` `ttl_s` annotated inline: it is the new lease's `original_ttl_s` after accept (handoff is release-and-reacquire, not in-place update). The offer-pending timeout is server-internal (Oban job).

  *Python contract anchor:*
  - `LeaseRecord` gains `earned_status: Literal["provisional","earned"] = "provisional"` field; `EarnedStatus` exported from `src/lease_plane/`.
  - `HandoffOfferRequest` docstring documents the release-and-reacquire model and the meaning of `ttl_s`.
  - `_parse_simple` fall-through (unknown error string) now preserves the raw error in `SimpleError.reason` instead of silently mapping to bare `service_unavailable`. Protocol-skew bugs become inspectable.
  - 5 new tests in `tests/test_lease_plane_client.py`: handoff offer ok, handoff accept not_found, unknown-error preservation, earned_status default, earned_status="earned" override.



- **v0 (2026-04-30):** Initial draft. Pre-council. Author: claude_code session `agent-68437d77-65c`. Synthesis of three-voice convergence (claude_code, codex, gpt-5.5) on 2026-04-30. Replaces the implicit RFC produced by dialectic session `95c9ddfd6bb09308` by formalizing scope, invariant, and rollout pattern.

- **v0.1 (2026-04-30, same session):** Folded in fourth-voice (grok) primer specifics. Added §3.5 substrate side-benefits (hot reload addressing `feedback_running-process-vs-master-commit.md`, native introspection via `:observer.start()`). Restructured §3.2 frontmatter `out_of_scope_explicit` into hard-line vs deferred-to-RFC categories. **Reversed Oban refusal** — Oban is now the substrate for reaper sweeps + handoff timeouts + audit-outbox drains (operator feedback caught reflexive scope-discipline-as-bias; using a GenServer sweep instead of Oban was reinventing a wheel). Added PromEx alongside Postgres outbox in §7.6 as sibling answer (different consumers, different durability requirements). Reframed §8.5 shelf-Python as contract anchor rather than retreat insurance. Linked operator runbook stub at `docs/operations/lease-plane-operator-runbook.md`.

- **v0.2 (2026-04-30, same session):** First council pass complete (parallel: dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier). 8 BLOCKs and 8 CONCERNs raised; all addressed in this revision. Documentation drifts surfaced by live-verifier all corrected. Material changes:

  *Schema (§4.4):*
  - Added `holder_class` (`process_instance`/`substrate_earned`/`role`) per identity.md v2 alignment (council BLOCK 1.1)
  - Added `heartbeat_required` boolean with CHECK constraint coupling it to `holder_kind` (council CONCERN 6)
  - Added §4.4.1 `release_reason` vocabulary table
  - Added §4.4.2 unified heartbeat/renew semantics — both extend `expires_at` atomically (council BLOCK 4)
  - Local_beam default TTL reduced 90s → 30s with in-process timer at TTL/3 (council BLOCK 2)
  - Outbox table `lease_plane.lease_plane_events` co-located in same Postgres DB as `audit.tool_usage`, BEAM role INSERT-only (council BLOCK 1.2)
  - Added unbounded-growth guard with Sentinel alarm thresholds (council CONCERN 5)

  *Contract (§4.5, §5):*
  - Acquire endpoint is idempotent on `(surface_id, holder_agent_uuid)`; returns `idempotent: true` for retry-on-lost-response cases instead of `held_by_other` against self (council BLOCK 3)
  - `holder_kind` immutable per `lease_id`; CHECK constraint (council CONCERN 3.1)

  *Rollout (§6.1):*
  - Promotion gate criteria now include 5 specific testable conditions with SQL spec
  - Type A (observed conflicts) and Type B (unintegrated-caller bypass) failure modes named explicitly
  - Adversarial-bypass cross-check via file-mtime delta (council CONCERN 3.3)

  *§7 open questions:*
  - §7.1, §7.4, §7.5, §7.6 marked RESOLVED with back-references to v0.2 changes
  - Added §7.8 Lease plane's own UNITARES identity — tentative: substrate-earned (Lumen-class) (council BLOCK 4.1)
  - Added §7.9 Surface_id renames/re-keying — tentative: out of v0 scope (council CONCERN 4.2)
  - Added §7.10 Force-release authority — tentative: separate elevated bearer token, Sentinel-alarmed on every event (council CONCERN 4.3)

  *Provenance (§3.4):*
  - Added explicit attribution table: dialectic-output vs RFC-side additions (council CONCERN 2.1)
  - §1 thread-A vs thread-B disclaimer for `operator-flagged-rebuild` two-thread reading (council BLOCK 2.2)

  *Documentation drift (live-verifier):*
  - PR #198 reframed as docs/incident-tracker closure (not code fix); concurrency-commit count adjusted from ~20 to ~17
  - PR #228, #230 characterizations corrected to match actual titles
  - "Mnemos" annotated as agent display claim (record carries UUID 07d0f9c7); KG IDs throughout normalized to include `+00:00` suffix
  - PR titles now quoted verbatim from `gh pr view` output
  - 14 auto-recovered-stuck-agent entries verified (claim was "12+", actual 14)
  - `audit.tool_usage` schema confirmed; lease event projection plan stated explicitly (no schema migration needed)

  *Counter-arguments (§8.5):*
  - Shelf-Python `:DOWN` caveat — Python implementation is contract-complete on remote_heartbeat path, degraded TTL-only on local_beam path

- **v0.3 (2026-04-30, same session):** Ack-pass on v0.2 amendments complete (parallel: dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier). 3 new BLOCKs + 6 new CONCERNs + 3 new DRIFTs surfaced — all introduced *by* the v0.2 amendments themselves, exactly the precedent the ack-pass exists to catch (cf. `onboard-bootstrap-checkin.md` v2.1). All addressed in v0.3:

  *Schema enforcement (§4.4):*
  - `holder_class` CHECK narrowed to `('process_instance','substrate_earned')` only — `'role'` is rejected at the storage layer, not just the application layer (closes role × local_beam admission gap)
  - Added `original_ttl_s int NOT NULL` column with `CHECK (original_ttl_s > 0 AND original_ttl_s <= 3600)` — hard cap at 1h, no indefinite leases possible
  - Added `BEFORE UPDATE` trigger `enforce_immutable_holder_kind` enforcing immutability of `holder_kind`, `holder_class`, `original_ttl_s` at the UPDATE level (closes the row-level-CHECK-bypass gap; prior immutability was documented but unenforced)

  *§4.4.2 renew/heartbeat semantics:*
  - Specified that `expires_at = now() + original_ttl_s` (immutable column), NOT a caller-supplied ttl. `/v1/lease/renew` accepts no `ttl_s` parameter. Closes indefinite-extension attack surface.

  *§4.4.1 release_reason vocabulary:*
  - Added `reaped_local_ttl` for the live-but-stuck local_beam case (process alive, in-process timer stopped firing, supervisor `:DOWN` never fired). Previously this would have been misclassified as `reaped_remote_ttl`.

  *§4.5 idempotent acquire:*
  - Specified that on retry with drifted parameters (`intent`, `ttl_s`, `holder_pid`), original values win and a `drift_warning` field surfaces the discarded keys. Silent discard is now explicit at the contract layer.

  *§3.5 hot reload caveat:*
  - Narrowed the "module swap without lease loss" claim — true for stateless modules; stateful `LeaseHolder` GenServer hot-reload requires a `code_change/3` test pass before deploy or the lease releases spuriously via `:DOWN` cascade.

  *§7.8 substrate-earned identity:*
  - Renamed "Lumen-class" to canonical "substrate-earned" (per `docs/ontology/identity.md` Pattern — Substrate-Earned Identity appendix; "Lumen-class" was an RFC-side informalism not in the ontology)
  - Added explicit caveat: at v0 ship the lease plane has zero accumulated behavioral history, so the substrate-earned pattern is **not yet earned**. UUID is provisional, flagged `earned_status='provisional'` in audit telemetry until ≥30 days of stable operation accumulate. Pattern earns canonically after the threshold; clock restarts on substrate change.

  *§7.10 force-release authority:*
  - Token renamed `OPERATOR_FORCE_RELEASE_TOKEN` → `LEASE_FORCE_RELEASE_TOKEN` (conforms to the existing `~/.config/cirwel/secrets.env` noun-first / `_TOKEN`-suffix pattern)
  - Scope explicitly clarified: force-release is **local-Mac-only by design in v0**. Off-host force-release deferred to v1 if real-world incidents justify the token-distribution complexity.

  *§6.1 promotion gate criterion 5:*
  - Added explicit SQL with `payload->>'surface_id'` jsonb path expression
  - Named **payload-shape standardization** as a prerequisite for criterion-5 evaluability on write-heavy surface_kinds (different existing callers emit different keys; the canonical key must be agreed before the ratio is computable)
  - Criterion 5 N/A for non-write surface_kinds (`dialectic:/`, `resident:/`)

  *§1 prose precision:*
  - Split the conflated "14 unique entries" claim into two distinct verified measurements: 14 KG entries tagged `auto-recovery` in `knowledge.discoveries` (whose summary text cites critical_margin_timeout) AND 12 distinct agent UUIDs in `audit.events stuck_detected` payloads with structured `reason=critical_margin_timeout` (1,261 total payload entries). Both are evidence of the same incident class from different observation surfaces.

- **v0.4 (2026-04-30, same session):** Cross-linked with parallel ontology-track plan and captured implementation skeleton into the repo.

  *Convergence story:* Between 13:03-13:05 local on 2026-04-30 (after v0.1 of this RFC was filed in KG anchor `2026-04-30T18:25:41.223729+00:00`), a parallel agent session shipped an independent ontology-track spec (`docs/ontology/beam-coordination-kernel.md`, framed as UNITARES R7 row in `docs/ontology/plan.md`) plus implementation skeleton:
  - `db/postgres/migrations/024_lease_plane.sql` — exact match to v0.3 §4.4 schema (CHECK constraints, immutability trigger, partial unique index, vocabulary). Migration was applied to the live `governance` database before being committed to the repo (DB-ahead-of-repo drift, exactly the class `feedback_post-deploy-verify-fleet-wire-ins.md` warns about).
  - `src/lease_plane/{__init__,client,models}.py` — Python contract anchor with Pydantic discriminated-union typed-absence shapes per v0.3 §4.5. Includes `LeasePlaneDisabledClient` as the advisory-mode escape valve.
  - `tests/test_lease_plane_client.py` — client test infrastructure.

  *Action taken:* Captured the parallel session's untracked work into commit `b5364d3` ("feat(lease-plane): capture parallel-session implementation skeleton") with honest provenance in the commit body. Cross-links between the proposals-track RFC and the ontology-track plan added in this v0.4 amendment. Frontmatter `related:` field updated to surface the migration, Python client, and ontology-track plan as canonical co-artifacts.

  *RFC §9 checklist update:* "Shelf-Python sketch checked in alongside the Elixir spec" item is now **complete** — the parallel session's `src/lease_plane/` Python implementation IS the contract anchor, with same schema, same API, same return shapes per the §8.5 commitment.

  *No spec changes.* This version is purely cross-linking and changelog; the technical substance of v0.3 stands unchanged. No additional council pass required.
