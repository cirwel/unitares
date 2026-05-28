# BEAM Footprint Roadmap

**Created:** May 3, 2026
**Last Updated:** May 28, 2026 (v0.3.1b amendment — ODE profile landed; the "7s locked-phase floor" framing falsified; post-lock enrichment was the actual user-visible floor, Python-fixed in PR #533, and re-benchmarked at 8/16 concurrent workers)
**Status:** v0.3 — destination is **A′ (committed, operator-decision-driven, 2026-05-05)**. Stateful coordination ports to BEAM in waves; stateless computation (numpy ODE, embeddings, LLM SDK calls) stays Python and is called from BEAM via Ports / HTTP. v0.2 had reopened the destination after PR #350's verdict; v0.3 closes it again on operator call after four Python-fixable PRs (#350 / #354 / #360 / #361) closed every measured floor without moving the user-visible ~11s p50 per-turn overhead. **Read the V0.3 RESOLUTION block first**, then the V0.3.1 amendment for what changed on 2026-05-28. v0 / v0.1 / v0.2 bodies preserved as historical record.
**Council pass v0.1 (2026-05-04):** dialectic-knowledge-architect (2B/4C/3D/4N), feature-dev:code-reviewer (2B/3C/2D/2N), live-verifier (7 VERIFIED, 6 DRIFT, 0 REFUTED, 1 SOURCE_ONLY) — all findings folded inline. Architect C3 + reviewer C3 both flagged "v0.1 destination committed pre-experiment"; the v0.1 conditionality block was the fold for that finding, and v0.2 was the realization of it.
**Council pass v0.3:** none on the migration call itself — that's an operator decision after a multi-session debate, and adversarial review of the call after operator commitment is the relitigation pattern v0.3 is trying to end. Council passes ARE expected on technical scope (Wave 1 supervisor topology, BEAM↔Python boundary contracts, identity-state migration) once those land as RFCs.

---

## V0.3.1 AMENDMENT 2026-05-28 — ODE profile + post-lock enrichment Python-fix

**Read this after V0.3 RESOLUTION.**

The v0.3 RESOLUTION's load-bearing unknown was the ODE profile — the 7s remainder of what was framed as the "locked-phase floor." Both halves of that framing turned out to be wrong:

- **The "7s" wasn't in the locked phase.** A side-by-side profile on 2026-05-28 (`/tmp/mcp_profile_analysis.txt`) measured the locked phase at ~100ms under 4-way concurrency, ~21ms serial — 2% of user-visible wall-clock, not 100%. The structured-log boundaries at `src/services/update_workflow_service.py:110-153` and `src/mcp_handlers/updates/pipeline.py:58-86` were already in place; the breakdown was always derivable, just not derived.

- **The actual floor was post-lock enrichment**, specifically `enrich_learning_context` calling `audit_logger.query_audit_log` (`src/audit_log.py:54-84,729-778`), which scanned `.jsonl` files synchronously on the event loop. That blocking sync I/O was hogging the single shared ExecutorPool thread (introduced by PR #218); the other PG/KG-backed enrichers queued behind it.

- **PR #533 collapsed it Python-side.** Side-by-side benchmark, same 4-way concurrent load, same DB:

| Phase | Master `087404e7` | Branch `e83d2920` (PR #533) | Ratio |
|---|---|---|---|
| user-visible p50 | 5321 ms | **51 ms** | **104×** |
| enrichment phase | 5070 ms | 22.7 ms | 224× |
| checkin total | 5281 ms | 47 ms | 112× |
| `enrich_learning_context` (directly fixed) | 3330 ms | 6.3 ms | 528× |
| `enrich_knowledge_surfacing` (cascading) | 1784 ms | 5.3 ms | 337× |
| `enrich_mirror_signals` (cascading) | 112 ms | 1.9 ms | 59× |

The fix is a `loop.run_in_executor` wrap on the sync `query_audit_log` call plus an `asyncio.gather` refactor on the 5 independent reads in `build_temporal_context`. CLAUDE.md "Substrate Tax" pattern #2 (sync-client + executor) — already documented as the workaround for this exact bug class.

### V0.3.1b sustained-concurrency follow-up — 2026-05-28

PR #533 merged before the recommended 8-16 worker benchmark ran. A follow-up run on current master (`13517b42`, governance MCP on `127.0.0.1:8767`) used the repo-captured load generator in `scripts/dev/process_update_loadgen.py`. Each worker minted a fresh identity and ran 16 `process_agent_update` calls with `response_mode=minimal`; this tests multi-agent concurrency, not same-agent mailbox serialization.

| Run | Calls | Wall-clock | p50 | p95 | p99 | max | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| PR #533 4 workers | 32 | 0.5s | 51ms | n/a | 125ms | 125ms | 0 |
| Master 8 workers | 128 | 4.5s | 281ms | 338ms | 360ms | 378ms | 0 |
| Master 16 workers | 256 | 8.9s | 554ms | 622ms | 641ms | 647ms | 0 |

**Interpretation.** The single-ExecutorPool-thread ceiling is still visible: p50 grows roughly with worker count. But the ceiling under this synthetic fresh-agent load is sub-second at 16 concurrent agents, not multi-second. That materially weakens "Wave 3 as urgent latency rescue" and strengthens "Wave 3 only if the coordination/ownership argument survives its own gates."

This is not the Wave 3 RFC §0(A.2) production-telemetry measurement by itself. The run uses fresh synthetic identities, short audit histories, no resident traffic mix, and one local server window. It is still enough to require a production telemetry read before citing old 5-11s `process_agent_update` p99 as current evidence for handler-dispatch migration pressure.

**Bias accountability.** Memory `feedback_substrate-migration-status-quo-bias.md` flags that I reliably resist substrate migrations across sessions. This profile + fix lands in that pole. I'm flagging it explicitly: a Python-side fix collapsing 104× of the user-visible floor with one `run_in_executor` is exactly the shape v0.3 said it would no longer be moved by. The operator has two honest reads available:

1. **The destination is still A′ on the architectural-ceiling argument.** The single-ExecutorPool-thread serialization is still real and is structural to anyio + asyncio + asyncpg on a shared event loop. Under N>>4 sustained concurrency this benchmark's 51ms p50 will degrade. The v0.3 framing was wrong about *which mechanism* dissolves under BEAM but right that *some* mechanism does. The fix doesn't change the destination; it changes the timeline pressure.

2. **The destination is open to relitigation.** Every measured floor in the project's history has resolved as a Python-side fix. The CLAUDE.md "Substrate Tax" workarounds are workarounds-not-architecture, but they keep working. v0.3 already committed past this read once with eyes open; doing so a second time with the same data shape weakens the empirical posture. Stop sign #2 of v0.3 was conditional on "ODE profile lands and 6+ of the 7s is numpy/embedding compute" — the literal premise (locked-phase floor = 7s) didn't hold, so the conditional doesn't directly apply, but the spirit (Python-fixable floors keep collapsing) does.

This amendment does not pick between (1) and (2). The v0.3 RESOLUTION explicitly named operator-decision as the criterion; same posture applies here. Wave 1 (Sentinel-on-BEAM, `com.unitares.sentinel-beam` PID 1782) is running and not affected by this finding. Wave 2 and Wave 3 sequencing are operator territory after this amendment is read.

**What the amendment does NOT close:**
- The single-ExecutorPool-thread serialization. PR #533 unhogged the executor thread for this specific bug; it did not eliminate the fact that all asyncpg goes through one thread. The deeper fix (multiple ExecutorPool instances, or `lite_safe`-skip-under-contention path in `pipeline.py:58-86`) is unaddressed.
- Redis async clients are still not ExecutorPool-wrapped per CLAUDE.md; that's orthogonal to PR #533 but the same bug class.
- The §129 14-day window (T+0 = 2026-05-19, closes 2026-06-02) is on its original auto-trigger via launchd one-shot `com.unitares.wave-1-section-129-reeval`. Condition 1 (incident_id wired) is still pending; this amendment does not satisfy it.

**Artifacts:**
- PR #533 — the fix + benchmark
- Profile run: `/tmp/mcp_profile_analysis.txt`, `/tmp/mcp_phase_logs_tail100.txt`
- Benchmark: `/tmp/loadgen_8770.py`, `/tmp/loadgen_baseline_out.txt`, `/tmp/loadgen_worktree_out.txt`, `/tmp/parse_phases.py`
- Reproducible benchmark tooling: `scripts/dev/process_update_loadgen.py`, `scripts/dev/parse_update_phase_logs.py`
- Sustained local benchmark outputs: `/tmp/process_update_loadgen_8x16.json`, `/tmp/process_update_loadgen_16x16.json`
- Memory: `project_locked-phase-floor-is-the-ode.md` (the misattribution this amendment supersedes — needs a 2026-05-28 amendment of its own)

---

## V0.3 RESOLUTION 2026-05-05 — operator-decision migration commit; A′ binds

**Read this before any other section.**

**Operator decision:** operator, 2026-05-05, this session. Honest reading: not capitulation under measurement, not a panic flip — a strategic call to stop iterating on Python-side floors that resolve cleanly individually but don't move the user-visible per-turn overhead.

**The data context that frames the decision:**

| Floor | PR | Pre-fix | Post-fix | Classification |
|---|---|---|---|---|
| `force=True` on 6 observe sub-handlers | #350 | 17,062ms cold + 15,000ms+ timeout under load | 167–182ms steady-state | sequential awaits — Python-fixable |
| Force-reload audit across 18 more sites | #354 | (same shape latent) | (sites dropped or kept with explicit comment) | same — Python-fixable |
| Cold-start metadata load (~3000 agents) | #360 | 17,062ms first call after restart | expected sub-second; per-agent provisional fetch + sequential cache.set replaced | sequential awaits — Python-fixable |
| KG hybrid_rrf neighbor fan-out (`get_discovery` × 30) | #361 | ~3000ms in-handler floor (60× amplification candidate surface) | gather() within pool — expected ~150–300ms | sequential awaits — Python-fixable |

**What the data DOES say:** every measured floor today resolved as sequential awaits in our own loops, not anyio scheduler interaction. The substrate-tax-as-anyio-coupling hypothesis is at its weakest in the project's history.

**What the data does NOT say (the honest unknown that v0.3 is committing past):** the ODE itself (`process_update_authenticated_async`, the 7s remainder of the locked phase per memory `project_locked-phase-floor-is-the-ode.md`, 2026-05-04) is unprofiled. If that 7s is numpy ODE solve / per-tick monitor mutation, BEAM relocates the same compute. If it's PG-round-trip serialization through the agent lock, BEAM's actor model dissolves it architecturally (per-agent process, no shared lock, supervisor-restart on failure).

**Why migrate anyway, eyes-open:**

1. **Architectural ceiling.** Even with every Python floor closed, `execute_locked_update` serializes per-agent through a shared mutex by design. Under self-traffic the lock IS the bottleneck — not the awaits inside it. BEAM's per-agent process model (one GenServer per `agent_id`, mailbox-serialized, no shared lock) dissolves the ceiling rather than raising it. The Python-side fixes optimize the floor; the user pain is the ceiling.
2. **Three-anyio-mitigation accumulation.** CLAUDE.md documents three substrate-tax workarounds (cached snapshot, run_in_executor, tight wait_for). Today's PRs add a fourth shape (sequential-awaits-in-our-own-loop) that's distinct from anyio coupling but lives in the same complexity budget. The operator's read: if the substrate keeps generating distinct shapes that all need workarounds, that's substrate-shaped evidence even when each individual shape isn't.
3. **Lease plane already proves the boundary.** Phase A (PR #305, 2026-05-03) ships the BEAM↔Python contract pattern — bearer-auth, fail-closed, REST surface. That pattern survives v0.2's destination revert and stays load-bearing under v0.3. We're not designing a boundary; we're scaling a boundary that already works.
4. **Substrate-migration status-quo-bias accountability.** Memory `feedback_substrate-migration-status-quo-bias.md` warns I "reliably resist substrate migrations across sessions." v0.3 is the moment that pole flips with operator authorization, not a moment I'm authoring around the warning. The pole-flip is documented here so future-me reads the rationale before re-litigating from cold.

**v0.3 inherits A′'s technical scope from v0.1 verbatim.** No re-derivation. The cut is stateful-coordinating to BEAM, stateless-computing stays Python. See §"v0.1 cut: stateful-coordinating vs stateless-computing" below for the per-surface test (still load-bearing). The MCP SDK gate framing (§"MCP SDK gate (v0.1)") still applies — the transport layer stays Python until an Elixir SDK closes the gate.

**Sequencing:**

- **Wave 1 — Sentinel-on-BEAM.** Smallest first ship. Already framed as substrate-fit-not-bug-fix in v0.1 §"Wave 1." Reuses lease-plane bearer-auth pattern from #305. Lowest blast radius — Sentinel is read-mostly, no agent-state mutation on the BEAM side. Profile-the-ODE work happens in parallel with Wave 1 implementation; the profile result informs Wave 3 sequencing but does NOT gate Wave 1.
- **Wave 2 (lease-integration boundary hardening + Wave 0 schema extension)** — keep as-is. Force=True audit completed in #354; remaining Wave 2 scope is contract-hardening, which is right work regardless and feeds Wave 3.
- **Wave 3 — handler dispatch + identity middleware + dialectic resolution.** Largest port. Gets its own RFC and council passes (technical scope, not migration-decision). Sequencing depends on Wave 1 evidence + ODE profile result. If the ODE is PG-bound: Wave 3 dissolves the lock structurally, biggest win. If the ODE is numpy compute: Wave 3 still moves coordination off the lock but the per-call cost stays similar; Wave 3 ships anyway because the lock-dissolution is the architectural goal.

**Profile-the-ODE work** is the load-bearing data point for Wave 3 sequencing. Concrete next probe: `cProfile` or line-level timing inside `process_update_authenticated_async` against the running governance MCP. Operator-authorized (process-restart class, low blast radius for instrumentation builds). The profile result lands in a v0.3.1 amendment block, not a destination revert — destination is committed under v0.3.

**What v0.3 ISN'T:**

- A retraction of #350 / #354 / #360 / #361. Those PRs are correct on their merits. The Python-fixable surfaces stay fixed; we're not rolling them back. They become the cleanup pass before the migration starts, not wasted effort.
- A v0.1 redux with weaker conditionality. v0.1's "destination committed pre-experiment" was an honest mistake the council caught. v0.3's "destination committed post-experiment, post-cleanup, on operator call" is a different epistemic posture — measurements happened, fixes shipped, the shape stayed structurally limited, operator made a strategic call. That's not the same pattern.
- A retraction of CLAUDE.md's substrate-tax framing. The four documented patterns still describe real bug classes that need workarounds while we're still on the substrate. The framing stays load-bearing during the migration period and gets phased out per-surface as Wave 3 lands.
- A commitment to a timeline. Migration is wave-sequenced with checkpoints. Each wave gets its own RFC + council on technical scope. Operator can pause / redirect / amend at any wave boundary.

**Stop signs for v0.3 (mirroring v0.1's pattern, retuned):**

1. **Wave 1 ships and Sentinel-on-BEAM produces measurable contention or coordination failure that DOESN'T exist on Sentinel-as-Python today** → architectural premise wrong, RFC for v0.4 (revert or radically rescope).
2. **ODE profile lands and 6+ of the 7s is numpy/embedding compute** → Wave 3's lock-dissolution argument weakens; bring it back to council before committing scope. Wave 1 still ships.
3. **MCP SDK gate stays unmet through Wave 3 timeline** → transport layer migration stalls indefinitely, but coordination-layer Wave 3 can still ship per A′ — the gate was a v0.1-era binary that v0.3 keeps but does not let block coordination work.

**Memory anchors:**

- `feedback_substrate-migration-status-quo-bias.md` — the pole-flip is documented here.
- `project_locked-phase-floor-is-the-ode.md` — load-bearing for the "ODE-profile is the unknown" framing.
- `project_substrate-question-governance-mcp.md` — gets a v0.3-decision update entry.

**Files / artifacts:**

- This file: V0.3 RESOLUTION at top, V0.3.1 AMENDMENT (council fold) immediately below, v0.2 / v0.1 / v0 preserved further down.
- v0.3 amendment commits as part of the migration kickoff — NOT bundled with code; doc-first so future-me reads the rationale before any port code.
- Wave 1 RFC follows in `docs/proposals/beam-wave-1-sentinel.md` (to be created when Wave 1 implementation starts).

---

## V0.3.1 AMENDMENT 2026-05-05 — council fold (architect / reviewer / live-verifier)

**Read this with V0.3 RESOLUTION above.** Three council lanes ran in parallel after V0.3 was drafted, scoped adversarial-on-technical-detail (NOT on the migration decision, which v0.3 closed). Convergent finding across all three lanes: **V0.3's "Sentinel is read-mostly" framing is wrong**, and the doc had several other load-bearing gaps. v0.3.1 folds the council findings inline; v0.3's destination commitment is unchanged.

### B1 (architect + reviewer + verifier — 3-lane convergence) — Sentinel is NOT read-mostly

The "lowest blast radius" framing in V0.3 §Sequencing is technically correct *for the agent-state DB layer* but mis-stated as a general property. Sentinel actually owns:

1. **File-backed cycle state** at `~/.unitares/anchors/.sentinel_state` via `agents/sentinel/agent.py:492-509, 695-699` (`load_state()` / `save_state()`). Carries the `forced_release_alarm.last_event_ts` cursor that fences alarm-replay.
2. **Findings emit channel** via `post_finding(...)` for `sentinel_finding`, `sentinel_forced_release_alarm`, and `lease_plane_phase_b_transition` events (`agents/sentinel/agent.py:596, 681, 733`). Every active dashboard/Discord-bridge subscriber is downstream.
3. **Python-runtime-specific anyio mitigations** (`agents/sentinel/agent.py:449-453`): `_poll_sync_forced_release` uses `asyncio.run()` inside a thread executor specifically to escape the anyio loop. Pattern does not exist in BEAM and needs a clean async polling replacement.
4. **Lease-advisory scope** holding `resident:/sentinel_cycle` for 300s (`agents/sentinel/agent.py:549-554`) — mutation against the lease plane, the very surface BEAM owns.

**Fold:** V0.3 §Sequencing Wave 1 framing now reads as **"Sentinel is read-mostly on the agent-state DB layer; owns atomic-write cycle state (`STATE_FILE`), findings-emit channel, lease-advisory scope, and Python-runtime-specific anyio mitigations. Lowest blast radius for agent-state DB; Wave 1 RFC must enumerate these four surfaces explicitly with cutover semantics."** Wave 1 RFC (to be drafted) is the work artifact for this; v0.3.1 names the requirement.

### B2 (reviewer) — Three unnamed lock invariants Wave 3 must preserve

`src/mcp_handlers/updates/phases.py` `execute_locked_update` enforces three invariants today that V0.3's "GenServer dissolves the lock structurally" claim does not name:

1. **api_key PG/cache reconciliation** (`phases.py:659-716`) — atomic against concurrent `get_agent` reads under GenServer model.
2. **thread_id / node_index monotonic advancement** (`phases.py:756-782`) — PG write must remain synchronous within message handler; fire-and-forget loses thread lineage on crash mid-sequence. **Relaxed in master 2026-05-05 via PR #362** (perf: lock held 6569ms/10684ms per turn; in-memory `ctx.meta` treated as process-local source of truth, PG copy moved to fire-and-forget for cross-process visibility). Wave 3 RFC must decide whether to re-tighten under GenServer atomicity or inherit the eventual-consistency posture.
3. **previous_void_active read-then-use-then-write capture** (`phases.py:734-741`) — must remain a single mailbox message, not split across two GenServer calls.

**Fold:** Wave 3 RFC (when drafted) MUST include §"Lock-invariant inventory" enumerating these three plus any others it identifies, stating which become GenServer-internal synchronous message steps vs. which can be relaxed.

### B3 (architect) — Missing §"State ownership and rollback during transition"

V0.3 inherits A′'s Ports/HTTP boundary pattern from lease plane Phase A (#305) but does not specify, for any wave, who owns each piece of state during cutover. Lease plane sidestepped this because greenfield — no Python predecessor holding state. Every Wave 1+ surface has a Python predecessor.

**Fold (added inline as new §"State ownership and rollback during transition" — implemented as part of this amendment):**

For each migrating surface, the corresponding Wave RFC MUST cover:

- **Single source of truth per state surface** during the transition window (Python-side, BEAM-side, or shadow-mode dual-write with one canonical reader).
- **Cutover semantics** — direct flip / shadow-mode-then-flip / dual-write-then-converge. Default presumption: shadow mode for ≥1 cycle of meaningful traffic before flip.
- **State format compatibility** — if BEAM writes the same on-disk file (`.sentinel_state`, `sentinel.json` session anchor), schema MUST be backwards-compatible with the Python reader OR a documented migration shim is provided. Default: BEAM does NOT modify the Python-readable format until Wave-N+1 explicitly changes the canonical reader.
- **Rollback procedure** — named launchctl/systemd command sequence, state-file restoration step, and explicit acknowledgement of which side keeps writing during the rollback window.

This subsection is now binding on every Wave RFC.

### B4 (reviewer) — No Wave 1 rollback path

Specific instance of B3. Wave 1 RFC for Sentinel-on-BEAM MUST include §"Rollback procedure" covering at minimum:

- (a) `.sentinel_state` format versioning OR migration shim so Python `load_state()` (`agents/sentinel/agent.py:492`) can read what BEAM wrote without zeroing the alarm cursor.
- (b) Explicit statement that the BEAM process will NOT modify `sentinel.json` session anchor format beyond what Python `GovernanceAgent` expects (interaction with `refuse_fresh_onboard=True` at `agents/sentinel/agent.py:476` — modifying it bricks Python rollback).
- (c) Named command sequence (launchctl stop / unload / load Python plist) that restores prior state without corrupting the alarm cursor.

### B5 (verifier REFUTED) — MCP SDK gate is OUT OF DATE

V0.1 framed the MCP SDK gate as "no production Elixir MCP SDK exists." That premise is dead as of 2026-05-05. hex.pm now has at minimum:

- `mcp_elixir_sdk` 1.0.1 — name match for "Elixir MCP SDK"
- `hermes_mcp` 0.14.1 — 14 minor versions, active development
- Plus: `ex_mcp` 0.9.1, `erlmcp` 0.3.1, `emcp` 0.3.4, `gen_mcp` 0.8.0, `elixir_mcp` 0.1.1, `elixir_mcp_server` 0.1.0

**Fold:** §"MCP SDK gate (v0.1)" text below now reads "as of v0.1 there was no production Elixir MCP SDK; as of v0.3.1 (2026-05-05) `mcp_elixir_sdk` 1.0.1 and `hermes_mcp` 0.14.1 exist on hex.pm at non-trivial version numbers — operator should evaluate fitness before citing absence as a gate condition. The transport layer is no longer structurally pinned to Python by SDK absence." This MATERIALLY STRENGTHENS the migration case, not weakens it — the gate v0.1 named is no longer holding.

### C1 (architect) — ODE profile timing

V0.3 says ODE profile happens in parallel with Wave 1 implementation. Architect refines: the profile data must land before Wave 1's **exit criteria** are written, not before Wave 1 starts. Otherwise Wave 1's "BEAM dissolved the ceiling" claim cannot be distinguished from "Sentinel was cheap to port."

**Fold:** Wave 1 exit-criteria authorship gates on ODE profile result. Wave 1 implementation can proceed in parallel; the exit-criteria document is what blocks.

### C2 (architect) — Dialectic stateful/stateless split

V0.3 lists dialectic resolution in the "stateful-coordinating, ports to BEAM" column (inherited from v0.1's cut). Architect: `src/dialectic_protocol.py:162` imports numpy. Dialectic is plausibly BOTH stateful-coordinating (resolution timing, participant lifecycle) AND stateless-computing (numerical synthesis math).

**Fold:** Dialectic flagged as "TBD per Wave 3 RFC" rather than monolithically committed to BEAM. Wave 3 RFC must split it explicitly.

### C3 (reviewer) — Lease plane Phase A is advisory-only; Wave 3 needs Phase B

V0.3 says "Lease plane already proves the boundary." Reviewer correction: Phase A contract is advisory mode — failed acquire MUST NOT block the caller's normal operation. Pattern generalizes for Sentinel (Wave 1) cleanly but NOT for Wave 3 handler dispatch, which requires Python MCP to stop accepting writes for an agent while its BEAM GenServer is mid-update. That's Phase B enforcement. Phase B eligibility for `dialectic:/` opens 2026-05-16 per lease plane RFC; at v0.3.1 time, **no Phase B window had yet been named for `resident:/` surfaces.**

**Fold:** Wave 3 RFC MUST address opening a `resident:/` Phase B window OR specify a different enforcement-grade boundary mechanism. Superseded status note: resident Phase B opened later via PR #476 (merged 2026-05-20 UTC); Wave 3 still must specify its own enforcement-grade boundary for handler-dispatch cutover rather than treating resident evidence as proof for unrelated agent-state surfaces. Wave 1 stays unaffected (Sentinel is fine on Phase A advisory).

### C4 (reviewer) — Test strategy under migration

V0.3 doesn't specify a minimum test bar before Wave 1 ships. 8329-test Python suite cannot cover BEAM-side code or the cross-runtime boundary.

**Fold:** Wave 1 RFC MUST include §"Test strategy" with at minimum:

- (a) ExUnit test driving fixture EISV event stream, asserting BEAM Sentinel emits correct `post_finding` shape
- (b) Decision on whether the 8329 Python tests remain the acceptance gate for the Python side during migration
- (c) Named integration test proving `resident:/sentinel_cycle` lease round-trip works end-to-end across runtimes

### Stop sign #4 (architect) — boundary substrate-tax

Added to V0.3 §Stop signs:

> **4. Wave 0 instrumentation post-Wave-1 shows Ports/HTTP boundary accruing >1 distinct workaround pattern** → boundary design is wrong, halt before Wave 3. The four-anyio-mitigation argument that motivated v0.3 applies recursively to the boundary itself; if the migration replicates the substrate-tax shape one level out, the migration is not solving the problem.

### What V0.3.1 changes vs V0.3

- §Sequencing Wave 1: "read-mostly on agent-state DB" qualifier added, four state surfaces enumerated.
- §Stop signs: #4 added.
- §"State ownership and rollback during transition": new subsection (binding on all Wave RFCs).
- §"MCP SDK gate (v0.1)": text updated with hex.pm reality (gate dissolved).
- Wave 1 RFC requirements explicit: state-ownership matrix, rollback procedure, test strategy.
- Wave 3 RFC requirements explicit: lock-invariant inventory, dialectic split, Phase B enforcement gate.

### What V0.3.1 does NOT change

- Migration commitment unchanged. Operator decision stands.
- Wave sequencing unchanged (Wave 1 → Wave 2 → Wave 3).
- A′ technical scope unchanged (stateful-coordinating to BEAM, stateless-computing stays Python).
- ODE profile parallel-to-Wave-1 framing unchanged (only the exit-criteria authorship gates on it, per C1 fold).

---

## V0.3.1a INVENTORY ADDENDUM 2026-05-07 — `dialectic/session.py` Wave 3 surfaces

Bookkeeping addition. **No change to V0.3 destination, sequencing, scope, or stop signs.** Adds one file to the Wave 3 surface inventory and documents an operator-protective non-action.

**Surface.** `src/mcp_handlers/dialectic/session.py` carries six raw asyncpg awaits in MCP-handler context: three in `load_session_as_dict` (lines 273, 282 — `await conn.fetchrow` / `await conn.fetch` inside `compatible_acquire(db._pool)`) reached from the `@mcp_tool("get_dialectic_session")` handler at `dialectic/handlers.py:656`; one in `list_all_sessions` (line 433 — `await conn.fetch(query, *params)`) reached from `@mcp_tool("list_dialectic_sessions")` at `handlers.py:830`; plus two delegated `await pg_get_session(...)` calls at lines 230 and 247 inside the load path. **Same anyio-coupling shape as CLAUDE.md substrate-tax patterns #1 and #2 — not a new shape.** V0.3's "fourth shape" count is unchanged.

**Watcher trail.** Four `P004` findings (`1727cfea`, `8802bf3c`, `7fb72d52`, `7a207d7e`) raised against `.worktrees/dialectic-retire-quorum-escalated/src/mcp_handlers/dialectic/session.py:{240,249,400,433}` while PR #406 was in flight. PR #406 merged 2026-05-07; the underlying code is now on master at the line numbers above. Worktree-bound findings dismissed as `stale` 2026-05-07.

**Why no executor-wrap fix landed alongside the dismissal.** CLAUDE.md is explicit that "the accumulation of these patterns is not progress." Wrapping six new sites in `run_in_executor` one wave-cycle before Wave 1 ships is the workaround-stacking pattern V0.3 explicitly committed past. These surfaces enter the Wave 3 inventory; Wave 3's per-agent GenServer model dissolves them by replacing the shared event-loop dispatch, not by wrapping it. The non-action is the discipline holding.

**Wave 3 inventory implication.** When the Wave 3 RFC drafts the `@mcp_tool` handler port list, `dialectic/handlers.py::list_dialectic_sessions` and `dialectic/handlers.py::get_dialectic_session` are on it, and `session.py::load_session_as_dict` / `session.py::list_all_sessions` are the underlying functions to port (or replace with BEAM-side equivalents querying the same `core.dialectic_sessions` / `core.dialectic_messages` tables through Postgrex). This is consistent with V0.3.1 §C2's dialectic-stateful/stateless split — these are stateful-coordinating reads that go to BEAM under A′.

---

## V0.3.2 AMENDMENT 2026-05-09 — Wave 3 substrate re-litigation (scope question, not destination revert)

**Summary.** Wave 3 RFC has gone through four iterations (v0.1 → v0.1.1 → v0.1.2 → v0.2 → v0.3). Each iteration's council pass surfaced a fresh substrate-tax bias signature in the redraft itself — five across four iterations as of 2026-05-09. The v0.3 council architect lane invoked v0.3's own §15 escalation rule: *"if v0.3 surfaces a sixth bias signature, that's evidence the substrate question itself needs re-litigation rather than redraft mechanics."* It did. This amendment opens that re-litigation at the parent-roadmap altitude — **not as a destination revert** (A′ stands; operator decision 2026-05-05 unchanged) but as a scope question on Wave 3 specifically.

### Bias-signature accumulation across Wave 3 redrafts (council-evidenced)

| Iteration | Bias signature surfaced | Council source |
|------------|--------------------------|------------------|
| v0.1 | §0 disconfirmer set is post-hoc rationalization; §5.3 `_has_recently_reviewed` KEPT-Python on "PG round-trip dominates" reasoning | architect lane (2026-05-08) |
| v0.1.1 | §B6 cache-invalidation defense recreates substrate tax; §B8(iii) lunge-at-first-association on single-process; §8 criterion 6 sunk-cost protection | architect Lane 1 of v0.1.1 council |
| v0.1.2 | §C2 anchored-to-baseline-that-doesn't-exist (lease-plane Phase A had zero rows); architect predicted "if v0.1.2 produces another structural delta, v0.2 redraft becomes mandatory" | verifier DRIFT #3 + architect explicit warning |
| v0.2 | §10 versioned-baseline + §4 advisory-lock added sustained PG-coordination load to the substrate Wave 3 exists to relieve | architect lane (2026-05-09) — declared mandatory v0.3-redraft-from-scratch |
| v0.3 | §10 reconciliation loop is structurally same PG-as-arbiter pattern, slower cadence; perpetual periodic full-table SELECT against the substrate, "should never happen in steady state" justification = canonical conservative-substrate-bias-with-prettier-prose | architect lane (2026-05-09) — invoked substrate-re-litigation discipline |

The redraft pattern produced a new bias signature at every cycle. That is not a property of any individual redraft — it is structural to **what is being ported**. Wave 3's three surfaces (handler dispatch + identity middleware + dialectic resolution) all hold state whose canonical form lives in PostgreSQL: identity rows in `core.identities`/`core.agents`, dialectic state in `core.dialectic_sessions`, audit emissions in `audit.coordination_events`. Any BEAM port of these surfaces must coordinate against PG truth. Each redraft chose a different mechanism for that coordination (per-observe versioned reads, ETS+reconciliation, advisory locks); each mechanism was a fresh shape of "PG-as-arbiter," and each surfaced as a bias signature.

This is the architect's load-bearing finding from v0.3 council: the PG-coordination load is not a redraft defect, it is intrinsic to porting *these* surfaces.

### Independent calendar finding (verifier, 2026-05-09)

Live-verifier discovered Wave 1 actual elapsed is **~2 days**, not the v0.3 estimate of "~3 weeks." Git log: Wave 1 first commit `2026-05-05 11:39`, last Wave-1-tagged commit `2026-05-07 01:12`. Wave 3 v0.3's disconfirmer (E) "Wave 1 × 3 calendar cap" is therefore **~6 days**, not ~9 weeks. Wave 3 v0.3 has 10 prereq PRs (each requiring its own council pass), and PR #6 alone needs ≥14 days of Phase A measurement before disconfirmer (B) thresholds can be set.

**The calendar gate is structurally infeasible at current Wave 1 measurement.** This finding is independent of the bias-signature analysis — it is git arithmetic. Even if v0.3's structural bias issue is resolved, the prereq stack cannot ship inside (E)'s cap.

### What re-litigation does NOT mean

- **Not a destination revert.** A′ (operator-decision migration commit, 2026-05-05) stands. The substrate-migration call is not re-opened. The pole-flip rationale in V0.3 RESOLUTION above remains load-bearing.
- **Not a Wave 1 / Wave 2 reversal.** Wave 1 (Sentinel-on-BEAM) shipped Surface 1+2 successfully; Wave 2 boundary hardening landed clean. Both stand.
- **Not a v0.4 redraft of Wave 3.** Per v0.3 §15's own discipline, another redraft cycle is the wrong shape of move. Five bias signatures across four iterations is data; producing a sixth in v0.4 is just continuing the experiment.

### What re-litigation DOES open

The question for operator decision: **is Wave 3 (handler dispatch + identity middleware + dialectic resolution) the right shape of port given (a) the bias-signature accumulation pattern and (b) the calendar reality?**

Specific options:

**(α) Defer Wave 3 RFC pending Wave 1 burn-in + Phase A measurement.** Don't iterate further on Wave 3 RFC text right now. Ship the prereqs that aren't Wave-3-specific (lease-plane Phase A latency instrumentation, the ODE profile, the boundary-event helpers). Let those produce evidence over ≥14 days. Re-attempt Wave 3 RFC with measurement in hand, not before. The bias signatures may have been at least partly an artifact of designing without the data the design depends on.

**(β) Scope-reduce Wave 3 to the smallest port that doesn't tangle with PG-resident identity state.** Concrete shape: port **only dialectic resolution coordination** (the session-keyed GenServer + saga from v0.3 §9) — that surface has the cleanest PG boundary (writes to `core.dialectic_sessions` are sparse; the saga state is its own table). Defer handler dispatch and identity middleware to a later wave once the Wave 1+2 pattern produces more substrate evidence. Smaller blast radius, smaller PG-coordination footprint.

**(γ) Replace Wave 3 with a structurally different port.** The roadmap §"v0.1 cut" classified surfaces as stateful-coordinating vs stateless-computing. The current Wave 3 surfaces are all stateful-coordinating-with-PG-canonical. Are there *other* stateful-coordinating surfaces that don't have PG-canonical state? E.g., the `_baseline_cache` in `governance_core/ethical_drift.py:418` is process-local memory only — porting *just that* to BEAM as ETS would be a clean substrate-fit-not-PG-arbiter port, no shadow tables or sagas needed. That's a different Wave 3.

**(δ) Operator override — proceed with v0.3 implementation regardless of council BLOCK.** The bias signatures are real but possibly noise from over-recursive council framing; the calendar gate is real but possibly resolvable by extending (E)'s cap explicitly. If (δ), then minimum surgical work: fix the three implementation-blocking REFUTEDs from v0.3 verifier (regex `[a-z_]+` rejects `503_emission` event_type — change to `[a-z][a-z0-9_]*` or rename event; seed `'false'` invalid for `identity_strict_mode` — should be `'log'`; seed `'enforced'` invalid for `ipua_pin_check_mode` — should be `'strict'`); specify the cache-miss `BaselineWriter.warm/1` path; replace v0.3 §10.3 reconciliation loop with a PG-trigger-based out-of-band-write detector; pin (E)'s "Wave 1 elapsed" to the corrected ~2 days with explicit rationale for whether the cap stays at ×3 or expands.

### What the operator should weigh

- **Do you trust the bias-signature pattern as signal?** Each was found by the same architect-lane prompt; the prompt asked for bias signatures, so it found bias signatures. That's a real possibility. But the verifier's calendar arithmetic is independent — Wave 1 elapsed × 3 = ~6 days is hard data, not framing.
- **Is there evidence that *would* close the question?** The architect's diagnosis is structural (Wave 3 surfaces need PG-resident state). That's empirically testable: can you write a Wave 3 design that doesn't add ANY new PG-coordination load? If yes, the bias signatures were redraft-artifact. If no, they're structural.
- **Wave 0 is producing data right now.** The lease-plane Phase A baseline that v0.3 (B) depends on currently doesn't exist (`audit.coordination_events` has 0 rows; `127.0.0.1:8788` connection-refused at v0.3 council time). Designing Wave 3 RFC against that absence repeatedly produces "anchored to TBD" structures that the council marks as bias signatures. Reversing the order — measure first, then design — may resolve the pattern at zero substrate-question cost.

### v0.1.x / v0.2 / v0.3 branches

Preserved as reference history:
- `wave-3-rfc-draft` — v0.1 + v0.1.1 + v0.1.2 commits (council folds in amendment-stack form)
- `wave-3-rfc-v0.2` — v0.2 single-commit redraft from v0.1.2 tip
- `wave-3-rfc-v0.3` — v0.3 single-commit redraft from v0.2 tip

Each is referenceable for the architectural decisions tried at that iteration. None should be merged to master pending re-litigation outcome.

### What this amendment does NOT change

- A′ destination commitment.
- Wave 1 (shipped) or Wave 2 (in progress).
- The MCP SDK gate framing.
- The lease-plane v0 RFC or its Phase A.
- The Wave 0 channel design (`audit.coordination_events`) or its CHECK constraint scope.

What it changes: Wave 3 sequencing is on hold pending operator decision among (α)/(β)/(γ)/(δ). Until that decision lands, no v0.4 redraft, no implementation work on Wave-3-specific prereqs. The non-Wave-3-specific prereqs (lease-plane Phase A latency instrumentation, ODE profile, boundary-event helpers) can ship independently because they're useful regardless of Wave 3's eventual shape.

---

## V0.3.3 STATUS FOLD 2026-05-20 — resident Phase B + measurement persistence

This is a bookkeeping fold over shipped work after v0.3.2. It does not change
A' or resolve the Wave 3 re-litigation question.

- **Resident Phase B is already open.** PR #476
  (`feat(lease-plane): enforce resident phase b leases`) merged 2026-05-20 UTC
  after the mechanical evaluator returned PROMOTABLE with controlled drill
  evidence. Wave 3 no longer needs to open the `resident:/` Phase B window as
  future work, but it still needs an enforcement-grade boundary decision for
  handler-dispatch cutover.
- **Lease-boundary RPC latency is now recorder + persistence.**
  PR #480 added the Python client recorder in `src/lease_plane/client.py`;
  PR #481 added `perf_monitor_persist_task`, catalog entries, and
  `metrics.series` persistence for `lease_plane.client.v1.lease.acquire.p50`
  and `.p99`.
- **ODE profile persistence also landed.** PR #481 persists
  `ode.numpy_step_ms.p50` / `.p99`; the remaining question is the 7+ day
  reading, not whether the longitudinal storage path exists.

---

## V0.2 RESOLUTION 2026-05-05 — verdict landed; destination reopens *(SUPERSEDED by V0.3 RESOLUTION above; preserved as historical record)*

**This block is preserved for historical record.** v0.2 reopened the destination after PR #350's Python-fixable verdict. v0.3 (above) closes it on operator decision after three more Python-fixable PRs (#354, #360, #361) closed the remaining measured floors without moving user-visible per-turn overhead.

**Read this before any other section.**

PR #350 (merged 2026-05-05T03:28Z) dropped `force=True` from 6 observe sub-handlers, removing the per-call 3221-await loop on the request path. Per v0.1 §"Conditionality on PR #350's post-fix verdict," the experiment was: does the in-handler floor close (Python-fixable in-place) or persist (substrate-coupling)?

**Today's data (2026-05-05, ~05:00 UTC, post-restart probe):**

| Handler | Pre-fix | Cold-start (first call after restart) | Steady-state (subsequent calls) |
|---|---|---|---|
| observe(action=aggregate) | ~2,864ms (council live-verifier) / 15,000ms+ timeout under load | 17,062ms (one coord_failure event recorded) | 167–182ms (5 runs) |
| observe(action=anomalies) | (timeout under load) | not measured | 92–95ms (3 runs) |

**Verdict: Python-fixable.** Steady-state observe handlers are now sub-200ms. The 60× amplification floor was the 3221-await loop, not anyio/asyncio coupling at the substrate layer. v0.1's strongest single piece of falsifying evidence resolves as a Python-side anti-pattern that PR #350 closed for these specific handlers.

**Per v0.1's own conditionality, the destination commitment reverts to a question.** v0.2 is that revert.

**What v0.2 IS:**

- The destination is OPEN. Neither Read A (v0) nor A′ (v0.1) is currently committed-to. The Wave structure (Sentinel → force=True audit + lease-integration → handler dispatch + identity + dialectic) is preserved because that work is right regardless of destination — it eliminates the substrate-tax surface where it currently bites without pre-deciding the larger question.
- A formal record that v0.1's enthusiasm-pole-bias check (which architect C3 + reviewer C3 both flagged) was correct. v0.1 was committed pre-experiment; the experiment ran; the prediction the council warned against materialized; v0.2 is the discipline closing the loop.
- A live recommendation: continue accumulating Wave 0 channel data on the OTHER ~24 force=True sites (Wave 2 scope) before any further destination commitment. Those sites still bypass the cache (force=True is the bypass) and may still produce steady-state amplification. Today's verdict tells us about observe specifically; it does NOT generalize across the other force-reload-bearing surfaces.

**What v0.2 ISN'T:**

- A return to v0's Read A. v0's "bug class closed" premise (PR #290 fixed Sentinel-loop call site) is still narrow. Today's data doesn't restore Read A's load-bearing claim; it just means the case for moving past Read A is not as strong as v0.1 said it was. Both v0 and v0.1 had load-bearing premises that didn't survive contact with new evidence; v0.2 commits to neither and waits for more data.
- A retraction of the substrate-tax framing in CLAUDE.md / AGENTS.md. The four documented mitigation patterns (cached snapshot, run_in_executor, tight wait_for, force=True N-await) are still real. The asymptote argument (workarounds keep accreting) still applies if more patterns emerge. CLAUDE.md's "do not treat pattern-accumulation as progress" stance survives.
- A retraction of Wave 0 itself. Wave 0 just did the job it was designed for: surfaced a measurement, the measurement drove a destination commitment, the commitment was conditional on follow-up data, the follow-up data ran, the commitment resolved. That's the discipline working.

**What v0.2 keeps from v0.1:**

- The §"v0.1 cut" framing (stateful-coordinating vs stateless-computing) as the right *test* per surface, even though v0.1 used it to pre-decide a destination prematurely. The test stays useful for evaluating individual port decisions (Sentinel → BEAM still passes the test; observe handlers staying Python passes the test post-fix).
- The §"MCP SDK gate" — still the right binary if/when destination questions reopen. Three named conditions, NOT-closure list, named owner.
- Wave 1 (Sentinel-on-BEAM) — substrate-fit argument stands; today's verdict doesn't move it.
- Wave 2 (force=True audit + lease-integration + Wave 0 schema extension) — the work is right regardless of destination. The ~24 remaining force=True sites are still substrate-tax surfaces that need site-by-site treatment; PR #350 established the playbook.
- Wave 3 (handler dispatch + identity + dialectic) — deferred indefinitely until Wave 2 produces its data. The doc no longer leans on "Wave 3 is where governance MCP coordination ports" as a destination claim; Wave 3 is one possible future, not the committed path.
- The conditionality discipline. v0.2 itself is conditional on more Wave 0 data: if the other ~24 force=True sites produce steady-state amplification under load (post-Wave-2 cleanup), v0.3 may re-open A′; if they also resolve as Python-fixable, the destination genuinely is "stay Python with periodic substrate-tax cleanup," and v0.3 closes the question in that direction.

**Source of the v0.2 resolution.**

- **2026-05-05 verdict probe** (this session). Restarted governance MCP at 04:55 UTC after operator authorization (process-restart blast radius across active sessions). Pulled local master from 5615bc22 → 60fe16bb (PR #350's merge commit; local master had been stale 1.5h post-merge, requiring `git pull` before restart for the fix to actually be live in the running process). Probed observe(action=aggregate) and observe(action=anomalies) via curl against `localhost:8767/mcp/`; timed each via `python3 -c 'import time;print(time.time())'` deltas. Cold-start first call: 17,062ms (audit.events coord_failure recorded). Subsequent 5 aggregate + 3 anomalies probes: 92–182ms. No further coord_failure events.
- **v0.1's own conditionality block** (folded from architect council C3 + reviewer council C3). The conditionality WAS the fold; v0.2 is the conditionality firing on real data. This is the discipline doing what it was designed to do.
- **Memory anchors:** `feedback_substrate-migration-status-quo-bias.md` (cuts both ways), `feedback_verify-construction-lifecycle.md` (lazy vs eager — relevant to "cold-start tax stays even with force=True dropped"), `feedback_running-process-vs-master-commit.md` (the "long-lived resident may have stale code" pattern fired today: process restart didn't deploy the fix because local master was stale).

**What's needed.**

- v0.2 lands (this commit). No council pass required for v0.2 itself — it's a step-down from v0.1's commitment, which is the conservatively-safer move; the council finding that drove it was already addressed in the v0.1 fold; further adversarial review has diminishing returns when the change is "commit less, not more."
- Wave 2 begins (force=True audit across the ~24 sites). PR #350 established the playbook (drop / replace with single-agent fetch / keep with explicit-comment justification). Doing this site-by-site is the right work and will produce the next round of Wave 0 data.
- Memory project entry updated to reflect the resolution.

---

## V0.1 DESTINATION 2026-05-04 — A′ replaces Read A *(SUPERSEDED by V0.2 RESOLUTION 2026-05-05; preserved as historical record)*

**This block is preserved for historical record. It is NOT the current destination.** v0.1's destination commitment was conditional on PR #350's post-fix data per the Conditionality block below; that data landed 2026-05-05 and resolved as Python-fixable, reverting the destination to a question per the conditionality's own discipline. See V0.2 RESOLUTION above.

After the 2026-05-04 falsifying measurement (see AMENDMENT block below) and a substantive operator/agent dialogue on what the data actually argues for, **the destination of this roadmap is A′, not Read A.** Operator decision: 2026-05-04, this session.

**A′ in one sentence.** Stateful coordination — handler dispatch, identity middleware, dialectic resolution, sentinel/vigil/chronicler, force-reload-bound coordination paths — ports to BEAM. Stateless computation — LLM SDK calls, EISV math, pattern analysis, calibration, ML scoring, the MCP SDK transport layer until/unless an Elixir SDK exists — stays Python and is called from BEAM via Ports / HTTP. The cut is "stateful-coordinating vs stateless-computing," tested per-surface by ecosystem maturity, not "control plane vs intelligence plane" as v0 framed it.

**Conditionality on PR #350's post-fix verdict (folded from council pass C3, both lanes).** A′'s destination commitment is **conditional on PR #350's coordination-failure rate post-fix**. PR #350 (now merged 2026-05-05) drops `force=True` from 6 observe sub-handlers, eliminating the 3221-await loop on the request path. The Wave 0 channel will reveal one of two answers in the days following:

- **If observe-tool `coordination_failure.mcp_handler_timeout.tool_decorator` rate drops to near-zero post-fix** → the in-handler floor was the await loop (Python-fixable in-place). The remaining substrate-coupling evidence is then thinner than v0.1 leans on, and **v0.1 reverts to a question**: maybe the cut shift was right anyway because of the OTHER force-reload sites and the bystander effect, but the falsifying-evidence base is no longer the 60× number on KG calls — it's a smaller observation that requires its own substantive case. v0.2 reopens the destination decision.
- **If observe timeouts persist post-fix, OR if a different surface produces equivalent amplification under load** → the in-handler floor IS substrate-coupling, the falsifying evidence holds, and **v0.1's A′ destination becomes binding** (council ack pass v0.1.1 on this state, then merge as v0.1 final).

Per `feedback_substrate-migration-status-quo-bias.md` — both poles of the bias are wrong; "I want to fully migrate" is data about operator state, not about whether the substrate-tax is real. v0.1 honors that by gating the destination on the experiment that actually distinguishes the two readings of today's data, not on enthusiasm or council consensus alone.

**What changed from v0's Read A.** v0 cut at "Python thinks, BEAM governs," which placed governance MCP — the actual governance/control plane — on the *intelligence* side because it's running in Python today. That was the load-bearing error v0 inherited from the falsified "bug class is closed" premise. The 60× amplification measurement says the substrate-coupling tax is alive on every coordination surface that runs in Python on a shared anyio/asyncio event loop, governance MCP included. A′ moves the cut to where the data actually puts it: the boundary is "does this surface hold state and coordinate, or does it compute?" — not "is this surface decision-making versus reasoning?"

**What does NOT change.** The kernel doc's non-goal "Do not rewrite UNITARES in Elixir" stands — A′ does not rewrite UNITARES; it ports the coordination layer and keeps the compute layer in Python. The Pi-side anima-broker decision (retired 2026-05-01) stands — A′ is Mac-side governance MCP scope, not Pi. Lease plane (Phase A complete 2026-05-03) is a proof of pattern A′ generalizes; nothing about it changes.

**The MCP SDK gate (full Read B remains conditionally open).** A′ has one explicit gate to potentially-future Read B: the Anthropic Python MCP SDK is the primary reason the MCP transport layer stays Python under A′. If a production-mature Elixir MCP SDK lands (Anthropic-shipped, community-built and battle-tested, or a credible hand-roll spike) AND the Wave 0 channel post-A′ shows the BEAM↔Python boundary itself accruing new substrate-tax patterns at the Ports interface, then porting the MCP transport layer becomes the natural Wave-N decision and Read B comes onto the table. The gate is **explicitly external-dependency-bound** so the destination doesn't drift on internal enthusiasm. See §"MCP SDK gate" below for the exact trigger.

**Wave reordering under A′.** v0's "Wave 2 deferred pending Wave 1 evidence" reads under v0.1 as: Wave 1 (Sentinel) ships; Wave 2 is audit pipeline + lease integration (the highest-volume coordination paths after Sentinel); Wave 3 is handler dispatch + identity + dialectic resolution (the largest single port; gets its own RFC and council passes). Each wave still gates on the prior wave's exit criterion via the Wave 0 channel. See §"Wave 2 — under v0.1" and §"Wave 3 — under v0.1" below.

**Stop sign added under v0.1.** Re-cutting at "control plane vs intelligence plane" or any phrasing that places governance MCP coordination on the Python-permanent side is now explicitly out of bounds without operator authorization to revert v0.1. v0's Read A reasoning is preserved below as historical record; do not silently restore it.

**Source of the destination decision.**

- **2026-05-04 operator/agent dialogue** (this session). Operator question "is hybrid best? what do we lose in python if we go full on?" prompted a per-surface ecosystem-maturity audit instead of a generic "Python is for ML" framing. The audit produced the cut shift.
- **AMENDMENT 2026-05-04 evidence** (below). The 60× amplification number on governance-MCP path is the falsifying observation that v0's destination-decision rested on a closed premise.
- **Wave 0 channel** (PRs #342 + #345 + #348 + #350 all merged 2026-05-04/05). Real coordination_failure events confirming the substrate-coupling fingerprint on the governance-MCP request path, captured by the channel v0 prescribed for exactly this question. PR #350's post-fix data is the experiment that gates v0.1's destination commitment per the Conditionality block above.
- **Lease plane Phase A** (PR #305, merged 2026-05-03). Proves the Postgrex / OTP / Ports pattern works at scale; A′ is the same pattern applied to more surfaces.

**What's needed.**

- This v0.1 amendment lands. v0.2+ may revisit the cut with more Wave 0 data; v1 lands when Wave 1 closes and Wave 2 scope locks.
- Wave 0 instrumentation continues to evolve (PR #350 merged 2026-05-05; further force-reload audits across non-observe surfaces are now Wave 2 scope per v0.1 — see Wave 2 below).
- Council pass on this v0.1 amendment, per v0's discipline. Findings folded inline before the v0.1 status is treated as binding.

---

## AMENDMENT 2026-05-04 — falsifying measurement on governance-MCP path

**Read this before any other section.** A measurement on the governance-MCP request path on 2026-05-04 falsifies a load-bearing premise of v0.

**The measurement.** KG calls that complete in 21–71ms standalone run at ~4,464ms in-handler — a ~60× amplification, with the floor sub-100ms and the rest in scheduling / pool-acquisition / event-loop contention. The amplification is, by definition, in the substrate-coupling layer, not in Postgres or Cypher.

**What this falsifies.** v0 cites PR #290 (Sentinel-loop call site, ">400 cycles since restart with zero failures") as evidence the asyncpg/anyio bug class is closed and uses that to declare Wave 1's BEAM motivation "dead" (§"Wave 1 — Why first — the honest motivation") and to re-anchor the roadmap on substrate-fit-not-bug-fix grounds (§"Convergent evidence behind the substitution"). The 2026-05-04 measurement says the bug class is alive on a different surface — same coupling, different call path. PR #290 closed it at *one site*, not at the bug-class level. The conflation drove the Read-A-as-stable-destination conclusion.

**What this does NOT do.**

- It does not by itself argue Read B (full rewrite). The operator's stated destination ("full BEAM nervous system") and the substrate-migration-enthusiasm-bias check from §"Operator-consent framing" both still apply.
- It does not invalidate the lease-plane Phase A or the control-plane / intelligence-plane cut. Those stand on their own evidence.
- It does not retire Wave 0 — Wave 0's measurement infrastructure is exactly what makes amendments like this one possible, and is more clearly load-bearing now, not less.

**What it does change.**

- Sections that depend on "bug class closed" — specifically the bullet on line 17 ("Wave 1's central premise was stale"), the bullet on line 19 ("the asyncpg/anyio bug class … was closed in production"), the §"Wave 1 — Why first" claim "**That motivation is dead**", and the supporting citations on lines 219, 227, 230 — should be re-read as scoped-to-Sentinel-loop, not bug-class-closure.
- The "Read A as stable destination, not a way-station to Read B" framing in §"What this document is" depends on the falsified premise. It is not automatically wrong (substrate fit, supervision discipline, and operator cost are all independent arguments), but it no longer carries the "and the bug class is fixed anyway" wind that the original framing leaned on.
- Wave 1's exit criterion ("zero coordination-class incidents in the Wave-0 instrumentation feed for 14 days") is now also a probe of whether the bug class has substrate-shaped recurrence on a BEAM-resident service, not just a parity check. The same wave will produce the comparison data Read B's case rests on.

**What's needed.**

- Operator decision on whether v0's strategic conclusion holds, weakens, or flips given the new measurement. This amendment does not pre-decide; it re-opens a question v0 closed prematurely.
- A separate amendment or v0.1 that re-states Wave 1's BEAM motivation honestly (substrate-fit AND live bug class on governance-MCP path, not substrate-fit-only-because-bug-class-is-fixed).
- CLAUDE.md §"Substrate Tax: anyio-asyncio Coupling" (updated 2026-05-04) is the operational counterpart to this amendment — it tells in-repo agents the patterns are workarounds, not architecture.

**Source — and why this is on-mission for Wave 0, not adjacent to it.** v0 explicitly frames Wave 0 as the measurement infrastructure that makes later waves' exit criteria evaluable: "Without Wave 0, no later wave's 'exit criterion' can be honestly evaluated and no Read B trigger can fire on evidence rather than vibes" (§"Wave 0 — coordination_events"). The 2026-05-04 measurement is the first round of exactly that evidence:

- **Wave 0 channel proper.** PRs #342 (foundation) + #345 (step 2A: MCP decorator timeout chokepoint emit) + #348 (caller agent_id + session_id context fallback) produced 6 `coordination_failure.mcp_handler_timeout.tool_decorator` events on observe / list_agents in the ~10.75–14.5h window after #345 merged (first event at 21:15 UTC = 10.75h after merge; cluster running through 18:57 MDT next day = 14.5h). 100% concentration on two consolidated tools (`observe`, `list_agents`) at the time v0.1 was first drafted; the dataset has since grown to 13 events with additional event types (process_agent_update, detect_stuck_agents, identity), reducing observe/list_agents share to ~46% — the convergence on observe/list_agents was a *first-window* pattern, not a permanent one. Cascade pairs at 15:15:00.88 / 15:15:00.93 and 18:57:37.93 / 18:57:41.99 (MDT) suggesting in-handler contention; one 22.6s elapsed-past-15s outlier (`elapsed_s=22.615` at 18:18:20 MDT) indicating cancellation propagation friction (an asyncio/anyio coupling tell). These are the substrate-coupling fingerprint, captured by the channel the roadmap said would capture it. The data is truncated at the 15s decorator wall, so the channel sees the symptom but not the magnitude.

  **Schema-routing drift (live-verifier finding):** the events are written to `audit.events` (with `event_type LIKE 'coordination_failure.%'` filter), NOT to the dedicated `audit.coordination_events` table specified in v0's Wave 0 envelope section. The dedicated table exists with the correct schema and check constraints but is empty; `src/coordination_failure_emit.py` (the production wire from `@mcp_tool`'s TimeoutError handler) deliberately routes to `audit.events` via `audit_logger._write_entry` because the council BLOCKED the direct asyncpg-await-from-decorator path. This is consistent — the bug class itself is what blocked the dedicated-table path — but v0's Wave 0 envelope language reads as if `audit.coordination_events` is the canonical surface, which it currently is not. Routing to the dedicated table is a tactical follow-up, not a Wave 2 prerequisite (see Wave 2 below for v0.1 rescoping).
- **Probe alongside.** A parallel Claude session, looking for the unscoped magnitude, measured 21–71ms standalone vs ~4,464ms in-handler on KG calls — the ~60× number cited above. This is the same coupling, measured at a different boundary (per-call latency rather than per-handler timeout).
- **Wave 0 is producing the experiment, too.** PR #348's planned follow-up — drop `force=True` on `observe(action=aggregate|anomalies)` so the 3221-await `load_metadata_async` loop comes off the request path — is a Wave 0–enabled experiment. Post-fix coordination_failure rate is the verdict on whether the in-handler floor was the await loop (Python-fixable in-place) or the substrate-coupling floor (substrate-shaped). The signal will land on the same Wave 0 channel that surfaced the problem.

The amendment is what Wave 0 was for. The signal arrived earlier than the roadmap anticipated because step 2A was a low-risk wire and an unrelated probe converged on the same answer the channel was about to surface. Per `feedback_substrate-migration-status-quo-bias.md` ("ask 'what falsifying evidence would update you?' early"), this is exactly the falsifying-evidence shape the roadmap should fold in rather than route around.

---

## Operator-consent framing (read this first)

The operator stated 2026-04-30 (~13:30 local) and again 2026-05-03 that the goal-level destination is a "full BEAM nervous system" and expressed enthusiasm for "fully migrate." This roadmap **does not give the operator that.** It argues a hybrid architecture (BEAM for the control plane, Python for the intelligence plane) is the right shape, *not* a wholesale Python-to-Elixir rewrite of UNITARES governance MCP.

The operator should explicitly confirm or override this substitution before the roadmap is treated as binding. Drafting a roadmap that quietly translates "fully migrate" into "hybrid that keeps Python permanently" is the substrate-migration enthusiasm bias — exact mirror of the resistance bias in `feedback_substrate-migration-status-quo-bias.md`. Naming it does not absolve it; operator consent does.

**Convergent evidence behind the substitution (2026-05-03 — partially falsified 2026-05-04, see AMENDMENT block above):**

- 3-agent council (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`) on the prior draft of this roadmap rejected its diplomatic third-position framing and surfaced that Wave 1's central premise (asyncpg/anyio as live bug class) was stale. **[Falsified 2026-05-04: bug class is alive on governance-MCP path, ~60× amplification. The council's surfacing was correct against the prior draft's *wording*, but the underlying bug class is not stale.]**
- Independent third-party (Perplexity computer task, 2026-05-03): "I would not do a wholesale Python-to-Elixir/Erlang rewrite. The better path is: keep Python for ML, research code, model evaluation, data tooling, and fast iteration; move only the orchestration layer, agent supervision, long-running services, distributed coordination, queues, process lifecycles, telemetry, and fault-boundary logic onto BEAM."
- Live source check: the asyncpg/anyio bug class cited as primary motivation in earlier drafts was closed in production 2026-05-02 by PR #290 (`agents/sentinel/agent.py:413-450`); `phase-a-plan.md:347` confirms ">400 cycles since restart with zero asyncpg/anyio failures." **[Scope correction 2026-05-04: PR #290 closed it for the Sentinel-loop call site only, not at the bug-class level. The same coupling is alive on the governance-MCP request path, measured 2026-05-04.]**

If the operator wants Read B (full UNITARES rewrite in Elixir) regardless, this roadmap does not block it — but Read B requires a separate edit to `docs/ontology/beam-coordination-kernel.md` to amend the first non-goal ("Do not rewrite UNITARES in Elixir"), and that edit is the operator's call, not this document's.

## What this document is

A sequencing memo for *what BEAM does next, after the lease-plane Phase A complete on 2026-05-03 (PR #305)*. **Under v0.1 (2026-05-04), it commits to A′ as the destination** — stateful-coordinating surfaces port to BEAM, stateless-computing surfaces stay Python, the MCP SDK is an explicit external-dependency gate. v0's Read A and the §"The cut" framing below are preserved as historical record but are *not* the current destination; read the V0.1 DESTINATION block first.

It is not:

- a Phase B plan for the lease plane — that belongs to `surface-lease-plane-v0.md`;
- an amendment to `docs/ontology/beam-coordination-kernel.md` — the kernel doc's non-goal "Do not rewrite UNITARES in Elixir" remains satisfied under A′ (the compute layer stays Python; this is not a wholesale rewrite);
- an RFC for any specific port — each named wave below will get its own RFC if and when approved.

## v0.1 cut: stateful-coordinating vs stateless-computing

> **BEAM holds state and coordinates. Python computes.** *(v0.1, 2026-05-04 — supersedes v0's "Python thinks. BEAM governs." slogan. v0's framing is preserved in the historical-record §"The cut" block immediately below.)*

The v0.1 cut is per-surface, tested by ecosystem maturity rather than by static category. The test for a surface: "Does this code hold state, coordinate concurrent work, supervise tasks, or fight anyio/asyncio coupling?" If yes, it ports to BEAM. "Does this code compute over data with library dependencies that are decades-mature in Python (numerical, ML, LLM SDKs, schema validation)?" If yes, it stays Python and is called from BEAM via Ports / HTTP.

| Stays Python under v0.1 (with reason) | Moves to BEAM under v0.1 (with wave) |
|---|---|
| **MCP transport layer** — Anthropic Python SDK is upstream-first-class; no production-mature Elixir SDK exists today; see §"MCP SDK gate" for the explicit trigger that changes this | **Sentinel** (Wave 1) — fleet supervision; OTP-shaped |
| **`governance_core/`** (3,300 LOC; NumPy in `phase_aware.py` + `stability.py`) — numerical maturity | **`force=True` cleanup across remaining ~24 sites + lease-integration boundary hardening** (Wave 2) — reviewer-council-rescoped (see Wave 2 below for the rationale shift away from v0.1's original audit-pipeline framing) |
| **LLM SDK calls** (Anthropic, OpenAI) — tool-use, streaming, prompt cache ergonomics | **Handler dispatch + identity middleware + dialectic resolution coordination** (Wave 3) — the largest single port; gets its own RFC; explicitly NOT a clean cut at the `load_metadata_async` boundary (see "Cross-cutting concern" note below) |
| **Pattern analysis / calibration** — scipy/scikit-learn ecosystem | **Vigil** (post-Wave-3) — cron janitorial; substrate uniformity |
| **Pydantic v2 schemas** — declarative validation; Ecto changesets are a port-not-translate | **Chronicler** (post-Wave-3) — daily metrics; substrate uniformity |
| **KG retrieval (`src/knowledge_graph.py`, `hybrid_rrf` / `hybrid_rrf_graph` over AGE)** — see "KG retrieval placement" note below; this surface is where the 60× amplification was MEASURED, so its placement is load-bearing for v0.1's falsifying-evidence interpretation | — |
| **Watcher** — single-shot LLM pattern matcher per code edit; no coordination shape | — |
| **Hermes practice body** — research surface; solo-process; not coordination-shaped | — |
| **Pi anima-broker** — retired 2026-05-01 with measured falsifications; out of scope | — |
| **Discord dispatch** — TypeScript; different cost calculus | — |

The "stays Python under v0.1" column is **conditional**, not permanent. Specifically, the MCP transport layer is gated on external-SDK availability (§"MCP SDK gate"); the rest are gated on ecosystem-maturity comparisons that may shift over multi-year horizons (Nx maturing toward NumPy parity, Elixir LLM client libraries reaching production-grade ergonomics). v0.1's destination is A′; full Read B is conditionally-open if the gates close.

**KG retrieval placement (folded from architect council B1).** KG retrieval (`hybrid_rrf` over AGE through Postgres) is the surface where the 60× amplification was measured (see AMENDMENT block). Under v0.1 it stays Python because the AGE/Cypher integration in `src/knowledge_graph.py` is database-side (Cypher executes inside Postgres regardless of caller substrate) and the Python wrapper is a thin query layer that doesn't itself coordinate. **However:** the in-handler call sites that DO show the amplification — handler bodies that call `hybrid_rrf` or `hybrid_rrf_graph` — port to BEAM under Wave 3. The amplification on those paths is therefore expected to come down to the BEAM↔Python boundary cost (one Ports round-trip per KG call) rather than the in-handler floor's full 60×. This is a falsifiable prediction; Wave 3's exit criterion already requires "no new substrate-tax pattern at the Python-handler-body boundary" and Wave 2's Wave-0 schema extension (below) is what makes that measurement possible.

**Cross-cutting concern: `load_metadata_async(force=True)` (folded from reviewer council B2).** ~24 force=True call sites are spread across `src/mcp_handlers/dialectic/` (8: handlers.py + resolution.py + auto_resolve.py), `lifecycle/` (8: operations.py + mutation.py + resume.py + stuck.py), `support/condition_parser.py` (1), `admin/handlers.py` (1), `identity/handlers.py` (1), `agent_loop_detection.py` (1, line 601), plus other surfaces. Each is the same anti-pattern (full PG reload + 3221 sequential per-agent cache.set awaits) PR #350 removed from observe handlers, **except** that several of these legitimately need post-write read consistency (after an agent state mutation, reload to confirm). Wave 2 (v0.1-rescoped, see below) audits these site-by-site: drop force=True where the use is read-only-fleet-overview-shaped (matches PR #350's case), keep force=True where the use is post-write-consistency-shaped (matches lifecycle mutation patterns), or replace with single-agent fetch (`load_monitor_state(agent_id)` in executor) where only one agent's state needs freshness. The site-by-site audit is the Wave 2 work. Wave 3's "cleanly separable" claim survives this precisely because Wave 2 lands first — by Wave 3 the remaining force=True calls in dialectic/handlers.py et al. are either dropped or replaced with non-substrate-tax-amplified equivalents, so the Wave 3 port hits a coordination layer that's already been substrate-tax-mitigated.

## v0 historical-record cut: control plane vs intelligence plane (SUPERSEDED 2026-05-04)

> **Python thinks. BEAM governs.** *(v0 slogan, superseded by v0.1.)*

v0's organizing principle, framed by Perplexity 2026-05-03 (first session) and adopted in v0:

| Plane | v0: Stays Python (permanently) | v0: Ports to BEAM (eventually, by wave) |
|---|---|---|
| **Intelligence** | `governance_core/` (3,300 LOC; NumPy in `phase_aware.py` + `stability.py`); KG retrieval (`hybrid_rrf` over AGE); Watcher (LLM pattern matcher); Hermes practice body; paper v6/v7 corpus tooling; the dialectic engine's reasoning logic | — |
| **Control** | — | Sentinel (fleet supervision); Vigil (cron janitorial); agent lifecycle/heartbeats; identity/onboarding middleware; fault recovery (currently launchd; OTP supervision is a structural upgrade); event bus / telemetry |
| **Ambiguous** | The 31 `@mcp_tool` handlers are protocol glue + business logic, not "intelligence" — but their placement depends on the Elixir MCP server library question (see §"Read B-shaped risks"). The MCP transport layer itself sits on the control side conceptually but is gated by library maturity. | |

v0's "stays Python permanently" framing was load-bearing for v0's Read A as stable destination. **Under v0.1 the framing is superseded:** the placement of the 31 `@mcp_tool` handlers is no longer "ambiguous" pending the SDK question — it is "MCP transport stays Python until the gate closes; handler bodies port to BEAM under Wave 3 with the transport layer as a thin Python shim that proxies into BEAM after request unmarshalling." The v0 framing is preserved here so the diff between v0 and v0.1 is auditable, not lost.

## Why Read A, not Read B

### Cost (verified 2026-05-03 against `master` at `e4076657`)

- `src/` non-test Python: **83,071 LOC** across 31 `@mcp_tool` handlers in 16 modules.
- `governance_core/`: **3,300 LOC** (stays Python regardless — see §"The cut").
- Test files: **330** in `tests/`.
- Lease plane Elixir (already-shipped, for comparison): **2,798 LOC**.

A Read B port is roughly 30× the lease plane's volume *by raw LOC*, with the caveat that the comparison denominator is imperfect: not all 83K lines are coordination-runtime, and not all 2.8K Elixir lines are pure protocol. Reviewer council finding: "the comparative ratio is real but the denominator is not surgical" — treat 30× as an order-of-magnitude anchor, not a precise multiplier.

### Hidden costs (Read B-shaped risks)

1. **Elixir MCP server library.** Anthropic ships official Python and TypeScript SDKs; an Elixir SDK does not exist as of this writing. Read B requires either hand-rolling JSON-RPC over stdio + HTTP or adopting a community library with unknown maturity. Either path is weeks of work and the largest single risk surface.
2. **AGE / Cypher from Elixir.** Postgrex can call AGE through Postgres (`SELECT * FROM cypher(...)`), but every retrieval path in `src/knowledge_graph.py` and `src/mcp_handlers/knowledge/handlers.py` (verified to use `hybrid_rrf` + `hybrid_rrf_graph`) needs reimplementation.
3. **Identity / onboarding coupling.** CLAUDE.md flags this as a single coupled writer surface across `src/mcp_handlers/identity/`, middleware, schemas, and shared docs. Porting without breaking the live agent fleet is delicate.
4. **REST surface preservation (reviewer council finding the original draft missed).** Watcher, Sentinel, Vigil, the SDK, and external partners all hit governance MCP via REST endpoints (e.g. `post_finding` → `/api/findings`). Read B must replicate that REST surface byte-for-byte at the boundary, or every Python agent silently breaks. This is a contract the doc must preserve regardless of the runtime underneath, and the work is not free.
5. **Test corpus.** 330 test files, much of it pytest-fixture-shaped. ExUnit equivalents must be rebuilt before declaring the port "done." A meaningful fraction of the port effort.
6. **Runway opportunity cost.** Realistic timeline 3–6 months of focused work. Paper v6 / v7 corpus accumulation, Lumen evolution, dispatch refinement, fellowship, fleet maintenance all slow during the window.

### Reversibility and cognitive surface area (architect council findings)

- **Exit cost is not symmetric with entry cost.** Each Python service ported away makes the residual Python core easier to argue against keeping. Read A pretends to be reversible; in practice each wave shifts the operator's mental defaults toward Read B. The roadmap mitigates this by explicit "stays Python permanently" categorization, not by reassurance.
- **Two-substrate cognitive tax.** Running Elixir + Python indefinitely is itself a real cost: two language ecosystems, two CI/test pipelines, two on-call mental models, two flavors of dependency-pinning and security patching. Read A's stable-destination framing accepts this tax explicitly. Read B's "single substrate eventually" framing is comforting but the evidence does not support it (see §"What's *not* a Read B trigger").

## MCP SDK gate (v0.1)

A′ has one named external-dependency gate. v0.1 commits to A′ as destination *with* the gate explicitly open; it does not pre-commit to closing the gate.

**What the gate is.** The Anthropic Python MCP SDK is the load-bearing reason the MCP transport layer stays Python under A′. Re-implementing or maintaining a parallel Elixir MCP server is a non-trivial protocol-implementation cost that does not exist today and tracks Anthropic's ongoing protocol evolution.

**What closes the gate.** Any one of:

1. **Anthropic ships an official Elixir MCP SDK.** Tracked at the Anthropic SDK landscape (currently Python and TypeScript only).
2. **A community Elixir MCP server library reaches production-grade maturity.** Indicators: stable API across at least 3 minor protocol revisions; production deployments with public reference; passing the upstream MCP conformance tests (or the equivalent test suite the community has converged on); active maintainership.
3. **A credible Elixir MCP hand-roll spike completes (folded from architect council B2 — tightened to match the NOT-closure list).** A focused implementation effort that lands within bounded time (≤ 2 weeks of focused-engineer time) AND **supports the WHOLE MCP protocol surface** — tool-calls, notifications, prompts, sampling, resources, and tool-use streaming — without protocol-correctness regressions verified against the upstream `modelcontextprotocol/python-sdk` test suite (or community-converged equivalent). A spike that handles the easy 70% (tool-calls only) and stalls on streaming/sampling does NOT close the gate; the NOT-closure list below explicitly catches this case. The spike is the only path that moves the gate without external dependency, and is correspondingly the largest single risk surface; it is not a casual experiment.

**What the gate does NOT close on.**

- Operator enthusiasm. Per `feedback_substrate-migration-status-quo-bias.md`, both poles of the bias are wrong. "I want to fully migrate" is data about operator state, not about whether the SDK gap is bridged.
- A single working-prototype demo of one tool call over hand-rolled JSON-RPC. The MCP protocol surface is wider than tool-calls (notifications, prompts, sampling, resources, tool-use streaming) and the hand-roll either supports the whole surface or it doesn't.
- Apparent stalls in upstream Python SDK evolution. The Python SDK can be slow without that being grounds for replacing it.

**What happens if the gate closes.**

The MCP transport layer becomes the natural Wave-N port and Read B comes onto the table for explicit operator decision via the kernel-doc non-goal amendment process v0 prescribed (it does *not* port silently). Until the gate closes, v0.1's destination remains A′ with the MCP transport layer staying Python.

**What happens if the gate stays open indefinitely.**

A′ is stable destination. The BEAM↔Python boundary at the MCP transport layer accrues some glue (Ports protocol definitions, error translation, version-pinning) but the boundary is **bounded** — one well-understood interface — unlike the unbounded substrate-coupling tax that A′ eliminates from the coordination surfaces. This is the case for A′ as steady state, not transition.

## A′ is the destination (under v0.1; supersedes "Read A is a stable destination")

**Under v0.1, A′ is the destination.** v0's "Read A is a stable destination" is preserved below as historical record but is superseded; the destination is no longer Read A.

The original v0 draft framed Read C as "incremental ratchet" to an open-but-deferred Read B. The architect council on v0 found this collapses to Read A + governance, and that the "mature systems grow new substrate alongside until the old part becomes vestigial" claim was rhetorical comfort, not principle. Mature systems also fossilize around foreign substrate (C extensions in Python, JNI in JVM stacks, CGI-era PHP under modern Rails); the conditions distinguishing those outcomes are not generally known, and assuming the favorable one is bias.

v0 dropped the trichotomy and committed to a binary: Read A is the destination. v0.1 retains the binary commitment-discipline but updates the destination: **A′ is the destination.** Read B is open only via explicit operator decision AND closure of the §"MCP SDK gate" AND a separate edit to the kernel doc — not via roadmap drift, not via single-trigger enthusiasm.

### Failure mode named explicitly: integration glue ossifies

The honest failure mode of Read A is that the boundary between BEAM control plane and Python intelligence plane accumulates glue (proto definitions, REST contracts, version-pinned client libraries, error-translation layers) until the integration tax exceeds the original migration cost. The mitigation is:

- **Use Ports / HTTP / gRPC / Redis streams for BEAM↔Python interop.** Treat Python services as supervised external processes.
- **Do NOT use Pythonx NIFs** — embedded CPython runs in the same OS process as BEAM via NIFs and breaks the supervision/isolation guarantees that make BEAM worth the migration in the first place. Per Pythonx's own docs (cited via Perplexity 2026-05-03): for managing multiple Python programs, `System.cmd/3` or Ports is the better isolation model.
- **Keep boundary contracts narrow and versioned.** The narrower the contract, the cheaper the glue. Sentinel-on-BEAM should call governance MCP via the same REST surface every other agent uses, not via Pythonx embed and not via a new Elixir-only wire protocol.

## Wave 0 prerequisite: incident-rate instrumentation

The original draft cited "asyncpg/anyio incident rate trends up/down" as a falsification trigger. **There is no incident-rate measurement infrastructure in the repo.** Reviewer council finding (confirmed): no metrics-series row, no Chronicler schema, no structured event class for coordination-class failures. The triggers in the original draft were performative.

Wave 0 of this roadmap, before any port: emit structured events on coordination-class failures (asyncpg connect errors, anyio task-group cancellations, executor pool exhaustion, MCP handler timeouts) and persist them in a Chronicler-readable form. Without Wave 0, no later wave's "exit criterion" can be honestly evaluated and no Read B trigger can fire on evidence rather than vibes.

Wave 0 is small: a structured-event emitter in the existing Python services + a Chronicler row schema + a dashboard panel. Days of work, not weeks.

### Event envelope (defined upfront, not evolved)

Per Perplexity 2026-05-03 (second session): schemas are cheaper to design before code than after. Wave 0 commits to a stable JSONB envelope with these required fields, not ad-hoc structured logs:

| Field | Type | Required | Purpose |
|---|---|---|---|
| `event_id` | UUID | yes | replay/dedup key |
| `timestamp` | ISO 8601 UTC | yes | ordering and audit |
| `service` | enum (`sentinel`, `governance_mcp`, `lease_plane`, `vigil`, `chronicler`, `watcher`) | yes | originator |
| `event_type` | dotted enum (`coordination_failure.asyncpg_connect_error`, `coordination_failure.anyio_cancellation`, `coordination_failure.executor_pool_exhaustion`, `coordination_failure.mcp_handler_timeout`, …) | yes | category — extensible by namespace, never by ad-hoc string |
| `agent_id` | UNITARES UUID | optional | when the event is agent-attributable |
| `payload` | JSONB | yes | event-type-specific structure (defined per event_type, not free-form) |
| `context` | JSONB | yes | `git_commit`, `service_pid`, `running_since`, `host` — facts about the emitter, not the event |

The envelope persists in a single `audit.coordination_events` table (not per-service tables — single replay surface). Wave 0's Chronicler row schema is this envelope's projection into `metrics.series`.

Stability discipline: `event_type` extends by adding new dotted namespaces, never by reusing or renaming existing ones. `payload` shape per `event_type` is documented at the time the event_type lands. This is the contract Wave 1+ will rely on; getting it ad-hoc and refactoring later is the avoidable mistake.

## Wave 1 — Sentinel (re-justified on substrate-fit grounds)

### Why first — the honest motivation

The earlier draft pitched Sentinel-on-BEAM as the cure for the asyncpg/anyio bug class. **That motivation is *not* dead — see AMENDMENT 2026-05-04 at top of doc.** PR #290 closed the CONCERN at one call site (`agents/sentinel/agent.py:413-450` runs `asyncio.run(asyncio.wait_for(poll_forced_release_alarms(...), 30s))` inside a `loop.run_in_executor` call; `phase-a-plan.md:347` records ">400 cycles since restart with zero asyncpg/anyio failures"), but the same bug class is alive on the governance-MCP request path with ~60× amplification (measured 2026-05-04). For Wave 1 specifically, the Sentinel-loop call site IS mitigated, so the original "structural fit, not bug fix" reframing below remains the right argument *for Sentinel itself* — but it should not be read as evidence that the bug class is closed in the system.

Sentinel-on-BEAM's real motivation (with the original pitch scoped, not retired):

1. **Substrate fit, not bug fix.** A continuous fleet monitor with rule-based anomaly detection over event streams *is* the GenServer-per-rule-under-DynamicSupervisor shape. The Python implementation works (post-PR-#290), but the structural fit argues OTP supervision will hold under classes of failure the current mitigation pattern cannot cover (executor thread-pool exhaustion at sustained DB outage; cascading rule failure; alarm-handler crash without restart policy).
2. **Launchd → OTP supervision is a real upgrade.** Launchd restarts a crashed process; OTP can isolate failures within a process, restart subtrees, and apply explicit restart strategies. For a fleet monitor whose individual rules can fail independently, this is structurally better fault containment than what launchd offers.
3. **Second proof of the Postgrex pattern.** The lease plane proved Elixir/OTP can talk to the same Postgres database the Python services use without coordination pathology. Sentinel-on-BEAM tests whether that pattern generalizes to a service that *consumes* fleet events rather than *originates* coordination events.
4. **Smallest control-plane Python service.** Lower port cost than Vigil (cron infrastructure to redo) or governance MCP middleware (deeply coupled). Reasonable first wave.

### Out of scope for this roadmap

The Wave 1 RFC is a separate document. Roadmap-level: the alarm rules, the asyncpg-using probes, telemetry to UNITARES, launchd-managed lifecycle. Reuses lease-plane patterns (Postgrex, bearer-token auth from `~/.config/cirwel/secrets.env`).

### Exit criterion (gated by Wave 0)

Sentinel-on-BEAM has been the production fleet monitor for **≥ 14 days continuous** with:

- zero coordination-class incidents in the Wave-0 instrumentation feed (not "trends down" — zero, attributable);
- alarm rule parity with the Python implementation (every alarm fires the same way, verified by the existing `tests/test_sentinel_*` suite re-pointed at the BEAM endpoint);
- supervision tree absorbs at least one induced fault (kill a worker, supervisor restarts, no manual intervention);
- the operator does not declare success on enthusiasm — the 14-day window and the Wave 0 incident-feed must both hold before Wave 1 closes.

The last bullet is the architect council's stop-sign #1, promoted from a footnote into the exit criterion.

## Wave 2 — under v0.1 (REVISED post-council): force=True cleanup + lease-integration boundary hardening + Wave 0 schema extension

**v0.1 supersedes v0's "deferred pending Wave 1 evidence."** The original v0.1 draft framed Wave 2 as "audit pipeline + lease integration"; the reviewer council BLOCKed this on a factual ground — `audit.events` is already fire-and-forget by design (`src/audit_log.py:519-522` docstring: "Postgres persistence is intentionally fire-and-forget … keeping audit logging off latency-sensitive handler paths"), so the "substrate-coupling on highest-cardinality surface" justification did not actually apply to that path. The dedicated `audit.coordination_events` table is empty and the production wire deliberately routes around it. Audit-pipeline-as-Wave-2 was arguing against a problem that has already been mitigated differently.

**Revised Wave 2 scope (folded from reviewer council B1 + C3, both lanes):** the actually-highest-volume coordination surface still under substrate-tax was the ~24 `force=True` call sites in dialectic, lifecycle, admin, identity, support, and agent_loop_detection handlers. PR #350 dropped force=True from 6 observe sub-handlers; PR #354 then audited the remaining 19 sites, dropped force=True from 18, kept 1 TTL-gated admin refresh with an explicit comment, and pinned the invariant in `tests/test_force_reload_audit.py`. The non-force-reload Wave 2 work remains: lease-integration boundary hardening, Wave 0 schema extension, and the `audit.coordination_events` routing fix.

### Wave 2 scope

1. **`force=True` cleanup across all remaining ~24 sites — shipped by PR #354 (`297bf4f4`).** The audit covered `src/mcp_handlers/dialectic/handlers.py`, `dialectic/resolution.py`, `dialectic/auto_resolve.py`, `lifecycle/operations.py`, `lifecycle/mutation.py`, `lifecycle/resume.py`, `support/condition_parser.py`, `admin/handlers.py`, `identity/handlers.py`, and `agent_loop_detection.py`. Future sites still receive one of three treatments:
   - **Drop force=True** (PR #350 pattern) where the use is read-only-fleet-overview-shaped — the in-memory cache is fresh enough.
   - **Replace with `load_monitor_state(agent_id)` in executor** where only one agent's state needs freshness post-mutation. Cheaper than a fleet reload.
   - **Keep force=True with explicit comment justification** where the use is post-write-consistency-shaped (mutation handler reloading to confirm the write landed) AND the consistency requirement is real (i.e., the next read MUST see the just-written state). These are the legitimate cache-coherence patterns. The Wave 2 audit makes them explicit.
2. **Lease-integration boundary hardening.** Wave 1's Sentinel-on-BEAM speaks to governance MCP via REST. Wave 2 hardens that boundary — versioned contracts, error translation, supervised health — before Wave 3's larger handler-dispatch port takes the boundary as load-bearing. (This survives the architect council C1 ordering question because Wave 3 will reuse Wave 2's REST-contract work, just on the BEAM side after handler dispatch ports; the contract definition itself is reusable.)
3. **Wave 0 schema extension** (folded from architect council C4): add the `coordination_failure.beam_python_boundary.*` event_type namespace before Wave 3, so Wave 3's exit criterion #3 ("no new substrate-tax pattern at the Python-handler-body boundary") is measurable. Per v0's "Stability discipline" rule, this extends by adding a new dotted namespace, never by reusing existing ones.
4. **Tactical: `audit.coordination_events` routing fix.** The dedicated table exists with the correct schema but is empty (events go to `audit.events` via `coordination_failure_emit.py`). Dual-writing to both tables — without removing the existing audit.events path — restores the dedicated replay surface v0's Wave 0 envelope specified, without breaking the Wave 1 exit criterion's existing query. This is a Wave 2 task only because it's adjacent to the boundary work; could ship sooner if convenient.

### Why this scope

The volume argument from the original draft survives the rescope but lands on the right surface: ~24 sites × per-call cost (~16s blocking) is the actual coordination-tax cardinality today, not the audit writer. The Wave 0 channel will show whether Wave 2 closes the substrate-tax surface or surfaces a residual that's distinct from the force-reload pattern (substrate-coupling at a smaller cardinality). Either outcome informs Wave 3.

### Exit criterion (gated by Wave 1 + Wave 0)

- Wave 1 has closed (its 14-day window held with zero coordination-class incidents).
- All ~24 force=True sites have been audited and treated; the remaining force=True calls in master have explicit-comment justifications matching one of the documented use cases.
- Wave 0 schema extension `coordination_failure.beam_python_boundary.*` is live in `audit.events` / `audit.coordination_events` AND dual-writing is operational (architect C4 prerequisite).
- Wave 0 channel shows the 6 observe + 2 list_agents bystander timeouts that motivated PR #350 do not recur, AND no new force-reload-shaped events emerge from non-observe handlers, AND no new event_type pattern emerges that isn't already in the envelope (per "Stability discipline").
- Lease-integration boundary has absorbed at least one BEAM-side restart and one Python-side restart with no event loss attributable to the boundary (induced fault, observed, no manual intervention).

If the post-Wave-2 channel surfaces the same 60× amplification on a *different* surface that has nothing to do with force-reload — e.g., on knowledge graph reads inside a handler that doesn't touch metadata — that is the strongest possible confirmation of the substrate-coupling thesis (because the Python-fixable in-place hypothesis is then conclusively excluded), and Wave 3's BEAM motivation strengthens further.

## Wave 3 — under v0.1: handler dispatch + identity middleware + dialectic resolution

**v0.1 names Wave 3 explicitly.** This is the largest single port — the governance MCP handler dispatch layer (`src/mcp_handlers/` glue, identity middleware, dialectic resolution coordination). It gets its own RFC and full council passes; this section is roadmap-level scope only.

### Roadmap-level scope (the RFC will detail)

- Handler dispatch (the @mcp_tool decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per §"MCP SDK gate") and proxies to BEAM after request unmarshalling.
- Identity middleware (`src/mcp_handlers/middleware/identity_step.py`, the session-context contextvar chain, agent_id resolution, label resolution) ports to BEAM. This is the largest single coordination surface in governance MCP today and the highest-leverage substrate-tax elimination.
- Dialectic resolution (`src/mcp_handlers/dialectic/`) ports to BEAM. The dialectic engine's *reasoning logic* — what makes a thesis converge, the dialectic-knowledge-architect's substantive work — stays Python (it's compute, not coordination) and is called from BEAM. The coordination layer (session lifecycle, quorum tracking, condition resolution, audit emission) ports.
- Out of scope: `governance_core/`, Watcher, the LLM SDK call paths inside handlers (those stay Python and are called from BEAM via Ports).

### Exit criterion

- Wave 2 has closed (its exit criteria above all hold).
- Handler dispatch on BEAM has served production governance MCP traffic for ≥ 21 days continuous (longer window than prior waves because this is the largest blast-radius port).
- Wave 0 channel shows zero coordination-class incidents attributable to handler dispatch over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
- Operator-led behavioral parity test: existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff (REST contract preserved byte-for-byte, response shapes identical, error codes identical).

## Post-Wave-3 candidates (under v0.1, deferred)

After Wave 3 closes, what's left in Python is genuinely compute (governance_core math, LLM SDK calls, pattern analysis) plus the MCP transport layer (gated externally). Wave 2-3 will have produced enough Wave 0 channel data to know whether further porting of any kind is warranted. Candidates:

- **Vigil** — substrate uniformity for the cron-driven janitorial agent. Easy port if Wave 2-3 has solidified the patterns.
- **Chronicler** — substrate uniformity for the daily metrics agent.
- **MCP transport layer** — only if §"MCP SDK gate" closes.
- **A new BEAM service that fills a gap discovered during Waves 1-3** — only if real.
- **Pause.** Solo-founder runway is finite; closing A′ at Wave-3-exit and stabilizing is a real option.

The decision belongs to the operator at Wave 3 exit, with Wave 0's accumulated incident-rate data and Wave 2-3's parity evidence in hand.

## Out of scope for this roadmap

Stays Python (and not because Read A might "eventually" port them — the categorization is structural):

- **`governance_core/`** — intelligence plane. NumPy is used in `governance_core/phase_aware.py` and `governance_core/stability.py`; the dynamics path itself is stdlib. Either way, this is math, not coordination.
- **Watcher** — single-shot LLM pattern matcher invoked per code edit. No coordination shape; OTP supervision adds nothing. (Note: even staying Python, Watcher hits governance MCP REST endpoints — see §"Hidden costs" #4.)
- **Hermes practice body** — research/practice surface per `feedback_violist-poker-asymmetry` and `Mnemos_07d0f9c7`. Solo-process. Stays Python.
- **Pi anima-broker** — measured falsification 2026-05-01 (S1 idle RSS 123.7 MB falsified the §8.2 prediction; S6 distribution-win 50–75% on the 70% gate). See `docs/proposals/anima-broker-beam-port-v0.md`. Re-open requires operator-authorized "Lumen as appliance OS" reframe or a second Pi joining the fleet, not enthusiasm.
- **Discord dispatch bot** — TypeScript, not Python. Coordination-shaped (per-thread sessions, shared `.dispatch-sessions.json`) but not suffering Python's coordination class. Different cost calculus; port only if it starts hurting.
- **Data plane (Postgres + AGE schema, including `lease_plane`)** — the migration creates schema `lease_plane`, not `coordination`. Postgrex talks to it. The KG retrieval rebuild (`UNITARES_KNOWLEDGE_BACKEND=age`, `hybrid_rrf` / `hybrid_rrf_graph`) does not change.

## What's *not* a full-Read-B trigger (under v0.1)

**v0.1 update:** the v0 list below was framed as "Read B trigger" when the v0 destination was Read A. Under v0.1's A′ destination, the trigger list is the path *past* A′ to full Read B (porting the MCP transport layer and the remaining stateless-compute surfaces). The list still applies — and applies more sharply, because A′ already moves the surfaces v0's Read A kept Python.

Substrate-migration enthusiasm is the *prompt* to write this roadmap, not *evidence* that justifies escalation. Per `feedback_substrate-migration-status-quo-bias`, the bias cuts both ways: resistance and enthusiasm are symmetrical errors. Operator stating "I'm all about BEAM" is data about operator state, not about whether full-Read-B's costs are warranted.

What *would* be a full-Read-B trigger (kept here so the question is honestly open, not buried):

1. Wave 0 incident-rate instrumentation runs for ≥ 60 days post-A′-completion (i.e., post-Wave-3) and shows the coordination-class bug rate is *not* zero in the remaining Python compute surfaces. (Under A′ the remaining Python surfaces are stateless compute — `governance_core/` math, LLM SDK calls, pattern analysis — so a non-zero rate at the BEAM↔Python boundary itself would be the signal that the boundary tax is unbounded after all.)
2. The §"MCP SDK gate" closes (Anthropic ships an Elixir SDK, OR a community library reaches production maturity, OR a credible hand-roll spike completes).
3. A second runtime consumer materializes that wants OTP-native APIs (a non-Python harness, a partner integration).
4. Operator runway permits the additional port effort without sacrificing paper / fellowship / Lumen / dispatch.

Two or more triggers → operator decides whether to amend the kernel doc non-goal "Do not rewrite UNITARES in Elixir" (which A′ does NOT amend, but full Read B does). Single trigger → re-evaluate the roadmap, not the kernel doc.

## What this roadmap deliberately does not adopt from Perplexity (second session, 2026-05-03)

The second Perplexity output proposed elements that look reasonable in generic-architecture terms but are wrong for UNITARES specifically. Documenting the rejections so future agents reading this doc don't reintroduce them:

1. **Coherence-as-runtime-control-signal — REJECTED.** Perplexity #2 framed coherence as a generic governance metric ("entropy of tool-call distribution, drift from declared task objective, repeated failed retries…") with BEAM-side reactive control ("decide whether to continue, slow down, replan, isolate, or terminate"). UNITARES coherence is C(V, Theta), a thermodynamic state-vector property defined in `governance_core/` and the v6 paper — descriptive, not gating. Treating it as a reactive runtime threshold is the buzzword reading the v6.x corpus pushed back against. See `project_unitares-vocabulary-mismatch.md`.

2. **BEAM-owned `AgentRegistry` / `PolicyEngine` / `CoherenceMonitor` / `AuditLogWriter` — REJECTED.** Despite Perplexity #2's "not a rewrite" framing, moving authority over agent-registry, policy decisions, coherence monitoring, and audit writing to BEAM *is* Read B in disguise — those are the governance MCP's current responsibilities. Adopting this structure would amend the kernel doc's first non-goal ("Do not rewrite UNITARES in Elixir"), which requires a separate operator-authorized edit to that doc, not roadmap drift.

3. **Phase-five distributed BEAM nodes (Mac + Pi) — REJECTED.** Re-proposes the **retired** anima-broker BEAM port. S1 measured 123.7 MB idle RSS against a falsifier; S6 distribution-win was 50–75% on a 70% gate; retired 2026-05-01 (PR #279). Re-open requires operator-authorized "Lumen as appliance OS" reframe or a second Pi joining the fleet, per `anima-broker-beam-port-v0.md` §"Re-open conditions." Not enthusiasm.

4. **SQLite-or-Postgres audit start — REJECTED.** UNITARES has one Postgres database (governance), one location, by standing rule (CLAUDE.md "Database" section, `feedback`-anchored). The Wave 0 envelope persists in `audit.coordination_events` in the existing governance Postgres. SQLite reintroduces the second-instance anti-pattern the operator has explicitly forbidden.

These rejections are documented at envelope-table granularity so the boundary between "Perplexity #2 worth keeping" (slogan, event envelope, lifecycle state machine) and "Perplexity #2 generic-architecture overreach" is auditable, not lost in the diff.

## Stop signs

Pause and request review if a roadmap revision proposes any of these:

- silently amending the kernel doc's non-goals without an explicit operator decision and a separate edit to that doc;
- **(v0.1 stop sign)** re-cutting at "control plane vs intelligence plane" or any phrasing that places governance MCP coordination on the Python-permanent side — that is v0's superseded framing; v0.1's cut is "stateful-coordinating vs stateless-computing" and reverting requires explicit operator authorization;
- **(v0.1 stop sign, folded from architect council C2)** reclassifying any specific surface from stateful-coordinating to stateless-computing (or vice versa) — e.g., a future revision arguing "dialectic resolution turns out to be mostly LLM calls, so let's leave it Python" — without operator authorization to amend the v0.1 cut table itself. The per-surface table is load-bearing, not just the cut name. Implicit drift via individual-row reclassification has the same end-state as explicit cut reversion and is correspondingly defended.
- **(v0.1 stop sign, folded from reviewer council N1)** treating PR #350 as having closed the `force=True` problem system-wide. PR #350 closed it for 6 observe sub-handlers ONLY. The remaining ~24 sites (dialectic, lifecycle, admin, identity, support, agent_loop_detection) are explicit Wave 2 scope; treating them as already-handled is exactly the scope-creep this stop sign exists to catch.
- **(v0.1 stop sign)** treating §"MCP SDK gate" closure as having occurred without one of the three named conditions actually being met (Anthropic SDK, community library at production maturity, or completed hand-roll spike covering the WHOLE protocol surface per condition #3 as tightened) — partial demos, subset spikes, and stalls in upstream are explicitly not gate-closure;
- collapsing A′ back into "incremental ratchet to Read B" language — under v0.1 A′ is the destination, not a way-station;
- collapsing v0.1 back into v0's Read A destination via "we found the substrate tax is not so bad after all" — the falsifying evidence (60× amplification) is a measurement, not a sentiment;
- declaring any wave successful without **both** the per-wave window AND the Wave 0 incident-rate evidence;
- treating operator enthusiasm as substitute for §"What *would* be a full-Read-B trigger" evidence;
- pre-committing wave N+1 before wave N has shipped and its window closed;
- a full-Read-B spike has been "proposed but not scheduled" for > 90 days (drift by deferral; named gate, not ambient sentiment);
- including `governance_core/` math, Watcher, Pi-side anima-broker, Hermes practice body, Discord dispatch, or the data plane in any wave without separate operator approval;
- Pythonx / NIF embed proposed as the BEAM↔Python boundary instead of Ports / HTTP / gRPC / Redis streams.

## Re-evaluation cadence

Revised:

- after each wave ships and its window closes;
- if Wave 0 incident-rate data shifts materially in either direction;
- **after PR #350's post-fix data lands** (per V0.1 DESTINATION conditionality block) — verdict on whether observe timeouts close (Python-fixable) or persist (substrate-shaped) is what binds or reverts v0.1's destination commitment;
- if the Elixir MCP server library landscape changes (community library lands, Anthropic adds an SDK, or a credible whole-surface hand-roll spike completes);
- if a full-Read-B trigger fires.

**SDK gate monitoring (folded from reviewer council C1).** The MCP SDK gate cannot close without the operator noticing if no one is watching. v0.1 names the operator as the cadence owner for the SDK landscape check (no automated equivalent exists, by design — this is an external-ecosystem question, not a runtime metric). Operator commits to a quarterly check on Hex.pm + GitHub for new Elixir MCP libraries against the gate criteria, and to monitoring `modelcontextprotocol/elixir-sdk` 404 → exists transitions. Currently-tracked: `ex_mcp` v0.9.1 (active 2026-04-28, 14 stars — below production-mature bar), `hermes_mcp` v0.14.1 (dormant since 2025-08, not tracked further unless revived). If a third library appears or one of these crosses the production-maturity bar, the gate closure check fires.

Revisions land as `beam-footprint-roadmap-v0.1.md`, `v0.2.md`, etc.; full v1 when Wave 1 closes and the question shape itself updates.

## Relationship to other docs

| Doc | What it owns | Relationship to this roadmap |
|---|---|---|
| `docs/ontology/beam-coordination-kernel.md` | Integration framing, non-goals (incl. "Do not rewrite UNITARES in Elixir"), OTP process shape, lease-plane Phase 0–4 sequence | Roadmap respects non-goals; expansion past lease plane sits *outside* its scope |
| `docs/proposals/surface-lease-plane-v0.md` | Lease-plane contract spec, Phase A → Phase B gates | Roadmap's Wave 1 (Sentinel) is downstream of Phase A complete; not a Phase B item |
| `docs/proposals/surface-lease-plane-phase-a-plan.md` | PR-by-PR Phase A breakdown with status; the Sentinel asyncpg CONCERN closed at line 347 | Source of truth for Wave 1's "asyncpg fixed" claim |
| `docs/proposals/plexus-scope.md` | Plexus product/boundary name; what Plexus v1 owns and does not own | Roadmap is about runtime substrate, not lease semantics; orthogonal |
| `docs/proposals/anima-broker-beam-port-v0.md` (retired) | Pi-side BEAM port; retired with measured falsifications 2026-05-01 | Roadmap explicitly excludes Pi from scope |

## Sources of the substitution argument (v0)

- Council pass on draft v0 (2026-05-03), three agents in parallel:
  - `dialectic-knowledge-architect`: surfaced the operator-consent issue, the trichotomy collapse, the falsification asymmetry, the missing reversibility/cognitive-surface category, and the rhetorical-comfort claim.
  - `feature-dev:code-reviewer`: surfaced the dead Wave 1 motivation (Sentinel asyncpg fixed), the missing measurement infrastructure, the imperfect 30× denominator, the missing Watcher REST coupling.
  - `live-verifier`: factual corrections (test count 330 not 329, PR #305 merged 2026-05-03 not -05-02, schema `lease_plane` not `coordination`, "60 MiB target" absent from anima-broker doc, scipy unverified, NumPy verified in `phase_aware.py` + `stability.py`).
- Independent third-party (Perplexity computer task, 2026-05-03): control-plane / intelligence-plane cut; Ports-not-NIFs interop discipline; "hybrid architecture first, not full rewrite."
- Direct source verification (v0 drafter, 2026-05-03): `agents/sentinel/agent.py:413-450` confirms the asyncpg/anyio mitigation; `phase-a-plan.md:347` confirms ">400 cycles, zero failures."

## Sources of the v0.1 destination shift (2026-05-04)

- **2026-05-04 falsifying measurement** (parallel Claude session): governance-MCP request path KG calls 21–71ms standalone vs ~4,464ms in-handler (~60× amplification). See AMENDMENT block above. The measurement falsifies v0's load-bearing premise that PR #290 closed the asyncpg/anyio bug class system-wide.
- **2026-05-04 Wave 0 channel evidence** (PRs #342 + #345 + #348 + #350 all merged 2026-05-04/05): 6 `coordination_failure.mcp_handler_timeout.tool_decorator` events on observe / list_agents over the ~10.75–14.5h window after #345 merged, cascade pairs at 15:15:00.88/.93 and 18:57:37.93/41.99 MDT, 22.6s elapsed-past-15s outlier (`elapsed_s=22.615` at 18:18:20 MDT) — substrate-coupling fingerprint, captured by the channel v0 prescribed for exactly this question. Schema-routing drift: events are in `audit.events` filtered by `event_type LIKE 'coordination_failure.%'`, NOT in the dedicated `audit.coordination_events` table (which exists with the correct schema but is empty). Routing fix is Wave 2 scope per v0.1.
- **2026-05-04 operator/agent dialogue** (this session): operator question "is hybrid best? what do we lose in python if we go full on?" prompted a per-surface ecosystem-maturity audit. The audit produced the cut shift from "control vs intelligence" (v0) to "stateful-coordinating vs stateless-computing" (v0.1) by identifying that the v0 cut placed governance MCP coordination on the wrong side of its own test. Operator decision recorded after seeing the per-surface table.
- **Council pass on v0.1 (2026-05-04, three agents in parallel; same precedent as v0):**
  - `dialectic-knowledge-architect`: 2 BLOCK + 4 CONCERN + 3 DRIFT + 4 NIT — addressed inline. B1 (KG retrieval cut placement undefined) folded as new "KG retrieval placement" note in the cut section. B2 (MCP SDK gate condition #3 contradicted NOT-closure list — subset spike vs whole-surface) folded by tightening condition #3 to "whole protocol surface" with explicit cross-reference to the NOT-closure bullet. C1 (Wave 2 ordering — lease-integration redundancy with Wave 3) addressed by Wave 2 rescope (force=True cleanup primary; lease-integration boundary work stays but is reusable in Wave 3, not redone). C2 (drift via per-surface reclassification) folded as new stop sign. C3 (enthusiasm-pole bias on PR #350 outcome) folded as the Conditionality block in V0.1 DESTINATION — destination is conditional on PR #350's post-fix data. C4 (Wave 0 schema gap for BEAM↔Python boundary tax) folded as Wave 2 prerequisite #3. DRIFTs and NITs corrected inline.
  - `feature-dev:code-reviewer`: 2 BLOCK + 3 CONCERN + 2 DRIFT + 2 NIT — addressed inline. B1 (Wave 2 audit-pipeline justification was wrong; `audit.events` is fire-and-forget by design, substrate-coupling argument doesn't apply) folded by Wave 2 rescope (audit pipeline dropped; force=True cleanup substituted as the actual highest-volume coordination surface). B2 (Wave 3 separability undermined by ~24 force=True calls in the coordination layer) folded as new "Cross-cutting concern" note in the cut section, with the explicit Wave 2 ordering (force=True cleanup before Wave 3 ports the coordination layer). C1 (SDK gate monitoring is passive) folded into Re-evaluation cadence as named-owner (operator) + quarterly check + currently-tracked-libraries. C2 (Wave 1 exit criterion ambiguity post-Wave-2) folded into Wave 2's tactical "audit.coordination_events routing fix" item — dual-write preserves the Wave 1 query while restoring the dedicated table. C3 (force=True scope much larger than PR #350) folded as new stop sign + explicit Wave 2 enumeration (~24 sites, file by file). DRIFTs (test count drift, REST surface mechanism deferred to Wave 3 RFC) noted; D1 explicitly stated as architectural-sketch-deferred-to-RFC, D2 stays in v0 historical section per drafter discipline.
  - `live-verifier`: 7 VERIFIED + 6 DRIFT + 0 REFUTED + 1 SOURCE_ONLY — DRIFTs corrected inline (8.5h → 10.75–14.5h window; cascade timestamp .83 → .88; PR #350 status open → merged; phase-a-plan.md citation 347 → 349; sentinel citation 413-450 → helper at 413-454, run_in_executor at 668; force=True scope adds agent_loop_detection.py:601 + condition_parser.py:1; audit.coordination_events table empty / events route to audit.events). The 60× number is verified at order-of-magnitude (different specific call paths give 8–253×; "60×" is plausible for the call the prior session measured but not pinned to a specific handler). The "100% concentration on observe/list_agents" claim was true at first-window; dataset has since grown to 13 events with additional event types (~46% concentration now) — folded as "first-window pattern, not permanent."

All v0.1 council BLOCKs are addressable via folding without architectural revisit; none re-falsify A′ as destination. Wave 2 rescope is the largest single v0.1 change and is structural, not text-tightening.

The v0.1 destination survives if the operator confirms the rescope explicitly AND PR #350's post-fix data confirms the substrate-coupling reading per the Conditionality block above. Otherwise v0.2 reverts the destination to a question, not a plan.
