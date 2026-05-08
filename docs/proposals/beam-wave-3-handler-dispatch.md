# Wave 3 RFC: handler dispatch + identity middleware + dialectic resolution → BEAM

**Status:** v0.1-draft, 2026-05-08. Pre-council. Author: claude-wave3-rfc (UUID `326aadf6-66d0-4a92-a6e1-255ca8db3cdc`). No code lands until council pass closes.
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` v0.3 / v0.3.1 (operator-decision migration commit + council fold).
**Sibling, completed:** `docs/proposals/beam-wave-1-sentinel.md` (Sentinel-on-BEAM Surface 1+2 shipped, Surface 3 in flight).
**Sibling, completed:** `docs/proposals/surface-lease-plane-v0.md` Phase A + Wave 2 hardening (#412/#414/#417/#418/#419) — boundary contract is firm.
**Wave 0 channel:** `coordination_failure.beam_python_boundary.*` constants exist (#408) but are typed-but-unused; Wave 3 wires them at call sites so exit criterion #3 ("no new substrate-tax pattern at the Python-handler-body boundary") is measurable.
**Operator-protective single-writer surfaces:** Identity / onboarding (per `CLAUDE.md` "Before Starting Work on a Single-Writer Surface") spans this entire RFC. Branch from this RFC's head before any parallel work.

---

## §0 Falsifying-evidence question — and the honest answer

Per `feedback_substrate-migration-status-quo-bias` (the documented author bias — "I reliably resist substrate migrations across sessions") and the symmetric warning in `beam-footprint-roadmap-v0.md` §"Why Read A, not Read B" ("Substrate-migration enthusiasm is the *prompt* to write this roadmap, not *evidence* that justifies escalation"), the question this RFC opens with is:

> **What evidence would update us away from porting handler dispatch + identity middleware + dialectic resolution to BEAM?**

If the answer is "nothing" — if no observable Wave 1/Wave 2 outcome could change the migration call — then the RFC is a ratification, not a decision. The honest discriminators below are what the Wave 3 Go/No-Go gate (§Exit criteria) is built on.

### The five disconfirmers, in descending strength

**(A) The locked-phase floor IS the ODE.** Per auto-memory `project_locked-phase-floor-is-the-ode.md` (2026-05-04): "process_agent_update locked-phase floor is the ODE" — surrounding awaits already cheap; don't swing at them again. If profiling Wave 1's BEAM Sentinel post-cutover, plus an ODE-trace on the still-Python `governance_core/phase_aware.py` and `governance_core/stability.py` math path, shows the per-turn floor is dominated by ODE compute rather than handler-dispatch / identity / dialectic coordination, then porting the layer Wave 3 names doesn't shrink p99. The right answer in that world is "profile and optimize the ODE; leave the handler layer alone." This is the strongest single falsifier because Wave 3 explicitly leaves `governance_core/` Python (per roadmap §"Out of scope for this roadmap"), so a port that doesn't touch the floor cannot move the user-visible metric.

**(B) Wave 0's boundary channel shows BEAM↔Python round-trip cost ≥ substrate tax it removes.** Stop sign #4 (V0.3.1): "Wave 0 instrumentation post-Wave-1 shows Ports/HTTP boundary accruing >1 distinct workaround pattern → boundary design is wrong, halt before Wave 3." Sharper measurable form: if the `coordination_failure.beam_python_boundary.*` channel (Wave 2 #3, wired in this RFC) shows a sustained per-call boundary cost p50 ≥ 50ms or p99 ≥ 250ms over the 14-day Wave 1 window, the port is net-negative — Wave 3 trades a Python coordination tax for a cross-runtime boundary tax of comparable magnitude.

**(C) In-place Python remediation closes the user-visible gap.** Per `project_locked-update-overhead-fix.md`: PR #1 (#362) + PR #2 (#372) shipped 2026-05-05; per-turn overhead ~6.5s → ~5.0s; PR #3 deferred pending benchmark; if still >5s, substrate question strengthens. **Inverse:** if the deferred PR #3 (or any post-Wave-1 in-place Python fix) brings p99 of `process_agent_update` to under a stated threshold (proposed: <1.5s p99 across the Wave 1 window) without porting, the substrate-tax claim weakens to "real but not user-blocking" — which is not enough to motivate Wave 3's blast radius.

**(D) MCP SDK gate reverses on hands-on evaluation.** V0.3.1 §B5 dissolved the gate based on hex.pm presence (`mcp_elixir_sdk` 1.0.1, `hermes_mcp` 0.14.1, plus six others at non-trivial versions). If a hands-on spike on either of the named SDKs shows broken-on-Anthropic-streaming, MCP-spec drift, no maintainer responsiveness, or other production-disqualifying failure, the gate re-closes. In that world Wave 3 must keep the MCP transport Python — which means a Python→BEAM→Python sandwich for every request, with two boundary crossings per call instead of one. That doubles disconfirmer (B)'s threshold and likely flips it negative.

**(E) State-ownership cutover is structurally unsafe.** If the identity-middleware survey (§3 below) finds that any identity-binding state has irreducible per-request semantics that can't be moved to GenServer state without re-creating the same coordination problem at the boundary — e.g., a contextvar that holds a live `asyncio.Event` whose synchronization is needed across the boundary — then Wave 3 needs a more invasive port than estimated, and the boundary cost from disconfirmer (B) gets a structural component on top of the network component.

### What the answer is NOT

- Wave 1 simply "shipping without incident" is **not** confirmation. Wave 1's exit criterion is 14 days × zero coordination-class incidents AND ODE profile completed before exit-criteria authorship (V0.3.1 §C1). A clean Wave 1 with bad boundary numbers is disconfirmer (B).
- "BEAM is the right substrate philosophically" is **not** evidence. It's a prior. Per the symmetric bias warning, enthusiasm and resistance are both errors.
- Operator preference is **not** evidence. V0.3 closed the *destination* on operator decision; the *Wave 3 Go gate* is a separate, evidence-bearing call.

### How this RFC handles disconfirmation

§"Exit criteria" below makes Wave 3's go-decision conditional on the disconfirmers above being measured-and-not-triggered, not on the calendar. If disconfirmer (A), (B), (D), or (E) is present at the gate, Wave 3 halts and the roadmap re-opens.

---

## §1 Roadmap-level scope (inherited from beam-footprint-roadmap-v0.md Wave 3)

Verbatim from parent doc §"Wave 3":

- Handler dispatch (the `@mcp_tool` decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per §"MCP SDK gate") and proxies to BEAM after request unmarshalling.
- Identity middleware (`src/mcp_handlers/middleware/identity_step.py`, the session-context contextvar chain, agent_id resolution, label resolution) ports to BEAM. This is the largest single coordination surface in governance MCP today and the highest-leverage substrate-tax elimination.
- Dialectic resolution (`src/mcp_handlers/dialectic/`) ports to BEAM. The dialectic engine's *reasoning logic* — what makes a thesis converge, the dialectic-knowledge-architect's substantive work — stays Python (it's compute, not coordination) and is called from BEAM. The coordination layer (session lifecycle, quorum tracking, condition resolution, audit emission) ports.
- Out of scope: `governance_core/`, Watcher, the LLM SDK call paths inside handlers (those stay Python and are called from BEAM via Ports).

§5 below splits dialectic explicitly per V0.3.1 §C2.

---

## §2 Lock-invariant inventory (per V0.3.1 §B2)

The lock surface is `StateLockManager.acquire_agent_lock_async` (`src/state_locking.py:286-423`), a per-agent file-based lock that brackets the `execute_locked_update` phase chain in `src/mcp_handlers/updates/phases.py`. Eleven invariants identified, three named in V0.3.1 §B2 (kept for traceability) plus eight folded from the survey.

For each: file:line / classification / Wave 3 GenServer mapping decision (**internal-message** = synchronous step inside the agent's GenServer mailbox; **explicit-relax** = inherit PR #362-style eventual consistency with named tolerant consumer; **PG-anchored** = explicit lock at DB layer, GenServer just serializes access).

| # | Invariant | File:line | Classification | Wave 3 mapping (proposed; council reviews) |
|---|-----------|-----------|----------------|---------------------------------------------|
| 1 | api_key PG/cache reconciliation: PG-create succeeds → cache.api_key syncs to PG; PG-create fails → cache is truth | `phases.py:723-798` (esp. 745, 778, 792, 798; comment naming at 773-776) | Critical read-then-write-then-validate under lock; three-way (UUID, api_key, cache) | **internal-message** — must stay atomic inside agent GenServer; api_key auth desync risk if relaxed |
| 2 | thread_id / node_index monotonic advancement on `active_session_key` change | `phases.py:822-851` (relaxation comment 834-837; persist helper 670-707) | Read-modify-write under lock; PG fire-and-forget post-PR #362 | **explicit-relax** — inherit PR #362 posture; in-memory `ctx.meta` is process-local truth, PG is cross-process replica. Document the tolerant consumers (cross-process thread-lineage observers). |
| 3 | previous_void_active snapshot: read-once inside lock before ODE, used post-lock for CIRS emission decision | `phases.py:800-807` capture; `phases.py:1125-1137` use | Atomic snapshot-capture under lock; out-of-lock guard | **internal-message** — must remain a single mailbox message; do NOT re-read post-ODE |
| 4 | Monitor lifecycle consistency: metadata fetched (line 743/768/789) and monitor lookup (line 803) must refer to the same agent under one lock acquire | `phases.py:743-798, 803-807, 880-923` | Cache-coherence assumption (in-memory dict lookups) | **internal-message** — corollary of (1); BEAM must keep meta+monitor reads in the same handler frame |
| 5 | Dialectic session lock exclusion: SYNTHESIS→RESOLVED phase transition must serialize across two `submit_synthesis(agrees=True)` calls or both finalize_resolution calls race; second `pg_resolve_session` overwrites the first | `dialectic/handlers.py:1179-1190` (named comment) | Lock-protected critical section; CROSS-AGENT (not per-agent-state) | **internal-message** at the *session* GenServer (not the agent GenServer); requires session-keyed routing in dispatch — see §5 |
| 6 | Baseline preload: `get_baseline_or_none(agent_id)` loads once per process (lines 812, 817); cached in-process; no cross-process refresh | `phases.py:809-820, 856-899` | In-process single-writer cache; no lock | **PG-anchored** — read on miss; **decision required:** if BEAM runs N agents per process, validate that baseline cache is per-agent-keyed (not per-process-keyed), or move to PG-on-every-read |
| 7 | Monitor state snapshot for enrichment vs Phase 5 anomaly drift: pre-ODE snapshot (596-602) used for ODE input; post-ODE re-read (1143-1147) used for CIRS emission; the two must NOT cross-contaminate | `phases.py:536-602, 1143-1147, 1156-1164, 1203-1223` | Read-snapshot-before-mutate; post-mutation re-read isolation | **internal-message** — single GenServer call carries both snapshots; BEAM must not split into two messages |
| 8 | Metadata cache-PG eventual consistency contract (corollary of 2 + thread_id persistence): in-memory writer within lock, PG replica written out-of-band; in-memory NEVER rolled back to match stale PG | `phases.py:823-851, 928-943, 670-707` (named in 834-837 + persist docstring) | Single-writer-then-broadcast | **explicit-relax** — formalize as cross-layer contract; document as Wave 3 design rule, not just a phases.py local choice |
| 9 | api_key mutable reference under lock (corollary of 1): `ctx.meta.api_key` mutations (745, 778, 792, 798) must complete before ODE call (905) which receives `api_key` param | `phases.py:745, 778, 792, 798, 905-911` | Mutable reference coherence during lock hold | **internal-message** — covered by (1)'s framing; flag here for completeness |
| 10 | CIRS void_active transition guard (corollary of 3): post-ODE void state vs pre-ODE captured snapshot determines emission; comparison MUST use captured value, not re-read | `phases.py:800-807, 1125-1137` | Captured-state guard for out-of-lock decision | **internal-message** — covered by (3); flag for completeness |
| 11 | Agent-state mutation ordering: agent_state immutable under lock (read-only for ODE input); result immutable post-ODE (outcome events read but don't mutate) | `phases.py:635-668, 709-920, 1010-1240` | Read-only vs write-once isolation across phases | **architectural-pattern** — Wave 3 GenServer message handler must enforce this by structure, not by lock |

**Decisions inheriting forward:** invariants 1, 3, 4, 5, 7, 9, 10 collapse into "must be a single GenServer mailbox message handler on the BEAM side." Invariants 2, 6, 8 are explicit-relax (with documented consumers). Invariant 11 is structural — the BEAM message handler's pure-functional shape preserves it for free if the dispatch is single-message-per-update.

**Open question for council:** invariant 5 (dialectic session lock) is cross-agent. Wave 3's GenServer topology must include a *session-keyed* GenServer (one per active dialectic session) above the per-agent GenServers. This is named in §5 below; the lock-invariant inventory surfaces it here.

---

## §3 State ownership and rollback during transition (per V0.3.1 §B3 + §B4)

> *Survey in progress (Explore agent). Filled below when complete.*

Per V0.3.1 §B3, every Wave RFC must cover, for each migrating surface:

- **Single source of truth per state surface** during the transition window (Python-side, BEAM-side, or shadow-mode dual-write with one canonical reader).
- **Cutover semantics** — direct flip / shadow-mode-then-flip / dual-write-then-converge. Default presumption: shadow mode for ≥1 cycle of meaningful traffic before flip.
- **State format compatibility** — any on-disk or shared-DB schema MUST be backwards-compatible with the Python reader OR a documented migration shim is provided. Default: BEAM does NOT modify the Python-readable format until Wave-N+1 explicitly changes the canonical reader.
- **Rollback procedure** — named launchctl/systemd command sequence, state-file restoration step, and explicit acknowledgement of which side keeps writing during the rollback window.

### 3.1 Surface inventory (state-ownership matrix)

Identity middleware decomposes into eight state surfaces. Source-cited columns from `src/mcp_handlers/middleware/identity_step.py`, `src/mcp_handlers/identity/{resolution,persistence,session}.py`, `src/mcp_handlers/support/agent_auth.py`, `src/mcp_handlers/context.py`, and `src/background_tasks.py`.

| # | Surface | File:line (read) | File:line (write) | Single source of truth | Lock posture | BEAM port strategy | Cutover semantics |
|---|---------|-------------------|---------------------|------------------------|---------------|----------------------|---------------------|
| A | ContextVars (10 declarations; 4 identity-bearing — `_session_context`, `_mcp_session_id`, `_session_resolution_source`, `_pin_match_scope`) | `context.py:131-147` (`get_context_*`) | `context.py:86-114` (`set_session_context`, `update_context_agent_id`) | Process memory only (async-task-local) | None — request-scoped, never contended | **Stays Python (per-handler-task-local)** at the dispatch boundary. BEAM message handler threads request-context explicitly through GenServer state. ContextVars never cross the boundary. | **Direct flip** — ContextVars are ephemeral. BEAM owns identity-context at message-handler entry; Python's ContextVar layer sits above the BEAM call boundary in the still-Python MCP transport |
| B | Sticky transport binding cache (3-layer: in-memory dict / Redis / PG fallback) | `identity_step.py:289-298` (cache hit), `:292` (Redis recovery, 0.5s timeout) | `identity_step.py:98-157` (`update_transport_binding` + fire-and-forget Redis), `:230-248` (invalidate) | In-memory dict when populated; Redis when recovered; no PG anchor | Fire-and-forget to Redis; in-memory dict mutation under no lock | **BEAM owns** as per-process GenServer state (or stays Python — both work). Pure optimization layer. | **No shadow needed** — drop in-memory cache → next request falls through to Redis → falls through to session resolution. Zero data risk |
| C | Session→UUID Redis cache (`sticky:{ip_ua_fingerprint}:{mcp_session_id}` keys) | `resolution.py:430-470` (PATH 1 Redis lookup) | `persistence.py:175-200` (`_cache_session` Redis SETEX with `NX`) | PostgreSQL canonical; Redis is speed cache | NX flag on Redis writes (idempotent); 2h TTL | **Shadow-mode-then-flip.** Python writes both Redis + PG during warmup; BEAM reads. After ≥1 cycle of meaningful traffic, BEAM writes both, Python reads via context/HTTP fallback. | **Shadow ≥1 cycle then flip.** Rollback: re-enable Python writes, BEAM HTTP-read-only. ≤1-request consistency window at flip moment |
| D | Session→UUID PG canonical (`core.identities` + `core.agents` upsert on PATH 3 fresh mint) | `resolution.py:667-1088` (PATH 3) | `resolution.py` (`db.upsert_identity`, `db.upsert_agent`) | PostgreSQL — authoritative on fresh mint | `ON CONFLICT` clause (last-writer-wins at PG layer); in-memory `_session_identities` dict with S21-a `mint_guard=True` collision guard | **BEAM owns the upsert.** PG INSERT/UPDATE moves into GenServer message atomicity. Python REST-reads via boundary call on cache miss. | **Shadow ≥1 cycle then flip then dual-write window then BEAM-only.** Three-stage. Rollback: re-enable Python upsert, BEAM read-only. PG ON CONFLICT absorbs flip-moment race |
| E | Continuity token (cryptographic; HMAC over agent_uuid + chh + exp + iat + sid + opv) | `session.py:176-220` (extraction; no I/O) | `session.py` (`create_continuity_token` at onboard) | Cryptographic material — token string IS the source | None — stateless | **Stays Python OR moves to BEAM** — orthogonal substrates. Tokens issued by either are valid on both | **No rollback contract** — orthogonal credential layer |
| F | Onboard PIN (Redis-keyed `onboard_pin:{ip_ua_fingerprint}` with model scoping; IPUA pin-check enforces agent_id-as-proof per `project_ipua-pin-agent-id-proof.md`) | `session.py:440-530` (Redis lookup, 0.5s timeout) | `session.py` (`set_onboard_pin` SETEX, 30m TTL) | Redis (TTL 30m); IPUA pin treats `agent_id` claim as proof — invariant locked by contract test per memory | 0.5s read timeout (anyio mitigation) | **Shadow ≥1 cycle then flip.** Same pattern as (C). Validation logic mirrors per-runtime; the IPUA pin invariant CANNOT be relaxed without contract-test breakage. | **Shadow then flip.** PIN write moves to BEAM; PIN validation can stay either side |
| G | Agent metadata cache (`mcp_server.agent_metadata[uuid]` — label, public_agent_id, status, paused_at) | `agent_auth.py:59-134` (`compute_agent_signature`), `:151` (status check), `:309-377` (`require_registered_agent` label→UUID iteration) | `background_tasks.py` (`load_agent_metadata` broadcast → background load from PG) | PostgreSQL `core.agents` is canonical; in-memory dict is read-side cache | Fire-and-forget background loader; no explicit lock; status check accepts stale (advisory not fail-closed) | **Boundary service.** PG-anchored. OTP gen_server watches PG for changes and publishes; both BEAM + Python subscribe via the same broadcast channel | **No rollback contract** — read-mostly, stale reads degrade gracefully. Both sides can subscribe in parallel |
| H | Identity honesty gates (PATH 0 bare-UUID-passthrough strict-mode + FALLBACK 2 handler auto-generation) | `identity_step.py:365-474`, `agent_auth.py:271-293` | Config env var (`identity_strict_mode()`, `ipua_pin_check_mode()`); broadcast `identity_hijack_suspected` event | Config (env var) — no state surface | None — config-driven | **BEAM mirrors config check** at the same dispatch entry point. Broadcast event channel stays Python until OTP event-bus integration is decided (out of Wave 3 scope) | **Direct flip** — config-only. Env var change applies to both sides at restart |

### 3.2 Rollback procedure (named)

Following the pattern proven by Wave 1 (Sentinel had `.sentinel_state.pre-beam-*` snapshot files; runtime checkout per landmine #1 in the Wave 2 handoff):

1. **Snapshot before flip.** For every PG table touched by the Wave 3 BEAM service, `pg_dump` the table-set (at minimum `core.identities`, `core.agents`, `core.dialectic_sessions`, `core.dialectic_messages`) into `~/backups/governance/wave-3-pre-cutover-<ISO8601>/`.
2. **Plist swap.** New plist `com.unitares.handler-dispatch-beam.plist` lives in `scripts/ops/`. Cutover flips the BEAM service on; rollback unloads the BEAM plist and reloads the Python-only `com.unitares.governance-mcp.plist`.
3. **Single writer during rollback.** Per-surface protocol from §3.1 columns: stop BEAM writes first, then restore Python writers. No period of dual-write to the same canonical surface during rollback.
4. **Schema rollback.** Any new migration shipped with Wave 3 MUST have a paired DOWN migration that restores the prior shape; tested on a `governance_test` snapshot before the cutover migration runs in production.
5. **Per-surface rollback windows** (from matrix above):
    - Surfaces A, E, F (config + crypto): instantaneous; no data window
    - Surfaces B, C, G (caches): ≤2h staleness window (TTL); zero data risk
    - Surface D (PG canonical): ≤1-request inconsistency window at flip moment; ON CONFLICT absorbs
    - Surface H (config gates): instantaneous on env-var revert

### 3.2 Rollback procedure (named)

Following the pattern proven by Wave 1 (Sentinel had `.sentinel_state.pre-beam-*` snapshot files; runtime checkout per landmine #1 in the Wave 2 handoff):

1. **Snapshot before flip.** For every PG table touched by the Wave 3 BEAM service, `pg_dump` the table-set into `~/backups/governance/wave-3-pre-cutover-<ISO8601>/`.
2. **Plist swap.** New plist `com.unitares.handler-dispatch-beam.plist` lives in `scripts/ops/`. Cutover flips the BEAM service on; rollback unloads the BEAM plist and reloads the Python-only `com.unitares.governance-mcp.plist`.
3. **Single writer during rollback.** During the rollback window the BEAM service is stopped first, then the Python service is restored — no period of dual-write to the same canonical surface.
4. **Schema rollback.** Any new migration shipped with Wave 3 MUST have a paired DOWN migration that restores the prior shape; tested on a `governance_test` snapshot before the cutover migration runs in production.

---

## §4 `resident:/` Phase B enforcement gate (per V0.3.1 §C3)

V0.3.1 §C3 stated: lease plane Phase A is advisory-only; Wave 3 handler dispatch requires Python MCP to stop accepting writes for an agent while its BEAM GenServer is mid-update. That's Phase B enforcement. **Phase B eligibility for `dialectic:/` opens 2026-05-16** per lease plane RFC; **no Phase B window is named for `resident:/` surfaces.**

Wave 3 either:

- **(α)** Opens a `resident:/` Phase B window via amendment to `surface-lease-plane-v0.md`, or
- **(β)** Specifies a different enforcement-grade boundary mechanism (e.g., per-agent advisory lock at PG layer, taken with `pg_try_advisory_lock(hashtext(agent_uuid))` at the start of any writing handler — fails fast if BEAM holds the lock).

**Recommendation pending council:** option (β). The lease plane was greenfield; the `resident:/` surface has live Python writers across `agents/sentinel/`, `agents/vigil/`, `agents/chronicler/`, and the in-process Steward. Opening a Phase B window forces every Python resident to learn fail-closed-on-deny semantics in the same window the BEAM cutover happens, which couples two large changes. Option (β) keeps the lease plane unchanged and adds a per-agent PG advisory lock that BEAM acquires on enter and releases on exit; Python writers attempt the same lock with a 50ms timeout and fail-fast (returning a 503-equivalent that the MCP transport surfaces as `governance_temporarily_unavailable`).

Decision deferred to council pass.

---

## §5 Dialectic stateful/stateless split (per V0.3.1 §C2)

The architect council's finding: dialectic is plausibly BOTH stateful-coordinating (resolution timing, participant lifecycle) AND stateless-computing (numerical synthesis math — `src/dialectic_protocol.py:162` imports numpy). This RFC splits it explicitly.

### 5.1 Coordination surfaces → BEAM GenServer

Session lifecycle, participant binding (paused_agent_id ↔ reviewer_agent_id), phase FSM transitions, lock-protected critical sections, audit emission. All port to a *session-keyed* GenServer (one per active session) that supervises the per-agent message handlers for invariant 5 (§2 lock inventory).

| File:line | Function | Why coordination |
|-----------|----------|-------------------|
| `dialectic_protocol.py:464-524` | `DialecticSession.__init__` | Session lifecycle init; phase setup; timeout constants per session_type |
| `dialectic_protocol.py:526-552` | `submit_thesis` | THESIS→ANTITHESIS transition; auth check (only paused agent); state lock point |
| `dialectic_protocol.py:554-585` | `submit_antithesis` | Reviewer auto-assign if none set; ANTITHESIS→SYNTHESIS transition; reviewer role lock |
| `dialectic_protocol.py:587-638` | `submit_synthesis` | Convergence check (`agrees=True`→RESOLVED); synthesis_round counter mutation; multi-participant coordination |
| `dialectic_protocol.py:781-897` | `finalize_resolution` | Resolution lifecycle closure; dual-signature canonical-payload-v2 coordination |
| `mcp_handlers/dialectic/handlers.py:55-63` | `_resolve_dialectic_agent_id` | Session ownership verification; auth boundary |
| `mcp_handlers/dialectic/handlers.py:130-177` | `check_reviewer_stuck` | Circuit-breaker timeout (2h antithesis); session state validity; phase-gated |
| `mcp_handlers/dialectic/handlers.py:241-334` | `_build_dialectic_actionability` | Actionability state-machine assembly; next-valid moves per phase |
| `mcp_handlers/dialectic/handlers.py:335-412` | `_apply_reviewer_reassignment` | Session state mutation under stuck-session recovery |
| `mcp_handlers/dialectic/handlers.py:414-635` | `handle_request_dialectic_review` | Session creation; PostgreSQL write (`pg_create_session` line 478) |
| `mcp_handlers/dialectic/handlers.py:897-985` | `handle_submit_thesis` | PG write (`pg_add_message` 910); phase transition (922); session lock |
| `mcp_handlers/dialectic/handlers.py:986-1147` | `handle_submit_antithesis` | Reviewer assignment if missing (1040); phase transition (1056); session lock |
| `mcp_handlers/dialectic/handlers.py:1148-1388` | `handle_submit_synthesis` | Convergence check (1206-1228); synthesis_round multi-round (1181); session lock — **invariant 5 critical section** |
| `mcp_handlers/dialectic/handlers.py:1389-1506` | `handle_reassign_reviewer` | Session update (`pg_update_reviewer` 1460) |
| `mcp_handlers/dialectic/resolution.py:18-196` | `execute_resolution` | Agent state mutation (status→active, paused_at=None at 74-75); condition application sequencing |
| `mcp_handlers/dialectic/auto_resolve.py:54-220` | `auto_resolve_stuck_sessions` | Periodic stuck-session detection; reviewer reassignment; status mutation (`awaiting_facilitation`) |
| `mcp_handlers/dialectic/reviewer.py:121-200, 255+` | `is_agent_in_active_session`, `select_reviewer` | Quorum-prevention via participant tracking; collusion gate via state reads |

### 5.2 Computation surfaces → stays Python, called from BEAM via boundary

Pure functions: signature math, similarity scoring, safety regex, condition merging. No state mutation, no lock, no I/O.

| File:line | Function | Why computation |
|-----------|----------|------------------|
| `dialectic_protocol.py:1077-1162` | `calculate_authority_score` | numpy sigmoid health-score, Jaccard similarity, weighted authority aggregation; pure function |
| `dialectic_protocol.py:640-657` | `_normalize_condition_terms`, `_semantic_similarity_terms` | Term extraction + Jaccard; pure |
| `dialectic_protocol.py:659-743` | `_merge_proposals` | Condition semantic matching (0.6 threshold); intelligent merge via term overlap; pure |
| `dialectic_protocol.py:746-779` | `_conditions_conflict` | Contradiction detection via regex + term-overlap heuristics; pure predicate |
| `dialectic_protocol.py:250-265` | `DialecticMessage.sign` | HMAC-SHA256; deterministic |
| `dialectic_protocol.py:350-410` | `Resolution.compute_signature`, `verify_signatures` | HMAC-SHA256 keyed MAC + `hmac.compare_digest`; pure crypto |
| `dialectic_protocol.py:899-986` | `check_hard_limits` | Safety regex validation + threshold checks on risk/coherence; stateless predicate |
| `mcp_handlers/dialectic/handlers.py:180-200` | `_read_proposed_conditions` | Fallback alias handling; pure input normalization |
| `mcp_handlers/dialectic/calibration.py` (imported 99-102) | calibration updates from session outcomes | Statistical correlation without lock; numeric aggregation |
| `mcp_handlers/support/condition_parser.py` (imported in resolution.py:13) | condition parsing/application | Numeric/text transformation; stateless |

### 5.3 Mixed/boundary cases — RFC author judgments

| File:line | Function | Judgment | Reason |
|-----------|----------|----------|--------|
| `dialectic_protocol.py:995-1031` | `check_timeout` | **SPLIT**: coordination wrapper (reads phase + session.created_at; gates FSM) calls a stateless `_compare_against_timeout(now, created_at, phase, timeout_constants)` predicate. Wrapper ports to BEAM; predicate stays Python. | Time-comparison itself is pure; FSM-phase decision is coordination |
| `mcp_handlers/dialectic/reviewer.py:55-119` | `_has_recently_reviewed` | **KEEP TOGETHER as coordination**, called from BEAM. PG query is the load-bearing part; collusion-prevention is quorum coordination. | Splitting saves nothing; PG round-trip is the cost |
| `mcp_handlers/dialectic/auto_resolve.py:32-51` | `_parse_timestamp` | **Stays Python utility** (helper to coordination caller) | Pure helper; not worth boundary cost |
| `dialectic_protocol.py:318-329` | `Resolution.hash` | **Stays Python utility** (called from coordination) | Cryptographic hash; pure |
| `dialectic_protocol.py:331-347` | `Resolution.canonical_payload` | **Stays Python utility** (called from coordination — load-bearing for v2 signing per C2026-05-06 NEW-2) | Pure data serialization; substrate-agnostic |
| `calculate_authority_score` (reviewer selection math) | (per §5.2 already classified as computation) | **Stays Python, called from BEAM as `/v1/dialectic/select_reviewer`** during session creation | Pure compute; no shared state; same shape as `/v1/dialectic/synthesize` |

### 5.4 Storage surfaces (unchanged by Wave 3 — Wave 3 inherits)

- `core.dialectic_sessions` (sessions FSM table; `phase`, `status`, `paused_agent_id`, `reviewer_agent_id`, `quorum_*` reserved-but-unimplemented fields). Wave 3 BEAM session-keyed GenServer reads/writes via boundary; on-disk schema unchanged.
- `core.dialectic_messages` (append-only message history; `message_type` ∈ thesis/antithesis/synthesis/system/quorum_vote/failed). BEAM appends via boundary; schema unchanged.
- `audit.coordination_events` (referenced `src/coordination_events.py:35`; not yet wired for dialectic state transitions). **Wave 3 wires dialectic state transitions to this table** as part of §6 boundary-event instrumentation.

### 5.5 Lifecycle FSM (unchanged shape; preserved in BEAM port)

States from `DialecticPhase` enum (`dialectic_protocol.py:166-182`) and `dialectic_sessions` CHECK constraint:

```
THESIS → submit_thesis() → ANTITHESIS
ANTITHESIS → submit_antithesis() → SYNTHESIS (round 1)
SYNTHESIS → submit_synthesis():
    agrees=True → RESOLVED (terminal)
    agrees=False AND round < max → SYNTHESIS (round N+1)
    round ≥ max → FAILED (terminal)
ANTITHESIS (if check_reviewer_stuck) → auto_resolve → FAILED OR new ANTITHESIS (reassigned reviewer)
ESCALATED — reserved (quorum_voting); not implemented; out of Wave 3 scope
```

Phase-enforcement guards (lines 535-536, 569-570, 601-602) prevent out-of-order submissions. Wave 3 GenServer must reproduce these guards as message-handler preconditions, not as wrapping locks.

### 5.6 Boundary protocol for dialectic compute calls

For functions classified as **computation** (§5.2 + §5.3), the BEAM coordination layer calls them via the same Ports/HTTP boundary the lease plane established. Two new Python-side endpoints:

- `POST /v1/dialectic/synthesize` — input: bounded compute (proposals, conditions, threshold); output: merged result; no PG side-effect.
- `POST /v1/dialectic/select_reviewer` — input: candidate pool + paused-agent context; output: ranked candidates with authority scores; no PG side-effect.

Both endpoints are wrapped in the standard `coordination_failure.beam_python_boundary.beam_to_python_request_failed` instrumentation per §6.

---

## §6 `coordination_failure.beam_python_boundary.*` call-site wire-up (Wave 3 measurability)

Per Wave 2 #3 (PR #408): the typed event constants `python_to_beam_request_failed` and `beam_to_python_request_failed` exist with documented payload shape `{endpoint, method, error_class, status_code, elapsed_ms}` but are unused. Wave 3 wires them at every call site that crosses the boundary, so exit criterion #3 is measurable.

### 6.1 Call-site enumeration (Wave 3 introduces these)

- BEAM handler-dispatch service → Python MCP transport (response shaping post-handler-execution): `beam_to_python_request_failed` on any non-2xx return from the Python transport, `python_to_beam_request_failed` on any non-2xx return from BEAM.
- BEAM identity middleware → Python `governance_core/` math calls (when an identity decision needs ODE input): `beam_to_python_request_failed` on Port/HTTP failure.
- BEAM dialectic GenServer → Python `/v1/dialectic/synthesize` (per §5.1): `beam_to_python_request_failed` on synthesize failure.
- BEAM handler-dispatch service → Python LLM SDK call paths (per Wave 3 out-of-scope: LLM SDK stays Python, called from BEAM): both directions instrumented.

### 6.2 Emission contract

Every emission MUST populate all five payload fields. Empty/null `error_class` is itself a lint failure (this is what made the existing `coordination_failure.*` events useful to grep). Reviewer check during PRs: `python_to_beam_request_failed` and `beam_to_python_request_failed` must appear in audit-events tests with concrete payloads, not just `mock.call(event_type=...)`.

### 6.3 Wave 0 query

A new query lands in `scripts/ops/wave-0-channel-report.sh` (or wherever the Wave 0 dashboard sources from) returning, over a stated window: count, p50 elapsed_ms, p99 elapsed_ms, error_class breakdown, by endpoint. This query is what disconfirmer (B) (§0) reads against.

---

## §7 Test strategy under migration (per V0.3.1 §C4)

V0.3.1 §C4 said: 8329-test Python suite cannot cover BEAM-side code or the cross-runtime boundary. Wave 1 added an ExUnit suite for BEAM Sentinel; Wave 3 extends this.

### 7.1 Acceptance test classes

**(a) Existing Python suite.** All ~8400+ tests in `tests/` remain the Python-side acceptance gate. Pre-cutover gate: full green.

**(b) ExUnit suite for BEAM handler-dispatch.** New `elixir/handler_dispatch/test/` (or whatever the project layout settles on). Tests at minimum:
- Driven test: fixture MCP-style request → BEAM dispatch → assert correct Python handler is invoked with correctly-marshalled args.
- Identity middleware test: fixture process_agent_update with parent_agent_id → assert lineage declaration writes to PG with the correct shape (matches what `src/mcp_handlers/middleware/identity_step.py` produces today).
- Dialectic GenServer test: fixture session lifecycle (create → join → quorum → resolve) → assert the same audit.events row sequence Python produces today.

**(c) Cross-runtime integration test.** A new `tests/integration/test_wave_3_boundary.py` (Python side) that drives the full pipeline: MCP request → Python transport → BEAM dispatch → Python compute (governance_core) → BEAM coordination → Python audit emit. Asserts response shape byte-identical to pre-Wave-3 Python-only path. This is the Wave 3 byte-for-byte parity gate from the parent roadmap.

**(d) Behavioral parity gate.** Per parent roadmap exit criterion #4: "Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical)." Operator-led, not just CI; this is the cutover-day check.

### 7.2 What the Python suite stays the gate for

The Python suite stays canonical for: governance_core math, LLM SDK call paths, watcher pattern matching, all "compute" surfaces. The BEAM ExUnit suite is canonical for: handler dispatch routing, identity middleware coordination, dialectic GenServer state transitions. The integration suite is canonical for the boundary itself.

### 7.3 Migration-window test bar

During the cutover window (BEAM service running but pre-canary-100%), failure of any test class halts the canary advance. Specifically:
- (a) green AND (b) green AND (c) green → canary advances per schedule.
- Any single failure → canary stops, root cause identified, fix lands as a separate PR with its own council pass.

---

## §8 Exit criteria (Go/No-Go for Wave 3 close)

Inherited from parent roadmap §"Wave 3 — Exit criterion" + amended for measurability against §0 disconfirmers:

1. Wave 2 has closed (its exit criteria all hold; per Wave 2 handoff 2026-05-08, this is satisfied).
2. Handler dispatch on BEAM has served production governance MCP traffic for ≥ 21 days continuous (longer window than prior waves because this is the largest blast-radius port).
3. Wave 0 channel shows zero coordination-class incidents attributable to handler dispatch over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
4. **Disconfirmer (A) check:** ODE profile data, gathered before Wave 3 close, shows the per-turn floor is not dominated by `governance_core/` math alone. If it is, Wave 3 closes as a structural success but operator-acknowledged user-visible-metric miss; roadmap re-opens.
5. **Disconfirmer (B) check:** `coordination_failure.beam_python_boundary.*` channel shows p50 boundary cost < 50ms and p99 < 250ms over the 21-day window. Sustained breach halts.
6. **Disconfirmer (C) check:** if the deferred locked_update PR #3 (or any other in-place Python fix) shipped during the Wave 3 implementation window and brought p99 of `process_agent_update` to <1.5s without porting, the operator decides whether Wave 3 still closes (port already shipped) or whether the next port should be reconsidered.
7. Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical).
8. ExUnit + Python + integration test classes (§7.1) all green at gate.

---

## §9 Stop signs (additive to parent roadmap §Stop signs)

Inheriting parent roadmap stop signs #1–#4, plus Wave-3-specific:

**Wave 3 stop sign #5:** Identity-middleware port surfaces a coordination shape that Wave 1+2 didn't expose — e.g., the contextvar chain holding live object references that don't survive the Port boundary cleanly. Halt before canary advance; reopen architecture before continuing.

**Wave 3 stop sign #6:** Dialectic split per §5 turns out to be ungratified — a function classified as "computation" mutates state across calls (a hidden statefulness). Re-classify, possibly re-split, before canary advance.

**Wave 3 stop sign #7:** `resident:/` Phase B enforcement (option α or β per §4) blocks legitimate Python writers (Sentinel, Vigil, Chronicler, Steward) at non-trivial rate during the canary window. Halt; revisit the boundary mechanism.

---

## §10 What Wave 3 deliberately does NOT do

- Does not port `governance_core/`. Math stays Python.
- Does not port the MCP transport layer. Per §"MCP SDK gate" — even with V0.3.1 §B5's hex.pm reality, transport stays Python until disconfirmer (D) is run hands-on.
- Does not port the LLM SDK call paths. Anthropic/OpenAI/Ollama call paths inside handlers stay Python, called from BEAM via Ports.
- Does not port Watcher. Single-shot LLM pattern matcher; no coordination shape.
- Does not modify the existing `lease_plane` schema. Wave 3's new state lives in either GenServer memory or new tables (`coordination` schema is reserved for future use; Wave 3 default is GenServer memory + existing PG tables).

---

## §11 Council pass — pending

Three lanes scheduled in parallel (per `feedback_design-doc-council-review.md` and `feedback_council-adversarial-prompt.md`):

- **dialectic-knowledge-architect** — adversarial on the falsifying-evidence section's completeness, the dialectic split's structural rigor, and the Wave 3 framing as a whole. Does §0 actually enumerate the disconfirmers honestly, or is it ratification dressed as inquiry?
- **feature-dev:code-reviewer** — adversarial on the implementation patterns: lock-invariant inventory completeness, state-ownership matrix correctness, the option-α-vs-β recommendation in §4, the test strategy in §7.
- **live-verifier** — adversarial on every named file:line, endpoint, field, table, plist, lease-plane Phase B date, and runtime claim in this RFC. Cross-checks against running governance-mcp + lease-plane + the audit.events schema.

Each lane's findings will be folded inline as a §V0.1.1 amendment block.

---

## §12 Open follow-on (not Wave 3 scope, surfaced for completeness)

- The substrate-tax bug class is structural to anyio + asyncio + asyncpg on a shared event loop (per `CLAUDE.md` §"Substrate Tax: anyio-asyncio Coupling"). Wave 3 dissolves it in the Wave 3 surfaces; the remaining Python surfaces (governance_core compute, LLM SDK paths, Watcher, MCP transport) still live on the same substrate. If Wave 3 closes successfully and post-Wave-3 measurement shows the bug class persisting in those surfaces, the operator decides per §"Post-Wave-3 candidates" whether to continue porting or pause.
