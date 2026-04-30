---
status: DRAFT-v0.2 (council-pass-1 complete; needs ack-pass on v0.2 amendments before implementation)
authored: 2026-04-30
amended: 2026-04-30 (v0.1, v0.2 same session)
council_pass_1: 2026-04-30
author_session: agent-68437d77-65c (claude_code-claude_68437d77)
review_target: |
  Council pass 1 complete (parallel agents, 2026-04-30):
    - dialectic-knowledge-architect: 4 BLOCKs, 5 CONCERNs, 1 NIT — all addressed in v0.2
    - feature-dev:code-reviewer: 4 BLOCKs, 3 CONCERNs, 1 NIT — all addressed in v0.2
    - live-verifier: 9 CONFIRMED, 4 DRIFT — all 4 drifts corrected in v0.2

  Recommended next: lightweight reviewer-ack pass on v0.2 amendments themselves (per the
  v2.1 ack-pass precedent in onboard-bootstrap-checkin.md), specifically on:
    1. The new §3.4 provenance attribution table (does it accurately split dialectic vs RFC?)
    2. The §7.8 substrate-earned tentative answer for lease plane's own identity
    3. The §6.1 promotion-gate SQL — does it actually return non-zero in steady state once Phase A is up?
    4. The §4.4 holder_class CHECK constraint coverage (is there a holder_class × holder_kind pair we missed?)
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

UNITARES has been paying a steady concurrency tax. The git trail since 2026-01 shows ~17 concurrency-class *code* commits in `unitares` and ~13 more in `anima-mcp`, plus several documentation/incident-tracker closures (e.g., PR #198 `docs: close S17 Redis pin deadlock row` is a tracker-row closure, not a code fix; the underlying redis pin work landed elsewhere). Code-level fixes include TOCTOU remediation, anyio<->asyncpg loop-isolation (`ExecutorPool` PR #218 — `feat(db): ExecutorPool — asyncpg loop-isolation wrapper for anyio`), pool-recovery work (PR #228 `fix(db): dedupe pool-recovery destroy log via in-lock identity re-check`, PR #230 `fix(db): structural pool wedge fix + council adversarial review`), dialectic deadlock guards (PR #50 ships within `feat(watcher): region-aware hook + dialectic deadlock guards & synthesis participant gate`), and recurring "auto-recovered stuck agent" entries firing repeatedly (14 unique entries with `tag=auto-recovery` in the 5 days ending 2026-04-30, all `reason=critical_margin_timeout`, verified via live KG query).

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

1. **Hot code reload.** BEAM supports module-level swap on a running node. This directly dissolves `feedback_running-process-vs-master-commit.md` — the long-lived-resident-vs-master-commit drift that has cost real debugging time (~15min lost 2026-04-26 alone, plus repeated `ps -o etime` + `git log --since=` checks before scoping any resident-side fix). Lease plane upgrades become module swaps without lease loss; residents migrated under BEAM later get the same property. Out of scope to *automate* hot-reload deploys in v0, but the capability is a default, not a feature.

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
  CHECK (
    (heartbeat_required = true  AND holder_kind = 'remote_heartbeat') OR
    (heartbeat_required = false AND holder_kind = 'local_beam')
  ),
  CHECK (holder_class IN ('process_instance','substrate_earned','role')),
  CHECK (holder_kind  IN ('local_beam','remote_heartbeat'))
);

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
| `reaped_after_supervisor_failed` | Reaper found a local_beam lease whose supervisor write never landed |
| `reaped_remote_ttl` | Reaper found a remote_heartbeat lease past `expires_at` |
| `handoff` | Lease transferred via `handoff_accept`; new lease_id created for the new holder |
| `forced` | Operator-issued force-release (see §7.8) |

#### 4.4.2 Heartbeat / renew semantics

`lease_renew` and `lease_heartbeat` are aliased: **both update `expires_at = now() + ttl` and (if heartbeat_required) `last_heartbeat_at = now()`** in a single atomic UPDATE. This closes the in-flight-heartbeat-vs-reaper-sweep race (council finding 4): the reaper's sole predicate is `expires_at < now() AND released_at IS NULL`, applied to all holder kinds uniformly. There is no separate `last_heartbeat_at < threshold` predicate.

Local-BEAM holders also call `lease_renew` from an in-process timer at cadence `TTL/3` so that a supervisor crash (rare; the supervisor itself is supervised) doesn't leave a corpse lease for longer than `TTL`. Default local_beam TTL is **30s** (council finding 2 — the prior `90s` window was the ghost-lease problem). Remote_heartbeat default TTL stays at 90s (Pi 180s, §7.5).

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

`{ok: false, error: "service_unavailable"}` is the **advisory-mode escape valve**: if the lease plane is down, the Python caller logs the absence and proceeds. v0 enforcement does not block on lease-plane availability. This is the same shape as the IDENTITY_STRICT='log' rollout.

## 5. API surface (v0)

```
POST /v1/lease/acquire
  body: { surface_id, surface_kind, holder_agent_uuid, holder_kind, ttl_s, intent? }
  returns: typed-absence (acquire result above)

POST /v1/lease/renew
  body: { lease_id, ttl_s }
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
  body: { lease_id, to_holder_agent_uuid, ttl_s }
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
5. **Type B audit**: a fleet-wide caller-registration audit shows ≥ 95% of callers that mutated this surface_kind during the window passed through the lease plane (proxy: count `tool_name = 'write'`-class events in `audit.tool_usage` whose surface targets match the kind, vs. count of acquire events for that kind, ratio ≥ 0.95). This is the unintegrated-caller-bypass detector. If <0.95, the integration story has gaps and Phase B promotion would block the wrong population.
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

### 7.2 Surface ID schema

Opaque string, or typed scheme?

- Opaque: simplest, but no validation, no namespace discipline, surface-kind detection is by-convention.
- Typed scheme (`file:///...`, `dialectic:/...`, `td:/...`, `resident:/...`): namespaced, validatable, surface_kind is derivable. Bounded grammar.

**Tentative:** typed scheme. `surface_kind` is the parsed scheme prefix; `surface_id` is the full canonical URI. Document the grammar in this RFC §4.4 once chosen.

Cardinality bound on active leases per surface: 1 (the unique partial index). Cardinality bound per holder: open question — should one agent UUID be allowed to hold N leases concurrently? Hermes-style multi-file edits will need this.

**Tentative:** unbounded per holder; bounded per surface. Telemetry alerts if any single UUID exceeds a soft threshold (signals stuck/leaking holder).

### 7.3 Conflict semantics on `held_by_other`

What's the caller default behavior?

- **Wait** (with timeout) until the lease frees: friendly, but creates queueing pressure inside callers and reintroduces the asyncio.Lock bug class one altitude up.
- **Abort** (return failure to operator): honest, but every caller now has retry logic.
- **Auto-request handoff:** clean, but requires a holder that responds to handoff offers, which not all holder classes do (Hermes doesn't, Claude Code doesn't, only deliberate residents would).

**Tentative:** abort by default. Caller decides whether to retry. Handoff is opt-in, used for specific surface kinds (resident:/ during planned restart, dialectic:/ during reviewer reassignment).

### 7.4 Reaper authority on local-holder death — RESOLVED in v0.2

Council finding 2 (code-reviewer) flagged that supervisor-retry-exhausted plus reaper-not-yet-swept produces a 90s ghost-lease window where a dead holder still blocks fresh acquires.

**Resolution (see §4.4.2):** local_beam leases default to **30s TTL** (not 90s) and the holder process refreshes via in-process timer at TTL/3 (10s). Single reaper predicate `expires_at < now() AND released_at IS NULL` covers both holder kinds. If supervisor `:DOWN` write succeeds: lease released within ~ms via `release_reason='down_local'`. If supervisor write fails (Postgres flapping) and the in-process timer also stops (process is dead): lease auto-expires within 30s and the reaper sweep at next tick (≤30s cadence) writes `release_reason='reaped_after_supervisor_failed'`. Worst-case ghost window is 60s, not 90s; routine case is sub-second.

Reaper sweep cadence: 30s. Tunable via env, alarmed via Sentinel if reaper falls behind (`SELECT count(*) FROM lease_plane.surface_leases WHERE expires_at < now() - interval '60s' AND released_at IS NULL` should equal 0 in steady state).

### 7.5 Heartbeat cadence + TTL math — PARTIALLY RESOLVED in v0.2

Council finding 4 (code-reviewer): heartbeat must extend `expires_at`, not just touch `last_heartbeat_at`, otherwise the reaper races in-flight heartbeats. **Resolved in §4.4.2:** `lease_renew` and `lease_heartbeat` are aliased and both update `expires_at = now() + ttl` atomically.

Council NIT 4.4 (dialectic-knowledge-architect): defaults of `90s/25s` (Mac) and `180s/60s` (Pi) are hand-tuned guesses, not derived from observed heartbeat-loss telemetry. **OPEN — operator action item before Phase B promotion of any remote_heartbeat surface_kind:** instrument the existing Pi↔Mac heartbeat path (Steward sync, anima-mcp HTTP heartbeats) and measure the actual gap distribution over ≥7 days. Set TTL such that p99 of observed gaps × 1.5 ≤ TTL. If the measured p99 is wildly different from 60s, revisit defaults. Until measured, keep the hand-tuned defaults but treat them as provisional.

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

**Tentative:** option 3 (substrate-earned). The lease plane is genuinely a substrate, not a participating agent — its UUID is hardcoded, its audit rows are tagged with that UUID, and it does not run governance check-ins. This minimizes ontology churn. The ontology amendment for option 2 (BypassAgent) can come later if multiple system-level services emerge that genuinely need to be a class together.

**Anti-recursion guard:** the lease plane MUST NOT acquire leases for its own outbox writes (would be a self-deadlock at startup). This is enforced by code: the lease-plane-internal Postgres role does not have `INSERT` privilege on `surface_leases` for `surface_kind='lease_plane'`. Belt-and-braces.

### 7.9 Surface_id renames / re-keying (council finding 4.2)

A file rename, dialectic-session ID rotation, or resident relabel changes a surface's canonical ID. Active leases keyed on the old ID become orphans; new acquires on the new ID succeed; the "same surface" is double-leased semantically while the index thinks each entry is unique.

**Tentative:** v0 does not handle this. Active leases on a renamed surface must be explicitly released by the holder and re-acquired against the new ID. If the holder is unaware of the rename (e.g., another agent renamed the file), the orphan lease ages out via TTL. Document the gap in the operator runbook; revisit in a v1 if it becomes a real-world incident.

**Open question:** should `surface_id` be a *content-derived hash* (e.g., file inode + creation timestamp) instead of the literal path, to make renames invisible to the lease layer? This trades simplicity for robustness and probably belongs in v1, not v0.

### 7.10 Force-release authority (council finding 4.3)

`release_reason='forced'` exists in §4.4.1 vocabulary but the RFC didn't specify *who can issue it*. Force-release is a privilege-escalation surface: any caller who can force-release can free another agent's lease and acquire it themselves.

**Tentative:** force-release requires a separate elevated bearer token (`OPERATOR_FORCE_RELEASE_TOKEN`, distinct from the standard `GOVERNANCE_TOKEN`), sourced from `~/.config/cirwel/secrets.env` like all other UNITARES secrets. Operator-only. Logged to `lease_plane_events` with `event_type='forced'` and the operator's session_id, projected to `audit.tool_usage` like any other event. Sentinel alarm fires on every force-release event regardless of context — this is rare enough that an alarm-on-every-event is appropriate, not noisy.

**Anti-pattern:** the standard MCP bearer token must NOT permit force-release. Confirmed via integration test before Phase A ships.

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

- [ ] Council pass: dialectic-knowledge-architect, feature-dev:code-reviewer, live-verifier (parallel)
- [ ] §7 open questions all answered (RFC tentative -> RFC committed)
- [ ] Shelf-Python sketch checked in alongside the Elixir spec — same schema, same API, same return shapes
- [ ] Operational runbook draft: `docs/operations/lease-plane-operator-runbook.md` (stub created alongside this RFC; needs concrete commands once the service exists)
- [ ] Sentinel monitoring spec for `/v1/lease/status?surface_id=__healthcheck__`
- [ ] Decision: which exact surface_kind goes first into advisory (probably `dialectic:/`)
- [ ] Decision on §6.1 promotion-gate criteria — what specifically counts as "the conflict log says 'we would have prevented a real bug here'"

## 10. Runway tradeoff (operator decision, not technical)

This is a 4-8 week spike. It trades against:

- Fellowship deadline / Anthropic application
- Paper v6.9.x polish and v7 corpus-maturity work
- Public plugin / discord-bridge community work
- KG/UX maintenance and dogfood-driven fixes

The technical case is strong (three independent reviewers converged). The strategic case is the operator's call. If shelved, file this RFC as captured-decision so the next session doesn't re-litigate the substrate question from scratch.

## 11. Versions / changelog

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
