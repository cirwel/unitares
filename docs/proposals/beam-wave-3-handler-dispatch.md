# Wave 3 RFC: handler dispatch + identity middleware + dialectic resolution → BEAM

**Status:** v0.2, 2026-05-09. Full redraft superseding v0.1 / v0.1.1 / v0.1.2 (preserved on `wave-3-rfc-draft` branch as historical record). v0.2 is a single coherent document; v0.1.x amendment-stacking is removed.
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` v0.3.1.
**Sibling, completed:** `docs/proposals/beam-wave-1-sentinel.md` (Surface 1+2 shipped; Surface 3 in flight).
**Sibling, completed:** `docs/proposals/surface-lease-plane-v0.md` Phase A + Wave 2 hardening (#412/#414/#417/#418/#419).
**Wave 0 channel:** `audit.coordination_events` exists with `event_type ~ '^(coordination_failure)(\.[a-z_]+)+$'` CHECK constraint; zero rows as of 2026-05-09. The constraint scopes the table to failure events only — informational latency (lease-plane Phase A baseline, boundary measurements) needs a parallel channel; see §6.
**Single-writer surface:** Identity / onboarding (per `CLAUDE.md` "Before Starting Work on a Single-Writer Surface") spans this entire RFC plus its prereq PRs. Branch from this RFC's head before any parallel work.

## What changed in v0.2 (vs v0.1.2)

The third council pass on v0.1.2 returned BLOCK / BLOCK / DRIFT against six structural items. v0.1.2's own escalation rule named v0.2-redraft-from-scratch as the discipline; this draft executes that. v0.2 differs from v0.1.2 in six load-bearing places, all schema- or contract-grounded:

1. **Shadow-divergence design (was §B3.2).** v0.1.x's comparator referenced columns (`agent_uuid`, `api_key`, `label`, `public_agent_id`) that match neither `core.identities` nor `core.agents`. v0.2 uses live schemas verbatim, ships **two** shadow tables (`core.identities_shadow` + `core.agents_shadow`), and uses a full-outer-join comparator that detects value mismatch, canonical-row-missing, and shadow-row-missing distinctly. See §8.
2. **Saga state machine (was §B2.2).** v0.1.x's saga referenced `session_id UUID` against a live `core.dialectic_sessions.session_id TEXT`. v0.2 uses TEXT and adds two intermediate states (`reserved` → `paused_agent_applied` → `both_agents_applied` → `pg_committed`) so crash-restart can distinguish "PG committed but agents unaware" from "agents applied but PG uncommitted." See §9.
3. **Cache coherence (was §B6.2 + §B7.2).** v0.1.x relied on fire-and-forget Redis pub-sub for baseline-cache invalidation and feature-flag updates. Lossy pub-sub plus subscriber reconnect = silent divergence — exactly the substrate-tax pattern the RFC was trying to avoid. v0.2 uses a **versioned baseline** in PG (transactional version counter) plus a bounded reconciliation loop; pub-sub is an optimization layer, not the invariant. See §10.
4. **Latency measurement namespace (was §C2.2).** v0.1.x put informational lease-plane latency under `coordination_failure.beam_python_boundary.lease_plane_request`. The CHECK constraint on `audit.coordination_events.event_type` forces that prefix to mean "failure"; reading the table for failure metrics would pollute the result. v0.2 splits: a sibling table `audit.coordination_measurements` with the same shape but a different CHECK on event_type, scoped to informational events. See §6.
5. **§0 framing.** v0.1.1/v0.1.2 stacked bias-acknowledgments in prose. v0.2 removes the bias archeology; the bias discipline now lives in *how disconfirmer thresholds are anchored* — every threshold names the measurement source (existing or named-TBD) and the gate halts if the measurement is missing.
6. **Prereq-PR table.** v0.1.x had an 8-row prereq table mixed with amendment cross-references. v0.2 has one §14 table; nine rows; each row names the file paths it creates/touches and its dependency on prior PRs.

The §2 lock-invariant inventory, §3 state-ownership matrix, §5 dialectic split, §7 test strategy, and §12 stop signs are preserved structurally with verifier errata (DRIFT/REFUTED items 1–12 from v0.1.1) folded inline; no separate errata table.

---

## §0 Falsifying-evidence question

> **What evidence would update us away from porting handler dispatch + identity middleware + dialectic resolution to BEAM?**

The RFC opens with this question because per `feedback_substrate-migration-status-quo-bias.md` and the symmetric warning in `beam-footprint-roadmap-v0.md` §"Why Read A, not Read B", both substrate enthusiasm and substrate resistance are biases. The disconfirmers below name what would actually halt Wave 3, with each threshold anchored to a measurement source.

### Disconfirmer set

**(A) User-visible-metric headroom — two paths.**
- **(A.1) ODE-floor dominates.** ODE profile against still-Python `governance_core/phase_aware.py` and `governance_core/stability.py` shows >60% of `process_agent_update` p99 floor in `governance_core/` math over a 7-day production sample. Anchor: 60% chosen because below 50% the answer is ambiguous (port-helps-some), above 75% would let cases through where the math is *not yet* dominant. **Measurement source:** ODE profile commit on master (named in §14 PR #1). Wave 3 implementation cannot start before this commit lands.
- **(A.2) In-place Python fix closes the gap.** Any Python-side fix shipped during Wave 3 implementation window brings `process_agent_update` p99 below **2.0s** without porting. Anchor: per `project_locked-update-overhead-fix.md`, current per-turn overhead is ~5.0s post-#372; PR #3 deferred pending benchmark. 2.0s = "in-place fixes brought it to 40% of current" — defensible bar. **Measurement source:** `process_agent_update` p99 from existing production telemetry.

**(B) Boundary cost ≥ substrate tax removed.** `audit.coordination_measurements` channel (§6, prereq PR #6) shows sustained per-call boundary cost p50 ≥ lease-plane Phase A measured p50 × 2 OR p99 ≥ lease-plane Phase A measured p99 × 3 over a 14-day window. Anchor: ×2/×3 multipliers reflect Wave 3's heavier per-call payload (full request marshalling vs lease ack). **Measurement source:** lease-plane Phase A latency instrumentation (prereq PR #6) must produce ≥14 days of `audit.coordination_measurements` rows before disconfirmer (B) thresholds can be set. If <14 days at Wave 3 implementation gate, **gate halts on missing measurement** — there is no fallback default.

**(D) MCP SDK gate reverses.** Hands-on spike on `mcp_elixir_sdk` 1.0.1 or `hermes_mcp` 0.14.1 shows production-disqualifying failure (broken-on-Anthropic-streaming, MCP-spec drift, no maintainer responsiveness). Doubles disconfirmer (B)'s budget per §6's 4-crossing topology. **Measurement source:** spike result, recorded in `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` artifact before implementation gate.

**(E) State-ownership cutover structurally unsafe.** Identity middleware port (§3) surfaces irreducible per-request semantics that can't be moved to GenServer state without replicating coordination at the boundary. **Measurement source:** §3 surface-by-surface analysis at implementation gate; if any new "irreducible" surface is found beyond the eight in §3.1, gate halts.

**(F) Opportunity cost.** Wave 3 implementation projected calendar-weeks > (Wave 1 elapsed × 3) AND any of {paper deadline, fellowship application, HLH, R2 Phase 2 gate} would be sacrificed. "Sacrificed" defined as: calendar-week slip on any named item exceeds 25% of original deadline window OR operator's written go-decision document explicitly accepts the slip. **Measurement source:** `docs/proposals/wave-3-go-decision-<date>.md` artifact written by operator at gate, with §"Calendar reasoning" section enumerating each named item.

**(G) Dialectic-quality regression.** During canary, dialectic session-resolution rate (resolved / (resolved + failed + escalated) over a 14-day window) regresses >5% against pre-Wave-3 baseline. Reviewer-reassignment rate increases >20%. **Measurement source:** baseline computed from trailing 30 days of `core.dialectic_sessions` rows (47 total as of 2026-05-09; gate halts on insufficient baseline volume if 30-day window has <30 sessions). Both baseline mean and σ pinned in §11 prior to implementation start; pinning commit is itself a Wave 3 prereq (PR #9).

### What disconfirmation is NOT

- Wave 1 / Wave 2 "shipping without incident" is not confirmation. Clean operations with bad boundary numbers is disconfirmer (B).
- "BEAM is the right substrate philosophically" is not evidence — it is the prior the bias warning targets.
- Operator preference is not evidence at the Go gate (it was the input at scope; the gate is evidence-bearing).

§11 makes Wave 3's go-decision conditional on every disconfirmer being measured-and-not-triggered. There is no "structural success but user-visible miss" escape hatch; if any disconfirmer fires or any measurement source is missing at the gate, Wave 3 halts and the roadmap re-opens.

---

## §1 Roadmap-level scope

- **Handler dispatch** (the `@mcp_tool` decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per disconfirmer (D)) and proxies to BEAM after request unmarshalling.
- **Identity middleware** (`src/mcp_handlers/middleware/identity_step.py`, the session-context contextvar chain, agent_id resolution, label resolution) ports to BEAM. Largest single coordination surface in governance MCP today.
- **Dialectic resolution** (`src/mcp_handlers/dialectic/`) ports to BEAM. The reasoning logic (numerical synthesis math, condition merging, signature crypto) stays Python and is called from BEAM via the boundary. The coordination layer (session lifecycle, quorum tracking, condition resolution, audit emission) ports.
- **Out of scope:** `governance_core/`, Watcher, the LLM SDK call paths inside handlers (those stay Python and are called from BEAM via Ports/HTTP).

---

## §2 Lock-invariant inventory

The lock surface is `StateLockManager.acquire_agent_lock_async` (`src/state_locking.py:286-423`), bracketing the `execute_locked_update` phase chain in `src/mcp_handlers/updates/phases.py`. Eleven invariants:

| # | Invariant | File:line | Wave 3 mapping |
|---|-----------|-----------|------------------|
| 1 | api_key PG/cache reconciliation (three-way: UUID, api_key, cache) | `phases.py:723-798` | Single GenServer mailbox message — atomic; api_key auth desync risk if relaxed |
| 2 | thread_id / node_index monotonic on `active_session_key` change | `phases.py:822-851`; persist helper `phases.py:670-693` (`_persist_thread_identity_async`) | Explicit-relax with named tolerant consumer; Wave 3 BEAM saga can synchronously persist within session-resolution saga (§9), eliminating the staleness window for that path |
| 3 | previous_void_active snapshot (read-once before ODE, used post-lock for CIRS) | `phases.py:800-807` capture; `phases.py:1125-1137` use | Single GenServer message — must NOT re-read post-ODE |
| 4 | Monitor lifecycle: metadata fetched (743/768/789) and monitor lookup (803) refer to same agent under one lock | `phases.py:743-798, 803-807, 880-923` | Single GenServer message (corollary of 1) |
| 5 | Dialectic session lock: SYNTHESIS→RESOLVED serialization across `submit_synthesis(agrees=True)` | `dialectic/handlers.py:1184` (uses `get_session_lock` from `dialectic/session.py:55`) | Session-keyed GenServer mailbox + saga (§9). The asyncio.Lock in `session.py:51-68` (`_SESSION_LOCKS` + `_SESSION_LOCKS_DICT_LOCK`) is replaced |
| 6 | Baseline preload: `get_baseline_or_none(agent_id)` once per process; cached in `_baseline_cache` (`governance_core/ethical_drift.py:418`) | `phases.py:809-820, 856-899` | PG-anchored with versioned cache — see §10 |
| 7 | Monitor state snapshot: pre-ODE (596-602) used for ODE input; post-ODE re-read (1143-1147) used for CIRS emission; MUST NOT cross-contaminate | `phases.py:536-602, 1143-1147, 1156-1164, 1203-1223` | Single GenServer message carrying both snapshots; BEAM must not split |
| 8 | Metadata cache-PG eventual consistency (corollary of 2) | `phases.py:823-851, 670-693, 928-943` | Explicit-relax as cross-layer contract |
| 9 | api_key mutable reference under lock (corollary of 1) | `phases.py:745, 778, 792, 798, 905-911` | Single GenServer message (covered by 1) |
| 10 | CIRS void_active transition guard (corollary of 3) | `phases.py:800-807, 1125-1137` | Single GenServer message (covered by 3) |
| 11 | Agent-state mutation ordering: agent_state immutable for ODE input; result immutable post-ODE | `phases.py:635-668, 709-920, 1010-1240` | Architectural pattern — BEAM message handler's pure-functional shape preserves this for free if dispatch is single-message-per-update |

Invariants 1, 3, 4, 5, 7, 9, 10 collapse into single GenServer mailbox messages. Invariants 2, 6, 8 are explicit-relax (named tolerant consumers). Invariant 11 is structural.

**Multi-process serialization for invariant 5.** Wave 3 introduces multi-OS-process operation. Three options:
- **(i) PG advisory lock** per session_id (`pg_try_advisory_lock(hashtext(session_id))`). Observability gap: lock leaks on connection death.
- **(ii) `SELECT … FOR UPDATE`** on `core.dialectic_sessions` row at the start of any phase-mutating message handler. Row-level lock; releases on transaction commit. Doesn't break under multi-node BEAM.
- **(iii) GenServer-process-registry** serialization. Sufficient for single-BEAM-node only; requires re-port if multi-node ever ships.

**Recommendation: (ii).** Doesn't break under multi-node BEAM (parent roadmap §"Post-Wave-3 candidates" names multi-node as a real possibility). Verify (ii) safety against the `updated_at` trigger at `db/postgres/schema.sql:157` — FOR UPDATE + trigger + concurrent reads can deadlock under PG MVCC; council should confirm trigger doesn't acquire conflicting locks before (ii) is final. (iii) becomes the optimization, taken later if profiling shows row-level lock contends.

---

## §3 State ownership and rollback during transition

### 3.1 Surface inventory

Identity middleware decomposes into eight state surfaces. Source-cited from `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/identity/{resolution,persistence,session}.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/context.py`, `src/background_tasks.py`.

| # | Surface | Read | Write | Source of truth | BEAM port strategy | Cutover semantics |
|---|---------|------|-------|------------------|---------------------|---------------------|
| A | ContextVars (10 declarations; 4 identity-bearing) | `context.py:131-147` (incl. `update_context_agent_id` at 141-147 — writer) | `context.py:86-114` | Process memory only (async-task-local) | Stays Python at boundary; BEAM threads request-context explicitly through GenServer state. Marshalled context-payload bytes-per-request enters disconfirmer (B) budget | Direct flip — ephemeral |
| B | Sticky transport binding cache (3-layer: dict / Redis / PG fallback) | `identity_step.py:289-298` (Redis recovery 0.5s timeout at 292) | `identity_step.py:98-157` (fire-and-forget Redis), `:230-248` (invalidate) | In-memory dict when populated; Redis when recovered; no PG anchor | BEAM owns as per-process GenServer state OR stays Python | No shadow needed — drop in-memory cache → next request falls through |
| C | Session→UUID Redis cache (`sticky:{ip_ua_fingerprint}:{mcp_session_id}` keys) | `resolution.py:430-470` (PATH 1) | `persistence.py:175-200` (`_cache_session` SETEX); NX in inner `_cache_session_redis_write` at 206+ | PostgreSQL canonical; Redis is speed cache | Shadow ≥1 cycle then flip | Rollback: re-enable Python writes, BEAM HTTP-read-only. ≤1-request consistency window at flip |
| D | PG canonical identity (`core.identities` AND `core.agents` upsert on PATH 3 fresh mint) | `resolution.py:950-1116` (PATH 3) | `db.upsert_identity`, `db.upsert_agent` | PostgreSQL (both tables; coupled) | BEAM owns the upsert; PG INSERT/UPDATE moves into GenServer message atomicity | Shadow ≥1 cycle then dual-write window then BEAM-only. Both tables shadowed; see §8 |
| E | Continuity token (HMAC over agent_uuid + chh + exp + iat + sid + opv); actual fields: `v`, `opv`, `sid`, `aid`, `mf`, `ch`, `iat`, `exp` (verifier-corrected from v0.1) | `session.py:176-220` | `session.py` (`create_continuity_token` at onboard) | Cryptographic — token string IS source | Stays Python OR moves to BEAM — orthogonal | No rollback contract |
| F | Onboard PIN (Redis-keyed `onboard_pin:{ip_ua_fingerprint}` with model scoping; IPUA pin treats `agent_id` as proof per `project_ipua-pin-agent-id-proof.md`) | `session.py:769-797` (`lookup_onboard_pin` with `_PIN_REDIS_TIMEOUT = 0.5s` at line 28) | `session.py` (`set_onboard_pin` SETEX, 30m TTL) | Redis (TTL 30m); IPUA invariant locked by contract test | Shadow ≥1 cycle then flip; IPUA invariant CANNOT be relaxed | Shadow then flip |
| G | Agent metadata cache (`mcp_server.agent_metadata[uuid]`) | `agent_auth.py:59-134, :151, :309-515` (`require_registered_agent` ends at 515) | `background_tasks.py:343` (`background_metadata_load` — verifier-corrected from `load_agent_metadata`) | PostgreSQL `core.agents` canonical; in-memory dict is read-side cache | PG-anchored with versioned cache, see §10. OTP gen_server watches PG for changes; both BEAM + Python subscribe via the broadcast channel | No rollback contract — read-mostly, stale degrades gracefully |
| H | Identity honesty gates (`identity_strict_mode`, `ipua_pin_check_mode`) | `identity_step.py:365-474`, `agent_auth.py:271-293` | PG `core.feature_flags` (new in §10 — versioned, cache + reconciliation, NOT Redis pub-sub) | PG canonical | BEAM mirrors flag check at same dispatch entry | Direct flip via flag write; both runtimes converge within 60s reconciliation interval |

### 3.2 Rollback procedure

1. **Snapshot before flip.** `pg_dump` `core.identities`, `core.agents`, `core.identities_shadow`, `core.agents_shadow`, `core.dialectic_sessions`, `core.dialectic_messages`, `coordination.session_resolution_sagas`, `core.feature_flags` into `~/backups/governance/wave-3-pre-cutover-<ISO8601>/`.
2. **Plist swap.** New plist `com.unitares.handler-dispatch-beam.plist` in `scripts/ops/`. Cutover loads BEAM; rollback unloads BEAM and reloads `com.unitares.governance-mcp.plist`.
3. **503 circuit-breaker for the gap.** Python MCP transport, when proxying to BEAM, returns HTTP 503 `{"ok": false, "error": "governance_temporarily_unavailable", "reason": "handler_dispatch_unavailable"}` with `Retry-After: 5` on connection-refused or timeout. Clients (Watcher, Sentinel, SDK consumers) gain matching retry-on-503 logic before cutover. Rollback step order: stop BEAM writes first → transport returns 503 during gap → restore Python writers → transport resumes 200. **Stop sign #7:** 503 rate during cutover/rollback exceeding 1% of requests for >60s halts the procedure.
4. **Schema rollback.** Every new migration ships a paired DOWN migration; tested on `governance_test` snapshot before cutover migration runs in production.
5. **Per-surface windows:** A/E/H instantaneous; B/C/G ≤2h staleness (TTL); D ≤1-request inconsistency at flip moment (shadow + dual-write window keeps it bounded); F instantaneous.

---

## §4 Multi-writer enforcement gate

Wave 3 introduces multi-OS-process operation. Python MCP transport must stop accepting writes for an agent while its BEAM GenServer is mid-update. Two options:

- **(α)** Open a `resident:/` Phase B window via amendment to `surface-lease-plane-v0.md`. Forces every Python resident (Sentinel, Vigil, Chronicler, in-process Steward) to learn fail-closed-on-deny semantics in the same window as cutover — couples two large changes.
- **(β) — recommended.** Per-agent PG advisory lock at the writer entry point (`pg_try_advisory_lock(hashtext(agent_uuid))`). BEAM acquires on enter, releases on exit; Python writers attempt with 50ms timeout and fail-fast (returning 503-equivalent surfaced as `governance_temporarily_unavailable`). Keeps lease plane unchanged.

If (α) is chosen, B5.2 from v0.1.2 stands: operator evaluates `resident:/` against `surface-lease-plane-v0.md` §6.1 criteria and flips a flag rather than shipping a PR. If (β) is chosen (recommended), §4 is a binding implementation spec. Council confirms before implementation gate.

---

## §5 Dialectic stateful/stateless split

### 5.1 Coordination → BEAM session-keyed GenServer

| File:line | Function | Why coordination |
|-----------|----------|--------------------|
| `dialectic_protocol.py:464-512` | `DialecticSession.__init__` (body); `_generate_session_id` at 513-524 | Session lifecycle init |
| `dialectic_protocol.py:526-552` | `submit_thesis` | THESIS→ANTITHESIS; auth |
| `dialectic_protocol.py:554-585` | `submit_antithesis` | Reviewer auto-assign; ANTITHESIS→SYNTHESIS |
| `dialectic_protocol.py:587-638` | `submit_synthesis` | Convergence check; multi-participant coordination |
| `dialectic_protocol.py:781-897` | `finalize_resolution` | Dual-signature canonical-payload-v2 coordination |
| `mcp_handlers/dialectic/handlers.py:55-63` | `_resolve_dialectic_agent_id` | Auth boundary |
| `mcp_handlers/dialectic/handlers.py:130-177` | `check_reviewer_stuck` | Circuit-breaker (2h antithesis); phase-gated |
| `mcp_handlers/dialectic/handlers.py:241-334` | `_build_dialectic_actionability` | State-machine assembly |
| `mcp_handlers/dialectic/handlers.py:368-412` | `_apply_reviewer_reassignment` (verifier-corrected: 335-366 is `_validate_explicit_reviewer_candidate`, different function) | Stuck-session recovery |
| `mcp_handlers/dialectic/handlers.py:414-635` | `handle_request_dialectic_review` | Session creation; PG write `pg_create_session` line 478 |
| `mcp_handlers/dialectic/handlers.py:897-985` | `handle_submit_thesis` | PG write `pg_add_message` 910; phase transition 922 |
| `mcp_handlers/dialectic/handlers.py:986-1147` | `handle_submit_antithesis` | Reviewer assign 1040; phase transition 1056 |
| `mcp_handlers/dialectic/handlers.py:1148-1388` | `handle_submit_synthesis` | Convergence 1206-1228; round 1181; **invariant 5 critical section** |
| `mcp_handlers/dialectic/handlers.py:1389-1506` | `handle_reassign_reviewer` | `pg_update_reviewer` 1460 |
| `mcp_handlers/dialectic/resolution.py:18-196` | `execute_resolution` | Agent state mutation (status→active, paused_at=None at 74-75) |
| `mcp_handlers/dialectic/auto_resolve.py:54-220` | `auto_resolve_stuck_sessions` | Periodic detection; reviewer reassignment |
| `mcp_handlers/dialectic/reviewer.py:121-200, 255+` | `is_agent_in_active_session`, `select_reviewer` | Quorum-prevention; collusion gate |

### 5.2 Computation → stays Python, called from BEAM

| File:line | Function | Why computation |
|-----------|----------|------------------|
| `dialectic_protocol.py:1077-1162` | `calculate_authority_score` | numpy sigmoid + Jaccard + weighted aggregation; pure |
| `dialectic_protocol.py:640-657` | `_normalize_condition_terms`, `_semantic_similarity_terms` | Term extraction + Jaccard; pure |
| `dialectic_protocol.py:659-743` | `_merge_proposals` | Semantic matching (0.6 threshold); pure |
| `dialectic_protocol.py:746-779` | `_conditions_conflict` | Regex + term-overlap heuristics; pure |
| `dialectic_protocol.py:250-265` | `DialecticMessage.sign` | HMAC-SHA256; deterministic |
| `dialectic_protocol.py:350-410` | `Resolution.compute_signature`, `verify_signatures` | HMAC keyed MAC; pure |
| `dialectic_protocol.py:899-986` | `check_hard_limits` | Safety regex; stateless |
| `mcp_handlers/dialectic/handlers.py:180-200` | `_read_proposed_conditions` | Input normalization; pure |
| `mcp_handlers/dialectic/calibration.py` (imported 99-102) | calibration updates from session outcomes | Statistical correlation; numeric |
| `mcp_handlers/support/condition_parser.py` | condition parsing/application | Numeric/text transformation; stateless |

### 5.3 Boundary cases

| File:line | Function | Judgment | Reason |
|-----------|----------|----------|--------|
| `dialectic_protocol.py:995-1031` | `check_timeout` | **SPLIT**: wrapper ports to BEAM, `_compare_against_timeout(now, created_at, phase, timeouts)` predicate stays Python | Time-comparison is pure; FSM-phase decision is coordination |
| `mcp_handlers/dialectic/reviewer.py:55-119` | `_has_recently_reviewed` | **PORTS to BEAM** as part of session-keyed GenServer's reviewer-selection coordination. PG round-trip remains (Postgrex query directly), boundary crossing disappears. | Splitting the call from selection saves nothing; the Python→BEAM→Python sandwich is the cost to remove |
| `mcp_handlers/dialectic/auto_resolve.py:32-51` | `_parse_timestamp` | Stays Python utility | Pure helper |
| `dialectic_protocol.py:318-329` | `Resolution.hash` | Stays Python utility | Pure crypto |
| `dialectic_protocol.py:331-347` | `Resolution.canonical_payload` | Stays Python utility (load-bearing for v2 signing) | Pure |

### 5.4 Storage surfaces

- `core.dialectic_sessions` (`session_id TEXT PK, paused_agent_id TEXT, reviewer_agent_id TEXT, phase, status, ...` — verified live schema). Wave 3 BEAM session-keyed GenServer reads/writes via boundary; on-disk schema unchanged.
- `core.dialectic_messages` (append-only). BEAM appends; schema unchanged.
- `audit.coordination_events` (failure events; CHECK constraint on `event_type` prefix). Wave 3 wires dialectic state-transition emissions via the helper in §6.
- `audit.coordination_measurements` (NEW in §6, prereq PR #6). Informational metrics including lease-plane Phase A latency baseline.
- `data/dialectic_sessions/<session_id>.json` (env-gated by `UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT`, default ON per `session.py:71-75`). During shadow window, BEAM does NOT write; Python continues. Post-flip: BEAM writes, Python stops. Single writer always; no merge step.

### 5.5 Lifecycle FSM

```
THESIS → submit_thesis() → ANTITHESIS
ANTITHESIS → submit_antithesis() → SYNTHESIS (round 1)
SYNTHESIS → submit_synthesis():
    agrees=True → RESOLVED (terminal)
    agrees=False AND round < max → SYNTHESIS (round N+1)
    round ≥ max → FAILED (terminal)
ANTITHESIS (if check_reviewer_stuck) → auto_resolve → FAILED OR new ANTITHESIS (reviewer reassigned)
ESCALATED, quorum_voting — reserved (CHECK constraint allows them); not implemented; out of Wave 3 scope
```

Phase-enforcement guards (lines 535-536, 569-570, 601-602) become message-handler preconditions on the BEAM side, not wrapping locks.

### 5.6 Boundary endpoint (single)

For computation calls, BEAM calls Python via a single endpoint:

```
POST /v1/dialectic/compute
{
  "mode": "synthesize" | "select_reviewer" | "compare_timeout",
  "session_id": "<TEXT, idempotency key>",
  "round": <int, idempotency key for synthesize>,
  "input": { ...mode-specific bounded input... }
}
```

Response: `{"result": {...}, "elapsed_ms": <int>, "cache_hit": <bool>}`.

Idempotency: `(session_id, round, mode)` tuple; same input within a 60s window returns cached result. Timeout: BEAM applies 2.0s budget; on timeout, BEAM emits `coordination_failure.beam_python_boundary.beam_to_python_request_failed` with `error_class="timeout"` and fails the synthesis round (no retry at boundary; retry policy lives in saga §9).

---

## §6 Boundary instrumentation — failure vs measurement separation

The existing typed event constants `python_to_beam_request_failed` and `beam_to_python_request_failed` (PR #408) live in `audit.coordination_events`. That table's CHECK constraint (`event_type ~ '^(coordination_failure)(\.[a-z_]+)+$'`) locks `event_type` to *failure* events. Informational latency (lease-plane Phase A baseline, per-call boundary round-trip measurements) does not belong there.

### 6.1 New table for informational measurements

Prereq PR #6 ships:

```sql
CREATE TABLE audit.coordination_measurements (
    ts          TIMESTAMPTZ NOT NULL,
    event_id    UUID NOT NULL DEFAULT gen_random_uuid(),
    service     TEXT NOT NULL CHECK (service IN ('sentinel','governance_mcp','lease_plane','vigil','chronicler','watcher')),
    event_type  TEXT NOT NULL CHECK (event_type ~ '^(measurement|telemetry)(\.[a-z_]+)+$'),
    agent_id    TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    context     JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(context) = 'object'),
    PRIMARY KEY (ts, event_id)
) PARTITION BY RANGE (ts);
```

Same partition strategy + indexes as `audit.coordination_events`. Different CHECK on `event_type` admits measurement-namespace events.

### 6.2 Failure call-sites (existing channel, `audit.coordination_events`)

Wave 3 wires `coordination_failure.beam_python_boundary.*` emissions at:
- BEAM handler-dispatch ↔ Python MCP transport: `python_to_beam_request_failed` / `beam_to_python_request_failed` on non-2xx.
- BEAM identity middleware → Python `governance_core/` math: `beam_to_python_request_failed` on Port/HTTP failure.
- BEAM dialectic GenServer → Python `/v1/dialectic/compute`: `beam_to_python_request_failed` on synthesize/select_reviewer/compare_timeout failure.
- BEAM handler-dispatch → Python LLM SDK paths: both directions.

Plus `coordination_failure.redis_pubsub_lag` event_type (lands in prereq PR #2 with the WAVE_0_EVENT_TYPES update) emitted when the optional pub-sub layer (§10) lags >60s.

### 6.3 Measurement call-sites (new channel, `audit.coordination_measurements`)

- Lease-plane Python client emits `measurement.lease_plane.request` on every request to `127.0.0.1:8788`, payload `{endpoint, method, status_code, elapsed_ms}`. Prereq PR #6's primary deliverable; runs ≥14 days before disconfirmer (B) thresholds can be set.
- Wave 3 BEAM handler-dispatch emits `measurement.beam_python_boundary.request` on every successful boundary call (failures stay in `coordination_failure.*`), payload `{endpoint, method, elapsed_ms, payload_bytes}`.
- Marshalled context-payload bytes-per-request (Surface A in §3) is included in the payload above; enters disconfirmer (B) via the §6.5 dashboard.

### 6.4 Emission helper (enforcement)

`governance_core/coordination_events_helpers.py::make_boundary_payload(endpoint, method, error_class, status_code, elapsed_ms) -> dict` raises `ValueError` on None/empty/missing `error_class`. All `coordination_failure.beam_python_boundary.*` emissions MUST go through this helper. New helper `make_measurement_payload(endpoint, method, status_code, elapsed_ms, payload_bytes) -> dict` for the measurement channel. Direct dict construction is prohibited; CI lint (grep for event_type constants in non-helper code) fails the PR. Same pattern applies to BEAM emissions (Elixir-side helper module).

### 6.5 Wave 0 query

`scripts/ops/wave-0-channel-report.sh` reads both tables and produces, over a stated window: count, p50/p99 elapsed_ms, error_class breakdown by endpoint, separated into failure vs measurement panels. This is what disconfirmer (B) reads against.

### 6.6 Per-call boundary topology

```
MCP request
    ↓
Python MCP transport (unmarshal)
    ↓ [crossing 1: Python→BEAM via Ports/HTTP]
BEAM handler dispatch (route, identity middleware, dialectic coordination)
    ↓ [crossing 2: BEAM→Python for governance_core math + LLM SDK]
Python governance_core compute + LLM SDK
    ↑ [crossing 3: Python→BEAM with compute result]
BEAM continues handler dispatch (audit emit, response shape)
    ↑ [crossing 4: BEAM→Python for response serialization]
Python MCP transport (marshal response)
    ↓
MCP response
```

Per-call: up to 4 boundary crossings worst-case (dialectic-touching + governance_core math), 2 best-case (no dialectic + no math). Disconfirmer (B) budget at 4× per-crossing cost is correctly worst-case-anchored. The disconfirmer (B) budget MUST be set against measured-not-estimated per-crossing cost from lease-plane Phase A baseline per disconfirmer (B)'s measurement source.

---

## §7 Test strategy

### 7.1 Acceptance test classes

- **(a) Python suite.** All ~8400+ tests in `tests/`. Pre-cutover gate: full green.
- **(b) ExUnit suite.** New `elixir/handler_dispatch/test/`. Tests: fixture MCP request → BEAM dispatch → Python handler invoked with correctly-marshalled args; identity middleware fixture (process_agent_update with `parent_agent_id`) → asserts lineage write to PG with shape matching `src/mcp_handlers/middleware/identity_step.py`; dialectic GenServer fixture (create → join → quorum → resolve) → asserts same `audit.coordination_events` row sequence.
- **(c) Cross-runtime integration.** New `tests/integration/test_wave_3_boundary.py` drives full pipeline; asserts response shape matches pre-Wave-3 Python-only path under §7.2 byte-equivalence definition.
- **(d) Behavioral parity.** Operator-led; existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff.

### 7.2 "Byte-identical" defined

- Same JSON field-set, same value types (int stays int, float stays float — no implicit coercion), same nested dict ordering (Python 3.7+ dict insertion-order preserved), same float precision (12 decimal digits). String-byte equality NOT required.
- Golden-capture fixture (prereq PR #5): `tests/fixtures/wave3_response_golden/` with 50+ captured responses across the full handler surface.
- Comparison test `tests/integration/test_wave_3_response_parity.py` runs same fixture inputs against BEAM-side dispatch.
- **Timestamp masking:** keys matching `(.*_at|.*_time.*|.*_ms|server_time|processing_time_ms|elapsed_ms|created)` are masked before comparison. Capture script `scripts/dev/wave3-capture-goldens.sh` applies same masking; if a handler adds a non-deterministic field that doesn't match the regex, capture fails noisily (lint-style assertion).
- **Pre-cutover gate:** 100% golden-response parity. Failure of any golden halts cutover.

### 7.3 IPUA pin pipeline test

Prereq PR #4 lands `tests/integration/test_identity_path2_ipua_pin_pipeline.py` driving `handle_onboard_v2` end-to-end with `agent_id` in `arguments`, asserting strict-mode passthrough invariant. Wave 3 BEAM identity middleware port reuses the same integration test against the BEAM-side dispatch entry.

### 7.4 Migration-window bar

During cutover (BEAM running but pre-canary-100%), failure of any test class halts canary advance.

---

## §8 Shadow-divergence design

Surface D (PG canonical identity) writes to **two coupled tables** on PATH 3 fresh mint: `core.identities` AND `core.agents`. Wave 3 BEAM shadows both during the shadow window.

### 8.1 DDL

Prereq PR #1 ships:

```sql
-- Mirrors core.identities exactly + shadow_write_at
CREATE TABLE core.identities_shadow (
    LIKE core.identities INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Mirrors core.agents exactly + shadow_write_at
CREATE TABLE core.agents_shadow (
    LIKE core.agents INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`LIKE … INCLUDING ALL` pulls indexes, defaults, generated columns (e.g. `core.identities.metadata_tsv`), constraints. Schema drift on `core.identities` or `core.agents` would require either re-running `LIKE` or a paired migration; prereq PR #1 includes `db/postgres/schema_drift_check.sh` that fails CI if either table's shape changes without a corresponding shadow update.

### 8.2 Comparator (full outer join, both tables, three divergence kinds)

Prereq PR #1 ships `scripts/ops/wave-3-shadow-divergence-check.sql`:

```sql
-- core.identities divergence (FULL OUTER JOIN catches missing rows on either side)
WITH ident_compare AS (
    SELECT
        COALESCE(c.agent_id, s.agent_id)                       AS agent_id,
        c.agent_id IS NULL                                      AS canonical_missing,
        s.agent_id IS NULL                                      AS shadow_missing,
        (c.api_key_hash    IS DISTINCT FROM s.api_key_hash)     AS api_key_hash_diff,
        (c.status          IS DISTINCT FROM s.status)           AS status_diff,
        (c.parent_agent_id IS DISTINCT FROM s.parent_agent_id)  AS parent_agent_id_diff,
        (c.spawn_reason    IS DISTINCT FROM s.spawn_reason)     AS spawn_reason_diff,
        (c.metadata        IS DISTINCT FROM s.metadata)         AS metadata_diff,
        (c.provisional_lineage IS DISTINCT FROM s.provisional_lineage) AS provisional_diff,
        (c.confirmed_at    IS DISTINCT FROM s.confirmed_at)     AS confirmed_diff,
        (c.lineage_archived_at IS DISTINCT FROM s.lineage_archived_at) AS lineage_archived_diff
    FROM core.identities c
    FULL OUTER JOIN core.identities_shadow s USING (agent_id)
)
SELECT 'identities' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_hash_diff, status_diff, parent_agent_id_diff, spawn_reason_diff,
       metadata_diff, provisional_diff, confirmed_diff, lineage_archived_diff
FROM ident_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_hash_diff OR status_diff OR parent_agent_id_diff
   OR spawn_reason_diff OR metadata_diff OR provisional_diff
   OR confirmed_diff OR lineage_archived_diff;

-- core.agents divergence (separate query, joined by id)
WITH agent_compare AS (
    SELECT
        COALESCE(c.id, s.id)                                    AS agent_id,
        c.id IS NULL                                             AS canonical_missing,
        s.id IS NULL                                             AS shadow_missing,
        (c.api_key         IS DISTINCT FROM s.api_key)          AS api_key_diff,
        (c.status          IS DISTINCT FROM s.status)           AS status_diff,
        (c.parent_agent_id IS DISTINCT FROM s.parent_agent_id)  AS parent_agent_id_diff,
        (c.label           IS DISTINCT FROM s.label)            AS label_diff,
        (c.spawn_reason    IS DISTINCT FROM s.spawn_reason)     AS spawn_reason_diff,
        (c.thread_id       IS DISTINCT FROM s.thread_id)        AS thread_id_diff,
        (c.thread_position IS DISTINCT FROM s.thread_position)  AS thread_position_diff
    FROM core.agents c
    FULL OUTER JOIN core.agents_shadow s USING (id)
)
SELECT 'agents' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_diff, status_diff, parent_agent_id_diff, label_diff,
       spawn_reason_diff, thread_id_diff, thread_position_diff
FROM agent_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_diff OR status_diff OR parent_agent_id_diff
   OR label_diff OR spawn_reason_diff OR thread_id_diff OR thread_position_diff;
```

`shadow_write_at` is excluded (expected to differ). The query categorizes every divergent row as **canonical_missing** (BEAM wrote, Python didn't), **shadow_missing** (Python wrote, BEAM didn't), or **value_mismatch** (both wrote, values differ). Each non-empty row emits one `coordination_failure.beam_python_boundary.shadow_divergence` event with payload `{table_name, agent_id, kind: "canonical_missing"|"shadow_missing"|"value_mismatch", divergent_columns: [list]}`.

Hourly trigger via `scripts/ops/com.unitares.wave3-shadow-divergence-check.plist` (launchctl).

### 8.3 Load amplification before 7-day clock

Prereq PR #1 also ships `scripts/ops/wave3-shadow-replay.sh`: replays captured production traffic at 2× rate against the shadow path. **The 7-day-zero-divergence clock starts AFTER replay completes with zero events.** No clock-start before replay; no clock-restart on replay alone (it's a precondition, not a refresh).

### 8.4 Event type registration

`src/coordination_events.py` adds:
```python
COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE = "coordination_failure.beam_python_boundary.shadow_divergence"
```
and adds it to `WAVE_0_EVENT_TYPES`. `tests/test_coordination_events.py::test_event_type_constants_match_documented_set` is updated.

---

## §9 Crash-safe saga state machine

The session-resolution saga atomically applies a SYNTHESIS→RESOLVED transition across two agent GenServers + PG. v0.1.x's three-state machine (`reserved` → `applied` → `committed`) couldn't distinguish "PG committed but agents unaware of commit" from "agents applied but PG uncommitted" on crash-restart. v0.2 expands the state machine with a per-agent intermediate state and explicit recovery rules.

### 9.1 DDL

Prereq PR #7 ships `CREATE SCHEMA IF NOT EXISTS coordination` and:

```sql
CREATE TABLE coordination.session_resolution_sagas (
    saga_id                    UUID PRIMARY KEY,
    session_id                 TEXT NOT NULL REFERENCES core.dialectic_sessions(session_id),
    paused_agent_id            TEXT NOT NULL,
    reviewer_agent_id          TEXT NOT NULL,
    state                      TEXT NOT NULL CHECK (state IN (
        'reserved',
        'paused_agent_applied',
        'both_agents_applied',
        'pg_committed',
        'reverting',
        'reverted'
    )),
    resolution_payload_json    JSONB NOT NULL,
    resolution_payload_hash    TEXT  NOT NULL,
    paused_agent_ack_at        TIMESTAMPTZ,
    reviewer_agent_ack_at      TIMESTAMPTZ,
    pg_committed_at            TIMESTAMPTZ,
    reverted_at                TIMESTAMPTZ,
    last_attempt_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_count              INTEGER NOT NULL DEFAULT 0,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, resolution_payload_hash)
);

CREATE INDEX idx_saga_inflight ON coordination.session_resolution_sagas (state, last_attempt_at)
    WHERE state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting');
CREATE INDEX idx_saga_session ON coordination.session_resolution_sagas (session_id);
```

`session_id TEXT` matches live `core.dialectic_sessions.session_id` (verified live schema). `paused_agent_id` / `reviewer_agent_id` TEXT matches live shape.

### 9.2 Forward path

1. **Reserve.** Session GenServer INSERTs saga row with `state='reserved'`. Issues `GenServer.call(:reserve_for_session_resolution, {session_id, saga_id})` to both agent GenServers. Idempotent on `(session_id, saga_id)`.
2. **Apply paused agent.** Both agents ACK reservation → session GenServer issues `GenServer.call(:apply_resolution, {session_id, saga_id, payload, hash})` to **paused agent first**. On ACK, UPDATE saga `state='paused_agent_applied', paused_agent_ack_at=now()`. Idempotent on `(session_id, hash)`.
3. **Apply reviewer agent.** Session GenServer issues same call to reviewer. On ACK, UPDATE saga `state='both_agents_applied', reviewer_agent_ack_at=now()`.
4. **PG commit.** Session GenServer commits `pg_resolve_session` AND UPDATEs saga to `state='pg_committed', pg_committed_at=now()` in a **single PG transaction**. Both row writes are part of the same `BEGIN/COMMIT`.

The two-step apply (paused-first, then reviewer) gives crash recovery a deterministic ordering — `paused_agent_applied` means exactly "paused has applied, reviewer has not yet been asked."

### 9.3 Crash recovery rules

Session GenServer init reads any pending saga rows for its `session_id`:

| Saga state on init | Crash interpretation | Recovery action |
|---------------------|----------------------|------------------|
| `reserved` | Reservation may or may not be live in either agent | Query each agent: `:has_reservation`. If neither has it → UPDATE `state='reverting'`, drop via 9.4. If at least one has it → UPDATE `state='reverting'`, issue compensating revert to all reservation-holders, then 9.4 |
| `paused_agent_applied` | Paused agent applied; reviewer not yet asked; PG uncommitted | Query reviewer: `:has_reservation`. If yes → resume forward at step 3. If no → revert paused agent + drop via 9.4 |
| `both_agents_applied` | Both agents applied; PG uncommitted (crash between agent ACK and PG transaction) | Query each agent: `:has_applied`. If both still applied → re-issue PG commit at step 4 (idempotent at PG layer via the saga UNIQUE constraint + `ON CONFLICT (session_id) DO NOTHING` on the resolution INSERT). If either lost it → enter compensating-revert path (9.4) |
| `pg_committed` | PG row committed, but session GenServer might not have issued a final commit-confirmation message to agents | Re-issue `GenServer.cast(:commit_acknowledged, {session_id, saga_id})` to both agents (idempotent — agents transition `applied → committed` and discard saga state). No new PG write |
| `reverting` | Compensating reverts in progress; crash mid-revert | Re-issue `:revert_reservation` and `:revert_apply` to both agents (idempotent: revert-of-non-existent is a no-op ACK). On both ACK → UPDATE `state='reverted', reverted_at=now()` |
| `reverted` | Terminal | No action; row retained for audit |

### 9.4 Drop / revert path

Compensating revert when forward progress is unsafe: session GenServer issues `GenServer.call(:revert_reservation, {session_id, saga_id})` and (if the saga reached an `applied` state on either agent) `GenServer.call(:revert_apply, {session_id, saga_id})`. Both idempotent. On both ACK, UPDATE `state='reverted'`.

### 9.5 Phantom-read mitigation

Observers reading agent state via `audit.coordination_events` consumers OR `load_session_as_dict` (`session.py:261-342`) MUST treat agent state as in-flight if a non-terminal saga exists for the agent's active session:

```sql
SELECT NOT EXISTS (
    SELECT 1 FROM coordination.session_resolution_sagas
    WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
      AND state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting')
) AS is_stable;
```

Observers that can't accept stale-with-rollback semantics call this gate; observers that can (dashboard read paths) may proceed and re-read on the next polling cycle. **Wave 3 stop sign #8:** any observer surfacing stale `is_stable=true` reads during a `paused_agent_applied` window without checking the gate halts canary advance.

---

## §10 Durable cache coherence

v0.1.x's cache invalidation relied on fire-and-forget Redis pub-sub. Lossy pub-sub plus subscriber reconnect = silent divergence. v0.2 grounds cache coherence in **PG-versioned data** with a **bounded reconciliation loop**; pub-sub is an optimization layer, not the invariant.

### 10.1 Versioned baselines

Prereq PR #8 adds:

```sql
ALTER TABLE core.agent_behavioral_baselines
    ADD COLUMN version BIGINT NOT NULL DEFAULT 0;

CREATE INDEX idx_agent_behavioral_baselines_agent_version
    ON core.agent_behavioral_baselines (agent_id, version DESC);
```

Every baseline write increments `version` in the same PG transaction:

```sql
INSERT INTO core.agent_behavioral_baselines (agent_id, ..., version)
VALUES ($1, ..., COALESCE((SELECT MAX(version) FROM core.agent_behavioral_baselines WHERE agent_id = $1), 0) + 1)
ON CONFLICT (agent_id) DO UPDATE
    SET ... = EXCLUDED. ...,
        version = core.agent_behavioral_baselines.version + 1;
```

Cached readers (BEAM GenServer state, Python `_baseline_cache`) hold `(agent_id, version, baseline)` tuples.

### 10.2 Coherence rules

**On observe (per-agent path):** before using cached baseline for an agent, the cache reader checks `SELECT version FROM core.agent_behavioral_baselines WHERE agent_id = $1`. If returned version > cached → fetch fresh + update cache. The version-check query is cheap (indexed); single round-trip.

**Acceptable optimization:** version-check can be batched/skipped for ≤30 seconds since the last successful check for that agent. After 30s, the next observe forces the version check. This bounds staleness to 30s.

**Bounded reconciliation (the actual invariant):** a periodic task (BEAM GenServer or Python background task — both runtimes do it) every 60s SELECTs `(agent_id, version)` pairs for all currently-cached agents and refreshes any that have advanced. This is the safety net: if pub-sub is down, if version-check-on-observe is skipped, if a subscriber reconnects mid-window — the reconciliation loop catches it within the next 60s.

### 10.3 Pub-sub as optimization layer

Optional Redis pub-sub stays as a latency optimization, **never as source of truth**. After a successful baseline write, the writer publishes `governance:baseline:invalidate` with payload `{agent_id, written_at, source, new_version}`. Subscribers receiving the message can immediately invalidate the cached entry without waiting for the next observe / reconciliation tick. If a message is missed, dropped, or arrives out of order: bounded by 10.2's 60s reconciliation. A `coordination_failure.redis_pubsub_lag` event fires if observed pub-sub lag exceeds 60s for ≥2 consecutive reconciliation cycles (this is structural — it means reconciliation is doing all the work, optimization is dead, operator should investigate).

### 10.4 Feature flags (was Surface H)

Same pattern, applied to identity honesty mode (`identity_strict_mode`, `ipua_pin_check_mode`):

```sql
CREATE TABLE core.feature_flags (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    version     BIGINT NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);

INSERT INTO core.feature_flags (key, value) VALUES
    ('identity_strict_mode', 'false'),
    ('ipua_pin_check_mode',  'enforced');
```

Both runtimes read flag value on startup, cache locally per-process. Version-check on every read is cheap; bounded reconciliation every 60s; pub-sub `governance:feature_flag:invalidate` is optimization. On Redis-down: read directly from PG; on PG-down: last-known-good cached value with a `coordination_failure.feature_flag_db_unavailable` event. Bootstrap: env var as default before first PG read succeeds; once PG reachable, env var becomes init-only.

This explicitly rejects v0.1.x's "Redis is the runtime-mutable source of truth" framing — Redis being down should not lose feature-flag state, and pub-sub being lossy should not silently desynchronize the two runtimes.

### 10.5 Two independent caches: still rejected

v0.1's "two independent caches accepted" posture stays rejected. Both Python's `_baseline_cache` and BEAM's GenServer-state cache converge on PG via version-check + reconciliation. Cost: ~one extra PG version-check query per agent per 30 seconds (heavily indexed; trivial). This is the price to avoid the substrate-tax pattern recurring at the cache layer.

---

## §11 Exit criteria (Go/No-Go for Wave 3 close)

Each criterion names its measurement source. If any source is missing at gate, gate halts (no fallback default).

1. **Wave 2 has closed.** Per Wave 2 handoff 2026-05-08; satisfied.
2. **21-day production traffic on BEAM.** Handler dispatch on BEAM has served production governance MCP traffic for ≥21 days continuous. Source: Wave 0 dashboard.
3. **Zero coordination-class incidents.** `audit.coordination_events` filtered to `coordination_failure.beam_python_boundary.*` shows zero incidents over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
4. **(A.1 / ODE-floor gate)** ODE profile lands BEFORE Wave 3 implementation starts; result shows <60% of `process_agent_update` p99 floor in `governance_core/` math. Failure → halt and roadmap re-opens. Source: prereq PR #1.
5. **(B / boundary-cost gate)** `audit.coordination_measurements` filtered to `measurement.beam_python_boundary.*` shows p50 < lease-plane Phase A measured p50 × 2 AND p99 < lease-plane Phase A measured p99 × 3 over 21-day window. Sustained breach halts. Source: prereq PR #6 (must produce ≥14 days of data before thresholds can be set).
6. **(A.2 / in-place-fix gate)** if any Python in-place fix shipped during implementation window brought `process_agent_update` p99 below 2.0s, **gate fires pre-canary-100%**. No "operator decides post-shipment" escape hatch.
7. **(D / MCP SDK gate)** hands-on spike on `mcp_elixir_sdk` 1.0.1 OR `hermes_mcp` 0.14.1 recorded in `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` before implementation gate; result not "production-disqualifying."
8. **(E / state-ownership gate)** §3 surface-by-surface analysis at gate finds no irreducible per-request semantics beyond the eight surfaces.
9. **(F / opportunity cost gate)** operator's `docs/proposals/wave-3-go-decision-<date>.md` includes §"Calendar reasoning" naming current slip vs original target on each of {paper, fellowship, HLH, R2 Phase 2}; no item slips >25% of original deadline window OR slip is explicitly accepted.
10. **(G / dialectic-quality gate)** session-resolution rate regression ≤5% AND reviewer-reassignment rate increase ≤20% vs baseline. Baseline (mean + σ) computed from trailing 30 days of `core.dialectic_sessions`; pinned in this §11 prior to implementation start (prereq PR #9). Gate halts if baseline volume insufficient.
11. **Operator-led behavioral parity.** Existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff; REST contract preserved per §7.2 byte-equivalence definition.
12. **Test-class green.** ExUnit + Python + integration + golden-response-parity classes all green at gate.

---

## §12 Stop signs

Inheriting parent roadmap stop signs #1–#4, plus Wave-3-specific:

- **#5** Identity-middleware port surfaces a coordination shape Wave 1+2 didn't expose (e.g., contextvar chain holding live object references that don't survive the Port boundary). Halt before canary advance.
- **#6** Dialectic split per §5 turns out ungratified — a function classified as "computation" mutates state across calls. Re-classify, possibly re-split.
- **#7** 503 rate during cutover/rollback exceeding 1% of requests for >60s halts the procedure (per §3.2).
- **#8** Any observer surfaces stale `is_stable=true` reads during a `paused_agent_applied` window without checking the §9.5 gate. Halt canary advance.
- **#9** Bounded reconciliation loop (§10.2) firing more than 5× the steady-state expected rate of cache-mismatch detections — indicates pub-sub layer is dead AND reconciliation is masking it, possibly indicating PG version-counter contention. Halt canary; investigate.

---

## §13 What Wave 3 deliberately does NOT do

- Does not port `governance_core/`. Math stays Python.
- Does not port the MCP transport layer. Stays Python until disconfirmer (D) is run hands-on.
- Does not port the LLM SDK call paths. Anthropic/OpenAI/Ollama call paths inside handlers stay Python, called from BEAM via Ports/HTTP.
- Does not port Watcher. Single-shot LLM pattern matcher; no coordination shape.
- Does not modify the `lease_plane` schema. Wave 3's new state lives in GenServer memory, the new `coordination` schema (§9), shadow tables (§8), or extensions to existing `core.*` tables (§10).
- Does not extend `surface-lease-plane-v0.md` Phase B to `resident:/` unless §4 option (α) is chosen. Default recommendation is option (β) — per-agent PG advisory lock.

---

## §14 Implementation prereq PRs

All nine prereq PRs land BEFORE any commit in `elixir/handler_dispatch/` or any new `elixir/` tree on the implementation branch. CI lint check `scripts/dev/check-wave3-ode-prereq.sh` enforces.

| # | PR | Creates / modifies | Depends on |
|---|-----|---------------------|------------|
| 1 | ODE profile + shadow DDL + comparator + event_type | `db/postgres/migrations/0NN_identities_shadow.sql`, `db/postgres/migrations/0NN_agents_shadow.sql`, `src/coordination_events.py` (shadow_divergence constant), `tests/test_coordination_events.py` (updated set), `scripts/ops/wave-3-shadow-divergence-check.sql`, `scripts/ops/com.unitares.wave3-shadow-divergence-check.plist`, `scripts/ops/wave3-shadow-replay.sh`, ODE profile commit | — |
| 2 | Feature-flag reader (BEAM + Python) + redis-pubsub-lag event | `src/feature_flags.py` (Python reader), `elixir/feature_flags/` (BEAM reader), `coordination_failure.redis_pubsub_lag` event_type registration | #1 (event_type set) |
| 3 | `coordination_events_helpers.py::make_boundary_payload` + Elixir helper + `make_measurement_payload` | `governance_core/coordination_events_helpers.py`, Elixir helper module, CI lint (grep prohibition) | — |
| 4 | IPUA pin integration test | `tests/integration/test_identity_path2_ipua_pin_pipeline.py` | — |
| 5 | Golden-capture fixture + capture script + masking + parity test | `tests/fixtures/wave3_response_golden/` (50+), `scripts/dev/wave3-capture-goldens.sh`, `tests/integration/test_wave_3_response_parity.py` | — |
| 6 | Lease-plane Phase A latency instrumentation + measurement table | `db/postgres/migrations/0NN_audit_coordination_measurements.sql`, `src/lease_plane_client.py` (emit `measurement.lease_plane.request`), `scripts/ops/wave-0-channel-report.sh`. Runs ≥14 days before disconfirmer (B) thresholds set | #3 (`make_measurement_payload`) |
| 7 | Saga DDL + state machine | `db/postgres/migrations/0NN_coordination_session_resolution_sagas.sql` (CREATE SCHEMA + CREATE TABLE), Python interface stubs for tests | — |
| 8 | Versioned baseline + reconciliation loop (Python side) | `db/postgres/migrations/0NN_agent_behavioral_baselines_versioned.sql`, `core.feature_flags` migration, `governance_core/baseline_reconciliation.py`, `phases.py` baseline-write path adds version increment + optional pub-sub | #2 (event_type) |
| 9 | Dialectic baseline pinning artifact | `docs/handoffs/wave-3-dialectic-baseline-<date>.md` with mean + σ for resolution rate and reassignment rate over trailing 30 days from `core.dialectic_sessions`; criterion #10 references this commit | — |

PR #1 lands first (it's the disconfirmer A.1 anchor). PRs 2–9 land in dependency order shown. PR #6 must run ≥14 days before disconfirmer (B) thresholds can be set; if Wave 3 implementation gate is reached before PR #6 has 14 days of data, gate halts (per criterion #5).

Per disconfirmer (F): if these nine + their council passes consume more than (Wave 1 elapsed × 3) calendar-weeks, halt and re-evaluate.

---

## §15 Council pass — pending v0.2

Three lanes scheduled in parallel per `feedback_design-doc-council-review.md` and `feedback_council-adversarial-prompt.md`:

- **dialectic-knowledge-architect** — adversarial on §0's disconfirmer set (does each threshold actually anchor to a measurement source?), the dialectic split's structural rigor (§5), and the bias-discipline framing (does §0 honestly enumerate, or is it ratification?).
- **feature-dev:code-reviewer** — adversarial on §8 comparator (full outer join handles all three divergence kinds correctly?), §9 saga state machine (every crash point recoverable?), §10 versioned-baseline + reconciliation (correct under PG version-counter contention, BEAM GenServer restart, simultaneous Python and BEAM writes?), §6 namespace separation (CHECK constraint on new table prevents misuse?).
- **live-verifier** — adversarial on every named file:line, endpoint, schema column, table, plist, and runtime claim. Cross-checks against running governance-mcp + lease-plane + the audit.events schemas. Specifically: confirm `core.identities` and `core.agents` columns referenced in §8 match live; confirm `core.dialectic_sessions.session_id` is TEXT not UUID; confirm `audit.coordination_events.event_type` CHECK constraint is the regex named in §6; confirm `core.agent_behavioral_baselines` exists and has a writable schema for §10's version column.

If the v0.2 council pass returns BLOCK on any item, the discipline is **not** another amendment fold — v0.3 is the next step. v0.2 is the attempt to write the doc cleanly from third-council findings; if it didn't succeed, the cycle continues.

---

## §16 Open follow-on (not Wave 3 scope)

The substrate-tax bug class is structural to anyio + asyncio + asyncpg / Redis on a shared event loop (per `CLAUDE.md` §"Substrate Tax: anyio-asyncio Coupling"). Wave 3 dissolves it on the Wave 3 surfaces; remaining Python surfaces (governance_core compute, LLM SDK paths, Watcher, MCP transport) still live on the same substrate. Post-Wave-3 measurement decides whether to continue porting or pause.
