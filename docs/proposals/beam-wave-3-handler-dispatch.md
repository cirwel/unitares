# Wave 3 RFC: handler dispatch + identity middleware + dialectic resolution → BEAM

**Status:** DEFERRED per parent-roadmap V0.3.4 RESOLUTION (2026-06-11) — the v0.3.2 re-litigation resolved as (α): no redraft and no Wave-3-specific implementation before the §14 prereq 14-day measurement window (opened by PR #599, 2026-06-10) closes ~2026-06-24 and its data is read at the gate. Resume shapes favor (β)/(γ); see the parent roadmap. Body below preserved at v0.3.4 (§11 criterion-10 pin + reassignment event stream, 2026-06-11; v0.3.3 §5.2-audit fold 2026-06-10). Full redraft superseding v0.2 (which superseded v0.1.x). Each prior version is preserved on its branch as a historical record. v0.3 closes the architectural irony the v0.2 council surfaced: cache coherence and feature-flag state move off PostgreSQL into BEAM-native ETS, so the RFC stops piling new PG-coordination load onto the substrate it exists to relieve.
**Parent:** `docs/proposals/beam-footprint-roadmap-v0.md` v0.3.4.
**Sibling, completed:** `docs/proposals/beam-wave-1-sentinel.md` (Surface 1+2 shipped; Surface 3 in flight).
**Sibling, completed:** `docs/proposals/surface-lease-plane-v0.md` Phase A + Wave 2 hardening + resident Phase B + lease RPC recorder/persistence (#412/#414/#417/#418/#419/#476/#480/#481).
**Wave 0 channel:** `audit.coordination_events` exists with `event_type ~ '^(coordination_failure)(\.[a-z_]+)+$'` CHECK constraint; zero rows as of 2026-05-09. The constraint scopes the table to failure events only — informational latency lives in the parallel channel introduced in §6.
**Single-writer surface:** Identity / onboarding (per `CLAUDE.md` "Before Starting Work on a Single-Writer Surface") spans this entire RFC plus its prereq PRs. Branch from this RFC's head before any parallel work.

## Post-v0.3.2 status fold (2026-05-20)

Two non-Wave-3-specific prerequisites moved after this RFC was drafted:

- **Resident Phase B is already open.** PR #476 merged 2026-05-20 UTC after the
  lease-plane evaluator returned PROMOTABLE with controlled drill evidence.
  §4 option (α) is revised from "open resident Phase B" to "reuse existing
  resident enforcement where it actually applies"; it is not evidence for
  unrelated agent-state surfaces.
- **Lease RPC instrumentation uses the existing client module.** PR #480 added
  per-call recorder hooks in `src/lease_plane/client.py`; PR #481 persists
  acquire p50/p99 snapshots to `metrics.series`. §6/§14 PR #6 should build on
  that recorder if it still needs the `audit.coordination_measurements`
  channel.

## §14 prereq execution status fold (2026-06-12)

Bookkeeping only — no spec change, no redraft; the (α) deferral binds until
the PR #599 window closes (~2026-06-24). Rows executed so far, in landing
order:

- **Row 1** — shadow DDL (migrations 043/044), comparator/replay tooling,
  event-type constants, ODE profile, and the §5.2 boundary-cost audit
  (v0.3.3 fold above).
- **Row 3** — `governance_core/coordination_events_helpers.py` +
  `scripts/dev/check-boundary-event-helpers.sh`.
- **Row 6** — `audit.coordination_measurements` bridge + retention; PR #599
  opened the 14-day window 2026-06-10.
- **Row 9** — dialectic baseline pin + reassignment event stream, PR #626
  (v0.3.4 fold above).
- **Row 10** — transport 503 emission + sliding-window breach aggregator
  (inert behind `WAVE3_CUTOVER_503_AGGREGATOR`) + consumer retry-on-503
  (SDK async/sync clients, `post_finding`), PR #628. `src/mcp_transport.py`
  is the §3.2 single source; cross-repo consumers (dispatch worker, plugin)
  remain follow-ups in their own repos.
- **Row 4** — `tests/integration/test_identity_path2_ipua_pin_pipeline.py`
  per §7.3: end-to-end `handle_onboard_v2` through the real PATH 7 pin
  lookup and resolution-source contextvar, pinning the strict-mode
  passthrough invariant (`agent_id`/`agent_uuid` are proof), the no-proof
  strict fresh-mint + alert, pin-left-intact on strict refusal, and the
  log-mode resume. `drive_onboard` is the §7.3 swap point for the BEAM-side
  parity run.

Remaining: **row 5** (golden captures — parked while PR #619's response
envelope is in flight), **rows 2 / 7 / 8a / 8b** (Wave-3-specific
implementation — paused under (α); 8b additionally gated on the row-6
14-day window by `check-wave3-prereq-data-window.sh`).

## What changed in v0.3.5 (vs v0.3.4) — §3.1 surface-inventory factual corrections (2026-06-28)

Factual corrections only (no architecture redraft; the (α) deferral still binds). A runtime
re-derivation pass against the *live* code/DB — prep for the (D) state-ownership red-team —
found two §3.1 errors and surfaced a candidate ninth surface. Recorded here so the canonical
inventory is not wrong while the gate is pending; the **reducibility verdicts and the
halt/reopen decision remain the operator-led red-team's**, per (D).

1. **§3.1-H source-of-truth was wrong.** H listed `core.feature_flags` (PG) + §10 FeatureFlagWriter
   as the current SoT. That table **does not exist in the live DB**; the honesty gates are read from
   `config.governance_config.identity_strict_mode()` / `ipua_pin_check_mode()` (env/config
   functions). `core.feature_flags` + FeatureFlagWriter are *unbuilt §10 target* state. Consequence:
   porting H is **net-new durable coordination**, not a reduction — it must be counted on the
   introduces-coordination side of disconfirmer (B), not the removes-tax side.

2. **§3.1 was not exhaustive — candidate 9th surface (I) added.** `resolution.py`'s PATH 2.8 holds
   the **substrate-anchored-UUID / UDS-peer-attestation gate** (SoT `core.substrate_claims`, exists
   live; mechanism `verify_substrate_claim(peer_pid)` → `read_service_label(peer_pid)` = OS-kernel
   peer-credential attestation). The RFC body mentions `substrate_claims` / `peer_pid` / UDS **zero
   times**. It is identity-middleware-scoped (lives in the onboard resolution path) and **likely
   irreducible**: kernel UDS attestation is bound to the OS process/socket boundary and cannot move
   to GenServer state without replicating the peer-cred check at the BEAM boundary — verbatim the
   (D) "irreducible per-request semantics" thesis. **Under (D)'s rules, a confirmed 9th surface
   halts the gate and reopens the RFC.** PATH-enumeration also confirmed the other unmapped paths
   fold (PATH 0 → H; PATH 2 PG-lookup → C/D read-tier with S21-a fail-closed → H; PATH 2.8's
   token-rebind proper → E). Full worktree worksheet: `docs/handoffs/wave-3-state-ownership-redteam-prep-2026-06-28.md`.

## What changed in v0.3.4 (vs v0.3.3)

19. **§11 criterion 10 pinned (prereq PR #9 executed, 2026-06-11).** The (F) baseline is formally unpinnable — trailing 30-day window held 1 session vs the ≥30 the haltspec requires — so the pin records the halt condition plus 90d/all-time context rates. The deeper finding: (F)'s reassignment-rate metric had no measurement source (reassignments were transcript-only; zero matching `audit.events` rows all-time). `_apply_reviewer_reassignment` now emits `dialectic_reviewer_reassigned`, making the threshold settable from the emission's deploy forward. Volume recovery (dialectic-rework arc) is upstream of any future re-pin.

## What changed in v0.3.3 (vs v0.3.2)

§5.2 boundary-cost audit executed (§14 row-1 deliverable, 2026-06-10; committed summary at `docs/proposals/wave-3-section-5-2-boundary-audit-summary.md`, full analysis in the operator-local dated handoff). Measured: helper compute 0.04–23 µs/call vs crossing floor 3.2–3.5 ms (PR #599 baseline) — every helper is crossing-dominated by raw ratio, so call placement is the operative rule: bundled-in-§5.6 stays Python; standalone-from-§5.1 ports.

16. **§5.2 → §5.3 reclassifications** (the `_compare_against_timeout` pattern, caught before any BEAM code lands): `_read_proposed_conditions`, `check_hard_limits` (regex-dialect golden gate; caller is the synthesis finalize path, handlers:1434 inside `handle_submit_synthesis`), `compute_signature`/`canonical_payload` (golden-vector **byte-parity** gate — the one load-bearing risk; documented fallback = a single bundled `sign_resolution` compute mode at 1 crossing/session; the golden corpus must include the three False-returning verification cases: `signature_version==1` rows, empty signature, empty api_key), `Resolution.hash`. `verify_signatures` goes **dual** (BEAM verifies at runtime; Python retains archival verification of stored v1/v2 resolutions). §5.3's `_parse_timestamp` row flips to PORTS — its caller is `auto_resolve` (§5.1); the literal twin of the v0.3 `_compare_against_timeout` correction. `DialecticMessage.sign` is NOT in the parity cluster: legacy per-message path with zero live call sites (uncapped sweep) — removal candidate at the gate, not a port.
17. **§5.2 `condition_parser` row split**: `parse_condition` (pure) ports-or-bundles; `apply_condition` is async state-mutation taking `mcp_server` (resolution.py:59) — coordination, not computation; it moves under §5.1's `execute_resolution` port.
18. **§5.2 calibration row corrected (council BLOCK fold)**: `backfill_calibration_from_dialectic` is its own MCP tool (top-level entry, not a helper-crossing question); `update_calibration_from_dialectic`(+`_disagreement`) are **live-wired** at handlers 1479/1481 (synthesis convergence branch) + calibration.py:243 — firing ≤1× per resolved session (~1/month at live volume): production-rare, not dead. Their crossing rides the synthesis-finalize surface (§5.1) at the implementation PR; no pre-gate action. An earlier draft called them imported-never-called — a head-capped-sweep artifact, council-refuted. Related flag: §5.1's line ranges have drifted (e.g. `handle_reassign_reviewer` starts at 1508, not 1389); refresh the line map at the implementation gate.

## What changed in v0.3.2 (vs v0.3.1)

v0.3 council (architect + code-reviewer + live-verifier) returned three CONCERNs, zero BLOCKs. Per §15 contract — no BLOCK + no new bias signature in §10 — this is not v0.4 substrate re-litigation territory. Mechanical findings landed as v0.3.1; design-decision findings are folded here as v0.3.2 against operator selection (2026-05-13).

10. **§10.2 cold-ETS startup race closed both ways** (code-reviewer BUG 2). New `Unitares.HandlerDispatch.Readiness` GenServer gates handler dispatch on both writers signalling `:writer_warm`; cold reads can't happen mid-init through the dispatch path. Belt-and-suspenders: surfaces that bypass dispatch (observers, dashboards) get a documented `:cold` return contract — never `nil`, always `{:ok, value}` or `:cold`. ExUnit test `cold_ets_contract_test.exs` lands in new PR #8a alongside the contract.
11. **§3.2 503-rate denominator named** (code-reviewer BUG 4). Transport emits `measurement.governance_mcp.request` to `audit.coordination_measurements` on every request accepted for proxying. Numerator + denominator share one source; rate is restart-recoverable from PG history. No process-memory counter; no cross-table join.
12. **§9.3 recovery SELECT made explicit** (code-reviewer BUG 3). Exact query in §9.3; explicit filter discipline (`state NOT IN ('reverted')`); `pg_committed` included for crash-between-commit-and-ACK recovery.
13. **§14 PR #8 split + PR #6 14-day mechanical gate** (code-reviewer BUG 5). PR #8 splits into 8a (BaselineWriter stub + Readiness + cold-ETS contract test, lands with #2) and 8b (wiring + reconciliation, gated on PR #6 14-day data window via `check-wave3-prereq-data-window.sh` CI lint). §11.5 is now mechanically enforced, not policy-enforced.
14. **§0(D) operator-led red-team forcing function** (architect CONCERN on self-referentiality). (D)'s measurement source is no longer "§3 surface-by-surface analysis at gate" reading the same §3 the RFC wrote. Operator-led red-team session before gate, with live-verifier lane, written to dated artifact, MUST enumerate non-surfaces as well as surfaces. Gate halts on artifact missing, 9th surface found, or any of the eight not re-derivable from live code by the verifier.
15. **§5.2 boundary-cost audit bundled into PR #1** (architect latent-v0.4 bias-trap flag). Same workstream as ODE profile; output `docs/handoffs/wave-3-section-5-2-boundary-audit-<date>.md`; reclassifications fold into §5.3 in a v0.3.x patch. Catches the under-counting habit before any BEAM code lands.

## What changed in v0.3.1 (vs v0.3)

v0.3 council mechanical findings (file:line drifts, missing §8 columns, present-tense framing for not-yet-existing files). See [PR #457 comment](https://github.com/cirwel/unitares/pull/457) for the full council synthesis.

## What changed in v0.3 (vs v0.2)

The v0.2 three-lane council returned: architect BLOCK on three structural items, code-reviewer CONCERN on four surgical items, live-verifier APPROVE with three minor line-drift items. The architect's load-bearing finding was that v0.2's §10 (versioned baselines + reconciliation) and §4 (advisory lock) added sustained PG-coordination load to the very substrate Wave 3 exists to relieve — the fifth bias signature across four iterations. v0.3 corrects all three architect items, all four code-reviewer items, and the three drifts.

1. **§10 redesigned around BEAM-native ETS** (architect BLOCK item 3, the bias signature). The canonical live cache for baselines and feature flags is an ETS table inside the BEAM application; PostgreSQL is the durable backing store, not the hot read path. Reads from any BEAM handler are `:ets.lookup` (lock-free, microseconds, no PG round-trip). Writes go through a single GenServer that commits to PG transactionally and then updates ETS. Python doesn't have its own cache for these surfaces post-Wave-3 — Python compute paths receive whatever they need as request payload from BEAM. The PG-on-observe pattern v0.2 introduced is gone.
2. **Disconfirmer (F) escape hatch removed** (architect BLOCK item 1). The "OR operator's written go-decision document explicitly accepts the slip" clause is dropped. >25% slip on any of {paper, fellowship, HLH, R2 Phase 2} halts Wave 3 unconditionally; re-opening requires re-scoping, not an acceptance memo.
3. **Three new stop signs for v0.3-introduced failure modes** (architect BLOCK item 2). §12 #10 (saga GenServer.call deadlock if two sessions ever share an agent during the saga window), #11 (ETS-vs-PG divergence detected on slow reconciliation), #12 (`audit.coordination_measurements` partition pressure exceeding a stated rate without retention policy applied). v0.2's stop sign #9 (pub-sub lag) is retired because pub-sub is no longer load-bearing.
4. **§8 comparator covers all live drift-candidate columns** (code-reviewer item 1, expanded in v0.3.1 from v0.3 council pass). `core.agents` adds `purpose / notes / tags / archived_at`, plus v0.3.1 adds `allow_rebind_after_exit / allow_concurrent_contexts`; `core.identities` adds `provisional_recorded_at`, plus v0.3.1 adds `disabled_at / last_activity_at / provisional_score_id / lineage_declared_at / lineage_demoted_at / lineage_last_eval_at / chain_obs_count` (the migration 036 lineage-lifecycle cluster + operational state). Verified against `\d core.agents` (16 cols) and `\d core.identities` (21 cols) 2026-05-13. Intentionally omitted: PK/identity columns, `created_at` / `updated_at` (would always drift on shadow-write lag), generated columns (`metadata_tsv`).
5. **§9 saga adds `UNIQUE (session_id) WHERE state NOT IN ('reverted')`** (code-reviewer item 2). Prevents two pending sagas per session even when payload hashes differ.
6. **§3.2 503 halt mechanism specified** (code-reviewer item 4). Counter is the `measurement.governance_mcp.503_emission` event in `audit.coordination_measurements`; halt direction is "complete step 3 (restore Python writers) before stopping"; SDK consumers (Watcher / Sentinel / dispatch) must honor `Retry-After` or use the response-body `retry_after_seconds` field, named in the prereq PR.
7. **Disconfirmers re-lettered cleanly to A/B/C/D/E/F** (architect cleanliness CONCERN). v0.1.x's (C) was retired in v0.2 but the alphabet kept the gap; v0.3 closes it. Mapping for backward citations: v0.2 (A/B/D/E/F/G) → v0.3 (A/B/C/D/E/F).
8. **Line cite drifts fixed** (verifier). `agent_auth.py:309-515` → `309-549`; `identity_step.py:365-474` honesty-gate range corrected to `384-474`. v0.3.1 corrects: `handlers.py:1185` → `1184-1185` (call site at 1184, critical-section block at 1185); `schema.sql:157` → `253` (the `core.dialectic_sessions` `updated_at` trigger relevant to §2(ii) `SELECT … FOR UPDATE` safety; 157 was an EISV column DDL).
9. **`_compare_against_timeout` re-classified to PORTS-to-BEAM** (architect §5.3 CONCERN). Pure timestamp comparison is trivially native to Elixir's `DateTime`; calling Python for it would add a boundary crossing for arithmetic.

The rest of the document — §2 lock-invariant inventory, §3 state-ownership matrix (modulo line-cite fixes), §5 dialectic split (modulo `_compare_against_timeout`), §7 test strategy, §6 boundary instrumentation namespaces — survives from v0.2.

---

## §0 Falsifying-evidence question

> **What evidence would update us away from porting handler dispatch + identity middleware + dialectic resolution to BEAM?**

Per `feedback_substrate-migration-status-quo-bias.md` and the symmetric warning in `beam-footprint-roadmap-v0.md` §"Why Read A, not Read B", both substrate enthusiasm and substrate resistance are biases. The disconfirmers below name what would actually halt Wave 3, with each threshold anchored to a measurement source.

The bias discipline lives in two places: (i) every threshold names its measurement source, and the gate halts if the source is missing; (ii) every new substrate cost the RFC introduces (PG queries, advisory locks, GenServer calls) is counted against disconfirmer (B)'s boundary-cost budget rather than treated as free. Item (ii) is what v0.3 corrects relative to v0.2.

Note on threshold derivations: the numerical anchors below ((A.1)'s 60%, (A.2)'s 2.0s, (B)'s ×2/×3 multipliers) are *operator-chosen priors against which measurement runs*, not derived constants. They are taste choices made at this stage; sensitivity tests at ±5% of each anchor are part of prereq PR #1's deliverables, so the gate decision doesn't hinge on a brittle exact value.

### Disconfirmer set

**(A) User-visible-metric headroom — two paths.**
- **(A.1) ODE-floor dominates.** ODE profile against still-Python `governance_core/phase_aware.py` and `governance_core/stability.py` shows >60% of `process_agent_update` p99 floor in `governance_core/` math over a 7-day production sample. Anchor: prior, sensitivity-tested at 50% / 65% / 75% if the result lands within ±5% of 60%. **Measurement source:** ODE profile commit on master (prereq PR #1). Wave 3 implementation cannot start before this commit lands.
- **(A.2) In-place Python fix closes the gap.** Any Python-side fix shipped during Wave 3 implementation window brings `process_agent_update` p99 below **2.0s** without porting. Anchor: per `project_locked-update-overhead-fix.md`, current ~5.0s post-#372; 2.0s = 40% of current. **Measurement source:** `process_agent_update` p99 from existing production telemetry.

**(B) Boundary cost ≥ substrate tax removed.** `audit.coordination_measurements` channel (§6, prereq PR #6) shows sustained per-call boundary cost p50 ≥ lease-plane Phase A measured p50 × 2 OR p99 ≥ lease-plane Phase A measured p99 × 3 over a 14-day window. Anchor: ×2/×3 multipliers reflect Wave 3's heavier per-call payload (full request marshalling vs lease ack). The (B) budget MUST include all PG-touching coordination v0.3's design adds — specifically §4(β) advisory locks (per-write, bounded) and §9 saga PG transactions (per-resolution, bounded). v0.2's per-observe PG version-checks are gone. **Measurement source:** prereq PR #6 must produce ≥14 days of `audit.coordination_measurements` rows before disconfirmer (B) thresholds can be set. If <14 days at Wave 3 implementation gate, **gate halts on missing measurement** — no fallback default. **Baseline-locus caveat (council fold, PR #599):** Phase A baseline rows originate from drain-equipped long-lived processes (MCP server, residents); short-lived/hook callers sample-but-drop (bounded, counted in each row's `meta.samples_dropped_total`). Long-lived processes carry the documented anyio↔asyncio contention, so the baseline may skew toward the event-loop-contended locus — i.e. *upward*, raising the ×2/×3 thresholds and tilting (B) toward proceeding. Whoever sets thresholds at the gate must cross-check `samples_dropped_total` magnitude (and the all-process `metrics.series` p50/p99 channel) before fixing values.

**(C) MCP SDK gate reverses.** Hands-on spike on `mcp_elixir_sdk` 1.0.1 or `hermes_mcp` 0.14.1 shows production-disqualifying failure (broken-on-Anthropic-streaming, MCP-spec drift, no maintainer responsiveness). Doubles disconfirmer (B)'s budget per §6.6's 4-crossing topology. **Measurement source:** spike result, recorded in `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` artifact before implementation gate. (Was (D) in v0.2.)

**(D) State-ownership cutover structurally unsafe.** Identity middleware port (§3) surfaces irreducible per-request semantics that can't be moved to GenServer state without replicating coordination at the boundary. **Measurement source:** operator-led red-team session before the implementation gate, with the live-verifier lane participating, written up in `docs/handoffs/wave-3-state-ownership-redteam-<date>.md`. The artifact MUST enumerate both surfaces and non-surfaces — i.e., name every identity-middleware coordination shape considered, including the eight in §3.1, and state for each whether it is reducible to GenServer state. The forcing function is "find a 9th surface OR explicitly mark the eight as exhaustive after independent re-derivation." Gate halts if the artifact is missing, if any 9th surface surfaces, or if any of the eight cannot be re-derived from the live code by the verifier lane. (Was (E) in v0.2. Architect CONCERN on self-referentiality closed in v0.3.2 by the operator-led red-team forcing function — the gate now reads independent re-derivation, not the same §3 the RFC already wrote.)

**(E) Opportunity cost.** Wave 3 implementation projected calendar-weeks > (Wave 1 elapsed × 3) AND any of {paper deadline, fellowship application, HLH, R2 Phase 2 gate} would be sacrificed. "Sacrificed" defined as: calendar-week slip on any named item exceeds 25% of original deadline window. **No acceptance-memo escape hatch.** Re-opening the gate requires re-scoping Wave 3, not a written acceptance of the slip. Wave 1's elapsed time is concretely named in §14 (currently estimated ~3 weeks; pinned to actual at gate). **Measurement source:** `docs/proposals/wave-3-go-decision-<date>.md` artifact written by operator at gate, with §"Calendar reasoning" section enumerating each named item with current slip vs original target. (Was (F) in v0.2.)

**(F) Dialectic-quality regression.** During canary, dialectic session-resolution rate (resolved / (resolved + failed + escalated) over a 14-day window) regresses >5% against pre-Wave-3 baseline. Reviewer-reassignment rate increases >20%. **Measurement source:** baseline computed from trailing 30 days of `core.dialectic_sessions` rows (47 total as of 2026-05-09; gate halts on insufficient baseline volume if 30-day window has <30 sessions). Both baseline mean and σ pinned in §11 prior to implementation start (prereq PR #9). (Was (G) in v0.2.)

### What disconfirmation is NOT

- Wave 1 / Wave 2 "shipping without incident" is not confirmation. Clean operations with bad boundary numbers is disconfirmer (B).
- "BEAM is the right substrate philosophically" is not evidence — it is the prior the bias warning targets.
- Operator preference is not evidence at the Go gate (it was the input at scope; the gate is evidence-bearing).

§11 makes Wave 3's go-decision conditional on every disconfirmer being measured-and-not-triggered. There is no "structural success but user-visible miss" escape hatch; if any disconfirmer fires or any measurement source is missing at the gate, Wave 3 halts and the roadmap re-opens.

---

## §1 Roadmap-level scope

- **Handler dispatch** (the `@mcp_tool` decorator's wrapper, per-tool routing, response shaping) ports to BEAM. The MCP transport layer itself stays Python (per disconfirmer (C)) and proxies to BEAM after request unmarshalling.
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
| 5 | Dialectic session lock: SYNTHESIS→RESOLVED serialization across `submit_synthesis(agrees=True)` | `dialectic/handlers.py:1681-1682` (`get_session_lock` call at 1681 from `dialectic/session.py:55`; `async with session_lock:` block at 1682) — corrected from `:1184-1185` (the synthetic-reviewer LLM-availability check) per §2 council pass | Serialized by the §9.2 step-1 `reserved`-saga INSERT against `idx_saga_one_pending_per_session` (design (B), §2 council pass) — NOT `FOR UPDATE`. The asyncio.Lock in `session.py:51-68` (`_SESSION_LOCKS` + `_SESSION_LOCKS_DICT_LOCK`) is replaced by the unique-index gate |
| 6 | Baseline preload: `get_baseline_or_none(agent_id)` once per process; cached in `_baseline_cache` (`governance_core/ethical_drift.py:418`) | `phases.py:809-820, 856-899` | BEAM-native ETS cache (§10); Python cache disappears post-Wave-3 |
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

**Recommendation: (ii).** Doesn't break under multi-node BEAM (parent roadmap §"Post-Wave-3 candidates" names multi-node as a real possibility). The trigger-safety question against `trg_dialectic_sessions_updated_at` (`db/postgres/schema.sql:292`, function `core.update_timestamp()` at `:51-57`) is **resolved by the §2 council pass below — the trigger is lock-free and discharged**; but the council surfaced a *different*, real deadlock (the §9 saga FK `FOR KEY SHARE` × `FOR UPDATE`) that the recommendation must address first. (iii) becomes the optimization, taken later if profiling shows row-level lock contends.

### §2 council pass — SAFE-WITH-CONDITIONS (2026-06-26)

Three-lane pass (dialectic-knowledge-architect / feature-dev:code-reviewer / live-verifier) on whether option (ii) `SELECT … FOR UPDATE` is deadlock-safe. **Verdict: SAFE-WITH-CONDITIONS** — (ii) is the right primitive but materially under-specified; do not merge the executor until the conditions are met.

- **Trigger concern discharged.** Live-verifier confirmed against the *running* governance DB (no schema.sql drift): the trigger is `BEFORE UPDATE FOR EACH ROW` running `NEW.updated_at = now(); RETURN NEW;` — lock-free, single-row, in-image, fires after the row write-lock is already held. Cannot deadlock with FOR UPDATE.
- **Real risk the recommendation did NOT name: FK `FOR KEY SHARE` × `FOR UPDATE` deadlock on the session row.** The §9 saga FK (`session_resolution_sagas.session_id REFERENCES core.dialectic_sessions`, migration 049) makes every saga INSERT take `FOR KEY SHARE` on the parent session row, which conflicts with `FOR UPDATE`. Cycle (architect + reviewer, independently): TxA holds FOR UPDATE → waits on TxB's FOR KEY SHARE; TxB holds FOR KEY SHARE (saga INSERT) → then requests FOR UPDATE → waits on TxA. The partial-unique `idx_saga_one_pending_per_session` *widens* the window (a conflicting INSERT blocks until the holder commits rather than erroring immediately).

**Conditions owed before the executor merges:**
1. **Lock-order invariant (blocking).** Every path writing `coordination.session_resolution_sagas` (§9.2 forward step 1, §9.3 recovery, revert paths) MUST take `SELECT … FOR UPDATE` on the session row *before* any saga INSERT. A transaction's own locks are self-compatible, so FOR UPDATE→FK-FOR KEY SHARE within one txn never cycles; the inverse order does. Document as a hard GenServer invariant, not a convention.
2. **Transaction scope (blocking).** FOR UPDATE is transaction-scoped, but dialectic writes are currently autocommit-per-statement (no `conn.transaction()` in `src/dialectic_db.py`); the asyncio.Lock it replaces (`handlers.py:1681-1682`) was a Python critical region across multiple pool acquisitions. The executor must hold ONE open transaction across SELECT→check→resolve→saga. §9.2 step-4's "single PG transaction" is the work item, with no existing helper.
3. **Mandatory gate (blocking).** `update_session_phase` / `update_session_status` / `resolve_session` (`dialectic_db.py:196/215/226`) currently mutate the row in bare autocommit with no gate; a FOR UPDATE waiter cannot see an ungated writer, so the invariant is only as strong as its least-disciplined write path.
4. **Real-Postgres concurrency test (owed).** Two concurrent forward-path txns on one session must resolve to a unique-constraint violation (retryable), NOT a deadlock. asyncio-mock tests do not exercise PG row locks.

**Design choice — RESOLVED: (B) chosen (operator, 2026-06-26).** The fork was **(A) fixed strongest-first lock ordering** (keep `FOR UPDATE`, mandate order) vs **(B) make the "reserve" saga INSERT itself the serialization point**. **(B) is adopted.** Invariant 5's mutual exclusion is the §9.2 step-1 `reserved`-saga INSERT against `idx_saga_one_pending_per_session`: at most one pending saga per session, so a concurrent SYNTHESIS→RESOLVED driver loses the INSERT with a unique-constraint violation and treats it as a retryable conflict (ack without writing; await the existing saga's terminal state — the behavior already specified at §9.1 and the migration-049 comment). Consequences:

- **`FOR UPDATE` is removed from invariant 5.** Option (ii) is no longer the invariant-5 mechanism; the FK `FOR KEY SHARE` × `FOR UPDATE` deadlock class is **eliminated by construction** (only one lock mode touches the session row from the saga path). `FOR UPDATE` may survive only as a narrow in-saga guard inside the already-serialized holder if a specific step needs it — not as the top-level gate.
- **Conditions 1–2 dissolve** (no `FOR UPDATE` ordering or cross-statement transaction discipline to enforce for invariant 5). **Conditions that remain under (B):**
  - **(B-1) Reserve-first discipline.** No path may drive SYNTHESIS→RESOLVED without first claiming the `reserved` saga slot; the resolution UPDATE is reachable only by the saga holder.
  - **(B-2) Unique-violation is a control-flow signal, not an error.** The INSERT's `unique_violation` (SQLSTATE 23505 on `idx_saga_one_pending_per_session`) must be caught and mapped to "another driver holds the slot → ack + await", never surfaced to the agent.
  - **(B-3) Real-Postgres concurrency test (still owed).** Two concurrent reserves on one session → exactly one succeeds, the other gets `unique_violation` (NOT a deadlock, NOT a second resolution). asyncio-mock tests do not exercise this.
  - **(B-4) Idempotent resolution.** The saga holder's `pg_resolve_session` must no-op if the session is already `resolved` (defends a crash-recovery double-drive per §9.3).

**Council also corrected:** the (i)-rejection rationale ("advisory lock leaks on connection death", below) is imprecise — `FOR UPDATE` and `pg_advisory_xact_lock` both release on backend exit; (ii)'s true advantage is participating in PG's deadlock detector and composing with the FK lock graph, which advisory locks do not.

---

## §3 State ownership and rollback during transition

### 3.1 Surface inventory

Identity middleware decomposes into eight documented state surfaces (A–H). **A runtime
re-derivation pass (2026-06-28, recorded below) surfaced a candidate ninth (I) that this
inventory had omitted, plus a source-of-truth error in H — see the v0.3.5 fold.** The (D)
gate's exhaustiveness criterion ("find a 9th surface OR mark the eight exhaustive after
independent re-derivation") is therefore NOT yet satisfiable as "eight": surface I must be
confirmed-or-folded by the operator-led red-team before the gate can read this section as
complete.

| # | Surface | Read | Write | Source of truth | BEAM port strategy | Cutover semantics |
|---|---------|------|-------|------------------|---------------------|---------------------|
| A | ContextVars (10 declarations; 4 identity-bearing) | `context.py:131-147` (incl. `update_context_agent_id` at 141-147 — writer) | `context.py:86-114` | Process memory only (async-task-local) | Stays Python at boundary; BEAM threads request-context explicitly through GenServer state. Marshalled context-payload bytes-per-request enters disconfirmer (B) budget | Direct flip — ephemeral |
| B | Sticky transport binding cache (3-layer: dict / Redis / PG fallback) | `identity_step.py:289-298` (Redis recovery 0.5s timeout at 292) | `identity_step.py:98-157` (fire-and-forget Redis), `:230-248` (invalidate) | In-memory dict when populated; Redis when recovered; no PG anchor | BEAM owns as per-process GenServer state | No shadow needed — drop in-memory cache → next request falls through |
| C | Session→UUID Redis cache (`sticky:{ip_ua_fingerprint}:{mcp_session_id}` keys) | `resolution.py:430-470` (PATH 1) | `persistence.py:175-200` (`_cache_session` SETEX); NX in inner `_cache_session_redis_write` at 206+ | PostgreSQL canonical; Redis is speed cache | Shadow ≥1 cycle then flip | Rollback: re-enable Python writes, BEAM HTTP-read-only |
| D | PG canonical identity (`core.identities` AND `core.agents` upsert on PATH 3 fresh mint) | `resolution.py:950-1116` (PATH 3) | `db.upsert_identity`, `db.upsert_agent` | PostgreSQL (both tables; coupled) | BEAM owns the upsert; PG INSERT/UPDATE moves into GenServer message atomicity | Shadow ≥1 cycle then dual-write window then BEAM-only. Both tables shadowed; see §8 |
| E | Continuity token (HMAC over agent_uuid + chh + exp + iat + sid + opv); actual fields: `v`, `opv`, `sid`, `aid`, `mf`, `ch`, `iat`, `exp` | `session.py:176-220` | `session.py` (`create_continuity_token` at onboard) | Cryptographic — token string IS source | Stays Python OR moves to BEAM — orthogonal | No rollback contract |
| F | Onboard PIN (Redis-keyed `onboard_pin:{ip_ua_fingerprint}` with model scoping; IPUA pin treats `agent_id` as proof per `project_ipua-pin-agent-id-proof.md`) | `session.py:769-797` (`lookup_onboard_pin` with `_PIN_REDIS_TIMEOUT = 0.5s` at line 28) | `session.py` (`set_onboard_pin` SETEX, 30m TTL) | Redis (TTL 30m); IPUA invariant locked by contract test | Shadow ≥1 cycle then flip; IPUA invariant CANNOT be relaxed | Shadow then flip |
| G | Agent metadata cache (`mcp_server.agent_metadata[uuid]`) | `agent_auth.py:59-134, :151, :309-549` (`require_registered_agent` body) | `background_tasks.py:343` (`background_metadata_load`) | PostgreSQL `core.agents` canonical; in-memory dict is read-side cache | BEAM-native ETS cache via single-writer GenServer (§10). Reads from any BEAM handler are `:ets.lookup`. Python compute paths receive metadata as request payload from BEAM, never read directly | No rollback contract — read-mostly |
| H | Identity honesty gates (`identity_strict_mode`, `ipua_pin_check_mode`) | `identity_step.py:384-474` (reads via `config.governance_config.identity_strict_mode()` at `:628`), `agent_auth.py:271-293, 312-315` | **CURRENT: `config.governance_config.identity_strict_mode()` / `ipua_pin_check_mode()` (env/config functions).** ⚠ The previously-listed `core.feature_flags` PG row + §10 FeatureFlagWriter is the *target* (post-§10) state — `core.feature_flags` does **not exist** in the live DB (verified 2026-06-28); the FeatureFlagWriter + table are **unbuilt Wave-3 §10 work** | CURRENT: config/env function. TARGET: PG durable + ETS canonical-live | BEAM target mirrors flag check from ETS — but note this surface is **net-new durable state introduced by the port**, not a reduction of existing coordination (today it is a config-function read) | Direct flip via flag write (target); converge within reconciliation interval (slow cadence: 5min) |
| I | **(CANDIDATE 9th — pending (D) red-team confirmation)** Substrate-anchored-UUID / UDS-peer-attestation gate | `resolution.py` PATH 2.8 (`grep -n "PATH 2.8"`); per-request `peer_pid` via `context.get_session_signals()` | n/a (read-gate; refuses HTTP token-resume for substrate-anchored UUIDs) | `core.substrate_claims` (exists live, 2026-06-28) + **OS-kernel peer-credential attestation** via `src/substrate/verification.py::verify_substrate_claim(peer_pid)` → `read_service_label(peer_pid)` | **Likely IRREDUCIBLE** — kernel UDS peer-cred attestation is intrinsically bound to the OS process/socket boundary; moving the gate to a BEAM GenServer would require replicating the UDS peer-cred check at the BEAM boundary (verbatim disconfirmer (D) thesis). Closes the Hermes-incident HTTP-token leak | No clean cutover if irreducible — the attestation cannot be a copyable token/payload by design |

### 3.2 Rollback procedure

1. **Snapshot before flip.** `pg_dump` `core.identities`, `core.agents`, `core.identities_shadow`, `core.agents_shadow`, `core.dialectic_sessions`, `core.dialectic_messages`, `coordination.session_resolution_sagas`, `core.feature_flags` into `~/backups/governance/wave-3-pre-cutover-<ISO8601>/`.
2. **Plist swap.** New plist `com.unitares.handler-dispatch-beam.plist` in `scripts/ops/`. Cutover loads BEAM; rollback unloads BEAM and reloads `com.unitares.governance-mcp.plist`.
3. **503 circuit-breaker for the gap.** Python MCP transport, when proxying to BEAM, returns HTTP 503 with body `{"ok": false, "error": "governance_temporarily_unavailable", "reason": "handler_dispatch_unavailable", "retry_after_seconds": 5}` and `Retry-After: 5` header on connection-refused or timeout. **Halt mechanism (per stop sign #7):** the transport emits two events to `audit.coordination_measurements` — `measurement.governance_mcp.503_emission` on every 503 returned, and `measurement.governance_mcp.request` on every request *accepted for proxying* (the denominator). Same emission point, same table, partition-aligned. A sliding-window aggregator over the last 60s reads both events once per 15s, computes `count(503_emission) / count(request)`, and emits `coordination_failure.governance_mcp.cutover_503_rate_breach` if the rate exceeds 1% in the window. Numerator and denominator share one source (`audit.coordination_measurements`), so the rate is restart-recoverable from PG history within the 60s window — no process-memory counter. **Halt direction:** when the breach event fires, the operator completes step 3 (restore Python writers) before stopping; BEAM is unloaded last. Do not stop mid-procedure with neither runtime accepting writes. **Client retry policy:** SDK consumers (Watcher, Sentinel, dispatch worker, plugin SessionStart paths) must honor `Retry-After` OR the body's `retry_after_seconds` field; the prereq PR (#10) adds matching retry logic to each named consumer. Clients without retry-on-503 fail through to the calling layer and are not the rollback procedure's responsibility.
4. **Schema rollback.** Every new migration ships a paired DOWN migration; tested on `governance_test` snapshot before cutover migration runs in production.
5. **Per-surface windows:** A/E/H instantaneous; B/C/G ≤2h staleness (TTL); D ≤1-request inconsistency at flip moment (shadow + dual-write window keeps it bounded); F instantaneous.

---

## §4 Multi-writer enforcement gate during cutover

The cutover window has a dual-write phase where both BEAM and Python actively write to the same agent's state. After full cutover, BEAM is the sole writer for the migrated surfaces; this section's coordination is bounded to the cutover window.

- **(α)** Reuse existing `resident` Phase B enforcement where the cutover touches resident-owned surfaces. PR #476 already opened resident enforcement, so this no longer couples Wave 3 to opening Phase B. It also does not generalize resident drill evidence to unrelated agent-state surfaces.
- **(β) — recommended.** Per-agent PG advisory lock at the writer entry point (`pg_try_advisory_lock(hashtext(agent_uuid))`). BEAM acquires on enter, releases on exit; Python writers attempt with 50ms timeout and fail-fast (returning the same 503-equivalent surfaced as `governance_temporarily_unavailable`). Keeps lease plane unchanged. **Cost accounting:** the per-write advisory-lock round-trip is counted against disconfirmer (B)'s budget (§0). Bounded per write, not per observe.

If (α) is chosen, implementation must verify `LEASE_PLANE_ENFORCED_SURFACE_KINDS`
includes `resident` in every process participating in the relevant cutover path
and must name any non-resident surface_kind separately. If (β) is chosen, §4 is
a binding implementation spec. Council confirms before implementation gate.

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
| `mcp_handlers/dialectic/handlers.py:368-412` | `_apply_reviewer_reassignment` | Stuck-session recovery |
| `mcp_handlers/dialectic/handlers.py:414-635` | `handle_request_dialectic_review` | Session creation; PG write `pg_create_session` line 478 |
| `mcp_handlers/dialectic/handlers.py:897-985` | `handle_submit_thesis` | PG write `pg_add_message` 910; phase transition 922 |
| `mcp_handlers/dialectic/handlers.py:986-1147` | `handle_submit_antithesis` | Reviewer assign 1040; phase transition 1056 |
| `mcp_handlers/dialectic/handlers.py:1148-1388` | `handle_submit_synthesis` | Convergence 1206-1228; round 1181; **invariant 5 critical section** |
| `mcp_handlers/dialectic/handlers.py:1389-1506` | `handle_reassign_reviewer` | `pg_update_reviewer` 1460 |
| `mcp_handlers/dialectic/resolution.py:18-196` | `execute_resolution` | Agent state mutation (status→active, paused_at=None at 74-75) |
| `mcp_handlers/dialectic/auto_resolve.py:54-220` | `auto_resolve_stuck_sessions` | Periodic detection; reviewer reassignment |
| `mcp_handlers/dialectic/reviewer.py:121-200, 255+` | `is_agent_in_active_session`, `select_reviewer` | Quorum-prevention; collusion gate |

### 5.2 Computation → stays Python, called from BEAM

*(Audited v0.3.3 — compute micro-benchmarked against the PR #599 crossing
baseline; every row below is invoked INSIDE a §5.6 compute bundle, so its
crossing cost is amortized. Rows whose callers are §5.1 coordination moved to
§5.3; see the v0.3.3 changelog and the committed audit summary.)*

| File:line | Function | Why computation (measured compute) |
|-----------|----------|------------------|
| `dialectic_protocol.py:1077-1162` | `calculate_authority_score` | numpy sigmoid + Jaccard + weighted aggregation; pure; bundled in `select_reviewer` (0.2 µs) |
| `dialectic_protocol.py:640-657` | `_normalize_condition_terms`, `_semantic_similarity_terms` | Term extraction + Jaccard; pure; synthesize-bundle internals (1.0 / 2.5 µs) |
| `dialectic_protocol.py:659-743` | `_merge_proposals` | Semantic matching (0.6 threshold); pure; synthesize bundle (23 µs) |
| `dialectic_protocol.py:746-779` | `_conditions_conflict` | Regex + term-overlap heuristics; pure; synthesize bundle (1.5 µs) |
| `mcp_handlers/support/condition_parser.py` | `parse_condition` ONLY | Pure text → ParsedCondition; ports-or-bundles. (`apply_condition` is async state-mutation taking `mcp_server` — moved under §5.1's `execute_resolution` port, v0.3.3.) |

*Calibration row removed (v0.3.3): `backfill_calibration_from_dialectic` is
its own MCP tool, not a helper-crossing question;
`update_calibration_from_dialectic`(+`_disagreement`) are imported-never-called
— dead wiring to resolve before the implementation gate.*

### 5.3 Boundary cases

| File:line | Function | Judgment | Reason |
|-----------|----------|----------|--------|
| `dialectic_protocol.py:995-1031` | `check_timeout` | **PORTS to BEAM.** Both the FSM-phase-gating wrapper AND the timestamp-comparison predicate (`_compare_against_timeout`) move; pure timestamp arithmetic is trivially native to Elixir's `DateTime` and avoids a boundary crossing for arithmetic | v0.2 had `_compare_against_timeout` staying Python; that introduced a boundary call for what's a `DateTime.diff/2` in Elixir. v0.3 corrects |
| `mcp_handlers/dialectic/reviewer.py:55-119` | `_has_recently_reviewed` | **PORTS to BEAM** as part of session-keyed GenServer's reviewer-selection coordination. PG round-trip remains (Postgrex query directly), boundary crossing disappears | Splitting from selection saves nothing |
| `mcp_handlers/dialectic/auto_resolve.py:32-51` | `_parse_timestamp` | **PORTS to BEAM** (v0.3.3; was "stays Python utility") | Caller is `auto_resolve` (§5.1, ports); `DateTime.from_iso8601/1` is native — the literal `_compare_against_timeout` twin (0.1 µs compute vs ms crossing) |
| `mcp_handlers/dialectic/handlers.py:180-201` | `_read_proposed_conditions` | **PORTS to BEAM** (v0.3.3; was §5.2) | All three callers (handlers 281/1061/1350) are §5.1 coordination; trivial dict normalization = pattern-match in Elixir (0.04 µs compute) |
| `dialectic_protocol.py:899-986` | `check_hard_limits` | **PORTS to BEAM** (v0.3.3; was §5.2), gated on regex-dialect golden tests (Python `re` vs Erlang `re`/PCRE) over the safety corpus | Production caller is the synthesis finalize path — handlers:1434 inside `handle_submit_synthesis` (§5.1); 2.5 µs compute. This IS the docstring's resolution-accept gate. (§5.1's stale line map previously mis-attributed it to reassign — refresh ranges at the gate) |
| `dialectic_protocol.py:350-376` | `Resolution.compute_signature` | **PORTS to BEAM** (v0.3.3; was §5.2), gated on golden-vector **byte-parity** (see canonical_payload row) | Callers are finalize coordination (§5.1; protocol 893-894); HMAC is `:crypto.mac(:hmac, :sha256, …)` native (0.9 µs compute). Fallback if parity proves brittle: one bundled `sign_resolution` compute mode (1 crossing/session) |
| `dialectic_protocol.py:250-265` | `DialecticMessage.sign` | **Legacy — removal candidate at the gate** (v0.3.3; was §5.2). NOT in the canonical_payload parity cluster: it signs a different object via its own JSON serialization | Zero live call sites (uncapped sweep 2026-06-10; sole reference is the protocol:791 docstring describing the replaced pre-v2 pattern) |
| `dialectic_protocol.py:331-347` | `Resolution.canonical_payload` | **PORTS to BEAM** (v0.3.3; was "stays Python utility") — THE load-bearing parity risk: signatures are HMACs over `json.dumps`-canonicalized bytes; the Elixir port must be byte-identical (key order, escaping, separators) or stored v2 signatures break. Gate: golden-vector parity tests (§8 golden discipline extended to signature vectors) | Pure (1.7 µs) but byte-contract-bearing |
| `dialectic_protocol.py:318-329` | `Resolution.hash` | **PORTS to BEAM** (v0.3.3; was "stays Python utility") | Callers are `execute_resolution` (§5.1; resolution.py 153/195); `:crypto.hash` native; same parity-gate family (3.3 µs) |
| `dialectic_protocol.py:378-410` | `Resolution.verify_signatures` | **DUAL** (v0.3.3): BEAM verifies at runtime; Python RETAINS verification for archival/audit reads of stored v1/v2 resolutions | Verification must outlive the port for the stored-resolution history |
| `mcp_handlers/support/condition_parser.py` | `apply_condition` | **Moves under §5.1** (v0.3.3; was §5.2 "stateless") | `async`, takes `mcp_server`, mutates agent state (resolution.py:59) — coordination glue, not computation |

### 5.4 Storage surfaces

- `core.dialectic_sessions` (`session_id TEXT PK, paused_agent_id TEXT, reviewer_agent_id TEXT, phase, status, ...`). Wave 3 BEAM session-keyed GenServer reads/writes via boundary; on-disk schema unchanged.
- `core.dialectic_messages` (append-only). BEAM appends; schema unchanged.
- `audit.coordination_events` (failure events; CHECK constraint on `event_type` prefix). Wave 3 wires dialectic state-transition emissions via the helper in §6.
- `audit.coordination_measurements` (NEW in §6, prereq PR #6). Informational metrics including lease-plane Phase A latency baseline and §3.2 503 emissions.
- `data/dialectic_sessions/<session_id>.json` (env-gated by `UNITARES_DIALECTIC_WRITE_JSON_SNAPSHOT`, default ON per `session.py:71-75`). During shadow window, BEAM does NOT write; Python continues. Post-flip: BEAM writes, Python stops. Single writer always.

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
  "mode": "synthesize" | "select_reviewer",
  "session_id": "<TEXT, idempotency key>",
  "round": <int, idempotency key for synthesize>,
  "input": { ...mode-specific bounded input... }
}
```

(Note: v0.2 listed `compare_timeout` as a third mode; v0.3 drops it because the timestamp comparison ports to BEAM per §5.3. A third `sign_resolution` mode is added ONLY if the §5.3 signing-parity fallback is taken — see the v0.3.3 changelog item 16.)

Response: `{"result": {...}, "elapsed_ms": <int>, "cache_hit": <bool>}`.

Idempotency: `(session_id, round, mode)` tuple; same input within a 60s window returns cached result. Timeout: BEAM applies 2.0s budget; on timeout, BEAM emits `coordination_failure.beam_python_boundary.beam_to_python_request_failed` with `error_class="timeout"` and fails the synthesis round (no retry at boundary; retry policy lives in saga §9).

---

## §6 Boundary instrumentation — failure vs measurement separation

The existing typed event constants `python_to_beam_request_failed` and `beam_to_python_request_failed` (PR #408) live in `audit.coordination_events`. That table's CHECK constraint (`event_type ~ '^(coordination_failure)(\.[a-z_]+)+$'`) locks `event_type` to *failure* events. Informational latency lives in a parallel channel.

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

**Retention:** monthly partitions; rolling 90-day retention. Older partitions detached and dropped by `scripts/ops/wave-0-partition-roll.sh` (cron daily at 02:00). The partition-roll script is part of prereq PR #6.

### 6.2 Failure call-sites (existing channel, `audit.coordination_events`)

Wave 3 wires `coordination_failure.beam_python_boundary.*` emissions at:
- BEAM handler-dispatch ↔ Python MCP transport: `python_to_beam_request_failed` / `beam_to_python_request_failed` on non-2xx.
- BEAM identity middleware → Python `governance_core/` math: `beam_to_python_request_failed` on Port/HTTP failure.
- BEAM dialectic GenServer → Python `/v1/dialectic/compute`: `beam_to_python_request_failed` on synthesize/select_reviewer failure.
- BEAM handler-dispatch → Python LLM SDK paths: both directions.
- Python MCP transport during cutover: `coordination_failure.governance_mcp.cutover_503_rate_breach` when sliding-window 503 rate exceeds 1% per §3.2.
- BEAM-vs-PG divergence detector (§10.4): `coordination_failure.beam_python_boundary.ets_pg_divergence` on slow reconciliation finding mismatch.

### 6.3 Measurement call-sites (new channel, `audit.coordination_measurements`)

- Existing `src/lease_plane/client.py` emits per-call lease RPC latency into
  `perf_monitor` (PR #480), and PR #481 persists acquire p50/p99 snapshots to
  `metrics.series`. If prereq PR #6 still needs
  `measurement.lease_plane.request` rows in `audit.coordination_measurements`,
  it should bridge from this recorder rather than create a parallel one-off
  client module.
- Wave 3 BEAM handler-dispatch emits `measurement.beam_python_boundary.request` on every successful boundary call, payload `{endpoint, method, elapsed_ms, payload_bytes}`. **Numerator-locus note (council fold, PR #599):** the Phase A baseline (prereq PR #6) measures *client-perceived* crossing cost — full marshal + transport + return, per §0(B)'s "full request marshalling vs lease ack" anchor. The Wave 3 emitter MUST measure its `elapsed_ms` at a comparable locus (caller-side bracket of the full boundary call), NOT plane/handler-internal time, or the ×2/×3 comparison silently breaks apples-to-oranges.
- Python MCP transport during cutover emits `measurement.governance_mcp.503_emission` on every 503 it returns, payload `{request_path, error_reason}` — input to §3.2's halt aggregator.

### 6.4 Emission helper (enforcement)

`governance_core/coordination_events_helpers.py::make_boundary_payload(endpoint, method, error_class, status_code, elapsed_ms) -> dict` raises `ValueError` on None/empty/missing `error_class`. All `coordination_failure.beam_python_boundary.*` emissions MUST go through this helper. Sibling helper `make_measurement_payload(endpoint, method, status_code, elapsed_ms, payload_bytes) -> dict` for the measurement channel. Direct dict construction is prohibited; CI lint at `scripts/dev/check-boundary-event-helpers.sh` greps for the event_type constants outside the helper modules and fails the PR. Same pattern applies to BEAM emissions (Elixir-side helper module).

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

Per-call: up to 4 boundary crossings worst-case (dialectic-touching + governance_core math), 2 best-case (no dialectic + no math). Disconfirmer (B) budget at 4× per-crossing cost is correctly worst-case-anchored.

---

## §7 Test strategy

### 7.1 Acceptance test classes

- **(a) Python suite.** All ~8400+ tests in `tests/`. Pre-cutover gate: full green.
- **(b) ExUnit suite.** New `elixir/handler_dispatch/test/`. Tests: fixture MCP request → BEAM dispatch → Python handler invoked with correctly-marshalled args; identity middleware fixture (process_agent_update with `parent_agent_id`) → asserts lineage write to PG matches `src/mcp_handlers/middleware/identity_step.py`; dialectic GenServer fixture (create → join → quorum → resolve) → asserts same `audit.coordination_events` row sequence; ETS-cache invariants (BaselineWriter and FeatureFlagWriter): single-writer GenServer is the only path that mutates ETS, reads under read_concurrency are lock-free, slow reconciliation detects PG-vs-ETS divergence.
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

Surface D writes to two coupled tables on PATH 3 fresh mint: `core.identities` AND `core.agents`. Wave 3 BEAM shadows both during the shadow window.

### 8.1 DDL

Prereq PR #1 ships:

```sql
CREATE TABLE core.identities_shadow (
    LIKE core.identities INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE core.agents_shadow (
    LIKE core.agents INCLUDING ALL,
    shadow_write_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`LIKE … INCLUDING ALL` pulls indexes, defaults, generated columns (e.g. `core.identities.metadata_tsv`), constraints. **FK note:** `INCLUDING ALL` does NOT include foreign-key constraints in PostgreSQL. The shadow tables have no FKs by default. Decision: leave FKs off the shadow tables — they are write-only audit replicas, not referential targets, and FKs would impose ordering constraints on the shadow writer that the comparator doesn't need. The decision is documented inline in the migration with a comment.

Schema drift on `core.identities` or `core.agents` would require either re-running `LIKE` or a paired migration; prereq PR #1 includes `db/postgres/schema_drift_check.sh` that fails CI if either table's shape changes without a corresponding shadow update.

### 8.2 Comparator (full outer join, all live columns, three divergence kinds)

Prereq PR #1 ships `scripts/ops/wave-3-shadow-divergence-check.sql`:

```sql
-- core.identities divergence
WITH ident_compare AS (
    SELECT
        COALESCE(c.agent_id, s.agent_id)                              AS agent_id,
        c.agent_id IS NULL                                             AS canonical_missing,
        s.agent_id IS NULL                                             AS shadow_missing,
        (c.api_key_hash             IS DISTINCT FROM s.api_key_hash)             AS api_key_hash_diff,
        (c.status                   IS DISTINCT FROM s.status)                   AS status_diff,
        (c.parent_agent_id          IS DISTINCT FROM s.parent_agent_id)          AS parent_agent_id_diff,
        (c.spawn_reason             IS DISTINCT FROM s.spawn_reason)             AS spawn_reason_diff,
        (c.metadata                 IS DISTINCT FROM s.metadata)                 AS metadata_diff,
        (c.disabled_at              IS DISTINCT FROM s.disabled_at)              AS disabled_at_diff,
        (c.last_activity_at         IS DISTINCT FROM s.last_activity_at)         AS last_activity_at_diff,
        (c.provisional_lineage      IS DISTINCT FROM s.provisional_lineage)      AS provisional_diff,
        (c.provisional_score_id     IS DISTINCT FROM s.provisional_score_id)     AS provisional_score_id_diff,
        (c.provisional_recorded_at  IS DISTINCT FROM s.provisional_recorded_at)  AS provisional_recorded_diff,
        (c.confirmed_at             IS DISTINCT FROM s.confirmed_at)             AS confirmed_diff,
        (c.lineage_declared_at      IS DISTINCT FROM s.lineage_declared_at)      AS lineage_declared_diff,
        (c.lineage_demoted_at       IS DISTINCT FROM s.lineage_demoted_at)       AS lineage_demoted_diff,
        (c.lineage_last_eval_at     IS DISTINCT FROM s.lineage_last_eval_at)     AS lineage_last_eval_diff,
        (c.chain_obs_count          IS DISTINCT FROM s.chain_obs_count)          AS chain_obs_count_diff,
        (c.lineage_archived_at      IS DISTINCT FROM s.lineage_archived_at)      AS lineage_archived_diff
    FROM core.identities c
    FULL OUTER JOIN core.identities_shadow s USING (agent_id)
)
SELECT 'identities' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_hash_diff, status_diff, parent_agent_id_diff, spawn_reason_diff,
       metadata_diff, disabled_at_diff, last_activity_at_diff,
       provisional_diff, provisional_score_id_diff, provisional_recorded_diff, confirmed_diff,
       lineage_declared_diff, lineage_demoted_diff, lineage_last_eval_diff, chain_obs_count_diff,
       lineage_archived_diff
FROM ident_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_hash_diff OR status_diff OR parent_agent_id_diff
   OR spawn_reason_diff OR metadata_diff
   OR disabled_at_diff OR last_activity_at_diff
   OR provisional_diff OR provisional_score_id_diff OR provisional_recorded_diff OR confirmed_diff
   OR lineage_declared_diff OR lineage_demoted_diff OR lineage_last_eval_diff OR chain_obs_count_diff
   OR lineage_archived_diff;

-- core.agents divergence
WITH agent_compare AS (
    SELECT
        COALESCE(c.id, s.id)                                          AS agent_id,
        c.id IS NULL                                                   AS canonical_missing,
        s.id IS NULL                                                   AS shadow_missing,
        (c.api_key                  IS DISTINCT FROM s.api_key)                  AS api_key_diff,
        (c.status                   IS DISTINCT FROM s.status)                   AS status_diff,
        (c.parent_agent_id          IS DISTINCT FROM s.parent_agent_id)          AS parent_agent_id_diff,
        (c.label                    IS DISTINCT FROM s.label)                    AS label_diff,
        (c.purpose                  IS DISTINCT FROM s.purpose)                  AS purpose_diff,
        (c.notes                    IS DISTINCT FROM s.notes)                    AS notes_diff,
        (c.tags                     IS DISTINCT FROM s.tags)                     AS tags_diff,
        (c.archived_at              IS DISTINCT FROM s.archived_at)              AS archived_at_diff,
        (c.spawn_reason             IS DISTINCT FROM s.spawn_reason)             AS spawn_reason_diff,
        (c.thread_id                IS DISTINCT FROM s.thread_id)                AS thread_id_diff,
        (c.thread_position          IS DISTINCT FROM s.thread_position)          AS thread_position_diff,
        (c.allow_rebind_after_exit  IS DISTINCT FROM s.allow_rebind_after_exit)  AS allow_rebind_diff,
        (c.allow_concurrent_contexts IS DISTINCT FROM s.allow_concurrent_contexts) AS allow_concurrent_diff
    FROM core.agents c
    FULL OUTER JOIN core.agents_shadow s USING (id)
)
SELECT 'agents' AS table_name, agent_id, canonical_missing, shadow_missing,
       api_key_diff, status_diff, parent_agent_id_diff, label_diff, purpose_diff,
       notes_diff, tags_diff, archived_at_diff, spawn_reason_diff, thread_id_diff, thread_position_diff,
       allow_rebind_diff, allow_concurrent_diff
FROM agent_compare
WHERE canonical_missing OR shadow_missing
   OR api_key_diff OR status_diff OR parent_agent_id_diff OR label_diff
   OR purpose_diff OR notes_diff OR tags_diff OR archived_at_diff
   OR spawn_reason_diff OR thread_id_diff OR thread_position_diff
   OR allow_rebind_diff OR allow_concurrent_diff;
```

Each non-empty row emits one `coordination_failure.beam_python_boundary.shadow_divergence` event with payload `{table_name, agent_id, kind, divergent_columns}`.

Hourly trigger via `scripts/ops/com.unitares.wave3-shadow-divergence-check.plist` (launchctl).

### 8.3 Load amplification before 7-day clock

Prereq PR #1 also ships `scripts/ops/wave3-shadow-replay.sh`: replays captured production traffic at 2× rate against the shadow path. **The 7-day-zero-divergence clock starts AFTER replay completes with zero events.**

### 8.4 Event type registration

`src/coordination_events.py` adds `COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_SHADOW_DIVERGENCE` and `COORDINATION_FAILURE_BEAM_PYTHON_BOUNDARY_ETS_PG_DIVERGENCE`, plus `COORDINATION_FAILURE_GOVERNANCE_MCP_CUTOVER_503_RATE_BREACH`, all into `WAVE_0_EVENT_TYPES`. `tests/test_coordination_events.py::test_event_type_constants_match_documented_set` is updated.

---

## §9 Crash-safe saga state machine

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

-- Prevent two pending sagas per session even when payload hashes differ.
CREATE UNIQUE INDEX idx_saga_one_pending_per_session
    ON coordination.session_resolution_sagas (session_id)
    WHERE state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting');

CREATE INDEX idx_saga_inflight ON coordination.session_resolution_sagas (state, last_attempt_at)
    WHERE state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting');
CREATE INDEX idx_saga_session ON coordination.session_resolution_sagas (session_id);
```

The partial unique index `idx_saga_one_pending_per_session` is the v0.3 addition (code-reviewer item 2). It enforces the invariant "at most one pending saga per session" at the DB layer; a second-saga INSERT for the same session while one is still pending now fails with a unique-constraint violation, which the session GenServer treats as a retryable conflict (ack the call without writing, await the existing saga's terminal state).

### 9.2 Forward path

1. **Reserve.** Session GenServer INSERTs saga row with `state='reserved'`. Issues `GenServer.call(:reserve_for_session_resolution, {session_id, saga_id})` to both agent GenServers.
2. **Apply paused agent.** Both ACK reservation → session GenServer issues `GenServer.call(:apply_resolution, {session_id, saga_id, payload, hash})` to **paused agent first**. On ACK, UPDATE `state='paused_agent_applied'`.
3. **Apply reviewer agent.** Session GenServer issues same call to reviewer. On ACK, UPDATE `state='both_agents_applied'`.
4. **PG commit.** Session GenServer commits `pg_resolve_session` AND UPDATEs saga to `state='pg_committed'` in a **single PG transaction**.

The two-step apply gives crash recovery a deterministic ordering — `paused_agent_applied` means exactly "paused has applied, reviewer has not yet been asked."

### 9.3 Crash recovery rules

Session GenServer init reads the most recent non-terminal saga row for its `session_id`. The exact recovery query (committed in §14 PR #7 stubs):

```sql
SELECT saga_id, state, paused_agent_id, reviewer_agent_id, created_at
FROM coordination.session_resolution_sagas
WHERE session_id = $1
  AND state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting', 'pg_committed')
ORDER BY created_at DESC
LIMIT 1;
```

The partial unique index `idx_saga_one_pending_per_session` guarantees at most one row with `state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting')`, so the `LIMIT 1` is structurally redundant for those states but defensive against operator-introduced anomaly. `pg_committed` is included so a crash between PG commit and BEAM in-process ACK is recoverable; `reverted` is explicitly excluded so a fresh retry on the same session cleanly INSERTs a new `reserved` row without colliding. **Filter discipline:** never recover on `state IN ('reverted')` — that's the terminal-already-undone state; doing so re-issues compensating reverts against already-clean agents.

Recovery action by state:

| Saga state on init | Recovery action |
|---------------------|------------------|
| `reserved` | Query each agent: `:has_reservation`. If neither → UPDATE `state='reverting'`, drop via 9.4. If at least one → UPDATE `state='reverting'`, issue compensating revert, then 9.4 |
| `paused_agent_applied` | Query reviewer: `:has_reservation`. If yes → resume forward at step 3. If no → revert paused agent + drop via 9.4 |
| `both_agents_applied` | Query each agent: `:has_applied`. If both → re-issue PG commit at step 4 (idempotent at PG layer). If either lost it (clean restart cleared in-memory) → compensating-revert path 9.4 |
| `pg_committed` | Re-issue `GenServer.cast(:commit_acknowledged, ...)` to both agents (idempotent — agents transition `applied → committed` and discard saga state). No new PG write |
| `reverting` | Re-issue `:revert_reservation` and `:revert_apply` to both agents. On both ACK → UPDATE `state='reverted'` |
| `reverted` | Terminal; no action |

### 9.4 Drop / revert path

Compensating revert when forward progress is unsafe: session GenServer issues `GenServer.call(:revert_reservation, {session_id, saga_id})` and (if applicable) `GenServer.call(:revert_apply, {session_id, saga_id})`. Both idempotent. On both ACK, UPDATE `state='reverted'`.

### 9.5 Phantom-read mitigation

Observers reading agent state via `audit.coordination_events` consumers OR `load_session_as_dict` (`session.py:261-342`) MUST treat agent state as in-flight if a non-terminal saga exists for the agent's active session:

```sql
SELECT NOT EXISTS (
    SELECT 1 FROM coordination.session_resolution_sagas
    WHERE (paused_agent_id = $1 OR reviewer_agent_id = $1)
      AND state IN ('reserved', 'paused_agent_applied', 'both_agents_applied', 'reverting')
) AS is_stable;
```

Observers that can't accept stale-with-rollback semantics call this gate; observers that can (dashboard read paths) may proceed and re-read on the next polling cycle. **Stop sign #8** halts canary if any observer surfaces stale `is_stable=true` reads during a non-terminal saga window.

### 9.6 Cross-session deadlock risk

Two concurrent sagas could in principle each hold `GenServer.call`s targeting the same agent (paused agent in session A, reviewer in session B simultaneously). `is_agent_in_active_session` (`reviewer.py:121-200`) prevents this at session creation but does not prove invariant during the saga's call window. **Stop sign #10** (new in v0.3) halts canary if the cross-session-shared-agent invariant is detected as violated during canary observation.

---

## §10 Cache coherence via BEAM-native ETS

v0.2's design used PG-versioned baselines and feature flags with bounded reconciliation; the per-observe PG read added sustained PG load to the substrate Wave 3 exists to relieve. v0.3 moves the canonical live cache into BEAM-native ETS — lock-free in-memory storage with microsecond reads — and uses PG only as durable backing storage written transactionally on writes.

### 10.1 ETS canonical-live, PG durable-canonical

Two named ETS tables, each owned by a single GenServer that is the only writer:

```elixir
# In Unitares.HandlerDispatch.BaselineWriter.start_link/0:
:ets.new(:agent_baselines, [:set, :public, :named_table, read_concurrency: true])

# In Unitares.HandlerDispatch.FeatureFlagWriter.start_link/0:
:ets.new(:feature_flags, [:set, :public, :named_table, read_concurrency: true])
```

`:public` allows any BEAM process to read directly; `read_concurrency: true` optimizes for many-readers / few-writers workloads. The `:set` type guarantees one entry per key (agent_id for baselines, flag-key for flag values).

**Read path (any BEAM handler):**
```elixir
case :ets.lookup(:agent_baselines, agent_id) do
  [{^agent_id, baseline}] -> baseline
  [] -> nil  # cold; trigger BaselineWriter.warm/1 for this agent
end
```
O(1), lock-free, no PG round-trip. Microsecond latency.

**Write path (single GenServer):**
```elixir
def handle_call({:write, agent_id, baseline}, _from, state) do
  # 1. Durable canonical write (transactional)
  case Postgrex.transaction(state.repo, fn conn ->
    Postgrex.query!(conn, "INSERT INTO core.agent_behavioral_baselines (agent_id, stats) VALUES ($1, $2) ON CONFLICT (agent_id) DO UPDATE SET stats = EXCLUDED.stats, updated_at = now()", [agent_id, baseline])
  end) do
    {:ok, _} ->
      :ets.insert(:agent_baselines, {agent_id, baseline})
      {:reply, :ok, state}
    {:error, reason} ->
      # PG write failed; ETS not updated; caller retries or surfaces.
      {:reply, {:error, reason}, state}
  end
end
```
PG-write-then-ETS-update means readers never see a value not yet in PG; if PG fails, ETS is unchanged. Single-writer GenServer means no race within a runtime.

### 10.2 Initial population and BEAM restart

On BEAM application boot, BaselineWriter and FeatureFlagWriter each run a single bulk SELECT to populate ETS:
```elixir
# BaselineWriter.init/1:
rows = Postgrex.query!(repo, "SELECT agent_id, stats FROM core.agent_behavioral_baselines", [])
Enum.each(rows, fn [agent_id, stats] -> :ets.insert(:agent_baselines, {agent_id, stats}) end)
GenServer.cast(Unitares.HandlerDispatch.Readiness, {:writer_warm, __MODULE__})
```
One-shot at startup; bounded by N agents (small for unitares-scale). Subsequent reads are ETS-only until the writer GenServer mutates.

**Readiness gate (default contract).** Handler dispatch is structurally gated on both writers signalling `:writer_warm`. The application supervision tree includes a `Unitares.HandlerDispatch.Readiness` GenServer that holds an MFA-set state `%{BaselineWriter => false, FeatureFlagWriter => false}` and refuses dispatch (returns `{:error, :not_ready}` to the transport adapter) until both flip true. Cold reads cannot happen mid-init — the boot-time window between `:ets.new` and bulk SELECT completing is invisible to handlers. The transport returns the same `503 governance_temporarily_unavailable` body during the readiness window as during full cutover (§3.2), so consumers honor the same `Retry-After`. Boot latency cost: bounded by the slower of the two writers (typically <1s at unitares-scale; bulk SELECT over ~10² agents).

**Nil-return contract (belt-and-suspenders for surfaces that legitimately tolerate stale reads).** Some BEAM surfaces — observers, dashboards, debug tooling — may bypass the readiness gate (calling ETS directly without going through handler dispatch). For those, `:ets.lookup(:agent_baselines, agent_id)` returning `[]` MUST be treated as a stale-read signal, not a fatal one. The contract:

```elixir
case :ets.lookup(:agent_baselines, agent_id) do
  [{^agent_id, baseline}] -> {:ok, baseline}
  [] -> :cold  # caller decides: skip, default, or trigger warm/1
end
```

Call sites that pattern-match on `:cold` are documented in `elixir/handler_dispatch/test/cold_ets_contract_test.exs` (PR #8 deliverable). Call sites that go through the readiness gate and STILL get `:cold` indicate a writer-side bug — emit `coordination_failure.beam_python_boundary.ets_pg_divergence` and let stop sign #11 catch it.

### 10.3 Slow reconciliation (PG → ETS divergence detector)

A scheduled job runs every 5 minutes inside the writer GenServer (`Process.send_after(self(), :reconcile, 300_000)`) and SELECTs all `(agent_id, stats)` pairs from PG, comparing against ETS. If any mismatch is found, the writer:
1. Emits `coordination_failure.beam_python_boundary.ets_pg_divergence` with payload `{key, ets_value_hash, pg_value_hash}`.
2. Updates ETS to match PG (PG is authoritative).
3. Logs and continues.

**Why slow cadence:** ETS-vs-PG divergence should never happen in steady state, because the only writer to PG for these tables is the writer GenServer itself. A divergence means either (a) an out-of-band PG write (e.g., operator running SQL by hand, or a stale Python writer that survived cutover), or (b) an ETS corruption (rare). Both are operator-investigable events, not steady-state coordination. 5 minutes is the staleness ceiling, not the steady-state correctness mechanism.

**Stop sign #11** (new in v0.3): if the reconciliation detector fires more than 3× in a 24h window, halt and investigate. A repeating divergence indicates an unknown writer.

### 10.4 Python-side reads during transition

Python compute paths (governance_core math, LLM SDK paths) are called from BEAM with the request payload — they don't read the cache directly. BEAM reads ETS, includes the value in the boundary call payload, Python receives it.

For Python paths that historically read `_baseline_cache` directly from `governance_core/ethical_drift.py:418` (`phases.py:809-820, 856-899`): these paths are part of the Wave 3 surface that ports to BEAM. After cutover, the Python reads do not exist (the surface is on BEAM). During the dual-write window, Python keeps using its in-process `_baseline_cache` and writes to PG; BEAM reads PG, populates ETS on first observe, then reads ETS thereafter. PG remains the rendezvous between runtimes during the window.

### 10.5 Feature flags

Same pattern, applied to identity honesty mode (`identity_strict_mode`, `ipua_pin_check_mode`):

```sql
CREATE TABLE core.feature_flags (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by  TEXT
);

INSERT INTO core.feature_flags (key, value) VALUES
    ('identity_strict_mode', 'false'),
    ('ipua_pin_check_mode',  'enforced');
```

**No version column needed** — ETS holds the live value; PG is the durable canonical written on every flag mutation. Concurrent-safe write via `UPDATE core.feature_flags SET value = $1, updated_at = now(), updated_by = $2 WHERE key = $3`. The single-writer GenServer (FeatureFlagWriter) serializes flag mutations in BEAM; the UPDATE is atomic at PG layer for the single-row case.

Boot: `SELECT key, value FROM core.feature_flags` populates ETS.

Read: `:ets.lookup(:feature_flags, key)`. Microsecond.

Python compute paths receive the flag value as request payload from BEAM (BEAM checks the flag at handler entry and includes the relevant flag values in the boundary call).

Reconciliation: every 5 minutes, same pattern as §10.3.

### 10.6 What this design eliminates

- **Per-observe PG read.** Eliminated. Reads are ETS only.
- **Pub-sub lossiness.** Eliminated. No pub-sub layer; ETS is the in-runtime broadcast (any BEAM process reads the writer's update on next access).
- **Cross-runtime cache divergence in steady state.** Eliminated post-Wave-3 (BEAM is the sole reader/writer for these surfaces; Python paths don't exist).
- **Lost-update bugs.** Eliminated. Single-writer GenServer per ETS table.

### 10.7 What this design accepts

- **5-minute staleness ceiling** if an out-of-band PG write happens. Acceptable — operator-investigable, not steady-state.
- **BEAM-only access** to the live cache. Python reads via boundary call (already the topology). No Python ETS-equivalent maintained.
- **One-shot bulk SELECT at BEAM startup.** Cost: bounded by agent count; negligible at unitares scale; happens at boot, not per request.

---

## §11 Exit criteria (Go/No-Go for Wave 3 close)

Each criterion names its measurement source. If any source is missing at gate, gate halts (no fallback default).

1. **Wave 2 has closed.** Per Wave 2 handoff 2026-05-08; satisfied.
2. **21-day production traffic on BEAM.** Handler dispatch on BEAM has served production governance MCP traffic for ≥21 days continuous.
3. **Zero coordination-class incidents.** `audit.coordination_events` filtered to `coordination_failure.beam_python_boundary.*` shows zero incidents over the 21-day window AND no new substrate-tax pattern at the Python-handler-body boundary.
4. **(A.1 / ODE-floor gate)** ODE profile lands BEFORE Wave 3 implementation starts; result shows <60% of `process_agent_update` p99 floor in `governance_core/` math. Failure → halt and roadmap re-opens. Sensitivity test at 50% / 65% / 75% if result lands within ±5% of 60%. Source: prereq PR #1.
5. **(B / boundary-cost gate)** `audit.coordination_measurements` filtered to `measurement.beam_python_boundary.*` shows p50 < lease-plane Phase A measured p50 × 2 AND p99 < lease-plane Phase A measured p99 × 3 over 21-day window. Sustained breach halts. The boundary budget includes §4(β) advisory-lock per-write cost and §9 saga PG transaction cost. Source: prereq PR #6 (must produce ≥14 days of data before thresholds can be set).
6. **(A.2 / in-place-fix gate)** if any Python in-place fix shipped during implementation window brought `process_agent_update` p99 below 2.0s, **gate fires pre-canary-100%**.
7. **(C / MCP SDK gate)** hands-on spike on `mcp_elixir_sdk` 1.0.1 OR `hermes_mcp` 0.14.1 recorded in `docs/handoffs/wave-3-mcp-sdk-spike-<date>.md` before implementation gate; result not "production-disqualifying."
8. **(D / state-ownership gate)** §3 surface-by-surface analysis at gate finds no irreducible per-request semantics beyond the eight surfaces.
9. **(E / opportunity cost gate)** operator's `docs/proposals/wave-3-go-decision-<date>.md` includes §"Calendar reasoning" naming current slip vs original target on each of {paper, fellowship, HLH, R2 Phase 2}; no item slips >25% of original deadline window. **No acceptance-memo escape.** Wave 1 elapsed time concretely named in the document; (E)'s "× 3" cap derives from the actual measured Wave 1 elapsed.
10. **(F / dialectic-quality gate)** session-resolution rate regression ≤5% AND reviewer-reassignment rate increase ≤20% vs baseline. Baseline (mean + σ) computed from trailing 30 days of `core.dialectic_sessions`; pinned in this §11 prior to implementation start (prereq PR #9). Gate halts if baseline volume insufficient (<30 sessions in window).
    **Pin (prereq PR #9, 2026-06-11 — `docs/handoffs/wave-3-dialectic-baseline-2026-06-11.md`):** trailing 30-day window held **1 session** → **(F) halts on its own volume haltspec; no 30-day mean/σ pinnable.** Context: 90-day n=9 → resolution 0.556 (binomial σ≈0.166); all-time n=48 → 0.646; monthly volume collapsed ~20/mo (2025-12) → ~1/mo (2026-05), coupled to the dialectic-rework arc — volume recovery is upstream of this gate. **Second-metric source created:** reviewer-reassignment rate previously had NO event stream (zero `%reassign%` rows in `audit.events` all-time; transcript-only — a violation of §0's source-naming discipline). `_apply_reviewer_reassignment` now emits `dialectic_reviewer_reassigned` (the single chokepoint for both the explicit reassign tool and the stuck-reviewer auto path); the reassignment baseline accrues from that deploy forward. Re-pin when any trailing-30d window holds ≥30 sessions.
11. **Operator-led behavioral parity.** Existing Watcher / Sentinel / SDK clients hit governance MCP with no behavioral diff; REST contract preserved per §7.2 byte-equivalence definition.
12. **Test-class green.** ExUnit + Python + integration + golden-response-parity classes all green at gate.

---

## §12 Stop signs

Inheriting parent roadmap stop signs #1–#4, plus Wave-3-specific:

- **#5** Identity-middleware port surfaces a coordination shape Wave 1+2 didn't expose. Halt before canary advance.
- **#6** Dialectic split per §5 turns out ungratified — a function classified as "computation" mutates state across calls. Re-classify, possibly re-split.
- **#7** Sliding-window 503 rate during cutover/rollback exceeds 1% for >60s (per §3.2). Halt; complete step 3 (restore Python writers) before stopping.
- **#8** Any observer surfaces stale `is_stable=true` reads during a non-terminal saga window without checking the §9.5 gate. Halt canary advance.
- **#9** *(retired in v0.3 — pub-sub no longer load-bearing under §10's ETS design)*
- **#10** Cross-session shared-agent invariant detected as violated during canary observation (`is_agent_in_active_session` failed to prevent two sagas targeting the same agent). Halt; re-derive serialization at session creation.
- **#11** §10.3 ETS-vs-PG reconciliation detector fires more than 3× in a 24h window. Halt; investigate the unknown writer producing out-of-band PG mutations.
- **#12** `audit.coordination_measurements` partition pressure: row insertion rate exceeds 10× the lease-plane Phase A baseline for >24h, OR partition-roll cron fails to drop a partition within 7 days of its drop window. Halt; review retention policy and emission rate.

---

## §13 What Wave 3 deliberately does NOT do

- Does not port `governance_core/`. Math stays Python.
- Does not port the MCP transport layer. Stays Python until disconfirmer (C) is run hands-on.
- Does not port the LLM SDK call paths. Anthropic/OpenAI/Ollama call paths inside handlers stay Python, called from BEAM via Ports/HTTP.
- Does not port Watcher. Single-shot LLM pattern matcher; no coordination shape.
- Does not modify the `lease_plane` schema. Wave 3's new state lives in BEAM ETS (live) + new PG schemas/tables (durable): `coordination` schema (§9), `core.identities_shadow` + `core.agents_shadow` (§8), `audit.coordination_measurements` (§6), `core.feature_flags` (§10).
- Does not open `surface-lease-plane-v0.md` Phase B for `resident:/`; PR #476
  already did that. Does not extend Phase B to any new agent-state or cutover
  surface_kind until §4 option (α) names it explicitly.
- Does not version columns in `core.agent_behavioral_baselines` or `core.feature_flags`. v0.2's version-counter approach is retired in favor of single-writer ETS canonical + PG durable.

---

## §14 Implementation prereq PRs

All ten prereq PRs land BEFORE any commit in `elixir/handler_dispatch/` or any new `elixir/` tree on the implementation branch. CI lint check `scripts/dev/check-wave3-ode-prereq.sh` enforces.

| # | PR | Creates / modifies | Depends on |
|---|-----|---------------------|------------|
| 1 | ODE profile + shadow DDL + comparator + event_types + §5.2 boundary-cost audit | `db/postgres/migrations/0NN_identities_shadow.sql`, `db/postgres/migrations/0NN_agents_shadow.sql`, `src/coordination_events.py` (shadow_divergence, ets_pg_divergence, cutover_503_rate_breach, governance_mcp_request denominator constants), `tests/test_coordination_events.py`, `scripts/ops/wave-3-shadow-divergence-check.sql`, `scripts/ops/com.unitares.wave3-shadow-divergence-check.plist`, `scripts/ops/wave3-shadow-replay.sh`, ODE profile commit. **§5.2 audit deliverable** (v0.3.2 fold of architect CONCERN on under-counting habit): for every helper in §5.2's "stays Python" table, profile boundary-vs-compute cost on the live request mix; reclassify to PORTS-to-BEAM any helper where boundary crossing dominates compute (the `_compare_against_timeout` pattern). Output written into `docs/handoffs/wave-3-section-5-2-boundary-audit-<date>.md`; reclassifications fold into §5.3 in a v0.3.x patch. CI gate `scripts/dev/check-wave3-ode-prereq.sh` enforces the audit artifact exists before any commit in `elixir/handler_dispatch/`. | — |
| 2 | FeatureFlagWriter (BEAM ETS + GenServer) + `core.feature_flags` migration | `db/postgres/migrations/0NN_core_feature_flags.sql`, `elixir/handler_dispatch/lib/feature_flag_writer.ex`, boundary endpoint `POST /v1/feature_flag/get` for Python reads during transition, ExUnit tests for ETS-PG single-writer invariant | #1 (event_type set) |
| 3 | `coordination_events_helpers.py` + Elixir helper + CI lint | `governance_core/coordination_events_helpers.py` (`make_boundary_payload`, `make_measurement_payload`), Elixir helper module, `scripts/dev/check-boundary-event-helpers.sh` (CI lint) | — |
| 4 | IPUA pin integration test | `tests/integration/test_identity_path2_ipua_pin_pipeline.py` | — |
| 5 | Golden-capture fixture + capture script + masking + parity test | `tests/fixtures/wave3_response_golden/` (50+), `scripts/dev/wave3-capture-goldens.sh`, `tests/integration/test_wave_3_response_parity.py` | — |
| 6 | Lease-plane Phase A latency instrumentation + measurement table + retention | `db/postgres/migrations/0NN_audit_coordination_measurements.sql`, `src/lease_plane/client.py` (existing recorder; bridge/emission to `measurement.lease_plane.request`), `scripts/ops/wave-0-channel-report.sh` (read both tables), `scripts/ops/wave-0-partition-roll.sh` + plist (90-day retention). Runs ≥14 days before disconfirmer (B) thresholds set | #3 (`make_measurement_payload`) |
| 7 | Saga DDL + state machine | `db/postgres/migrations/0NN_coordination_session_resolution_sagas.sql` (CREATE SCHEMA + CREATE TABLE + partial unique index), Python interface stubs for tests | — |
| 8a | BaselineWriter stub + Readiness GenServer + cold-ETS contract test | `elixir/handler_dispatch/lib/baseline_writer.ex` (skeleton: GenServer scaffolding, `:writer_warm` signal, no PG read yet), `elixir/handler_dispatch/lib/readiness.ex` (the readiness gate from §10.2), `elixir/handler_dispatch/test/cold_ets_contract_test.exs` (the `:cold` return contract; matches §10.2 belt-and-suspenders spec). No production wiring; allows the §10.2 invariants to ship and be tested before measurement data accrues. | #2 (FeatureFlagWriter pattern) |
| 8b | BaselineWriter wiring + boundary endpoint + slow-reconciliation cron | Wires `BaselineWriter` to PG (bulk SELECT on init, write path per §10.1), boundary endpoint `POST /v1/baseline/get` for Python reads during transition, slow-reconciliation cron, ExUnit tests for ETS-PG single-writer invariant + reconciliation divergence detection. **Gated on PR #6 14-day window**: `scripts/dev/check-wave3-prereq-data-window.sh` (CI lint, added in this PR) verifies ≥14 days of `measurement.lease_plane.request` rows in `audit.coordination_measurements` before this PR can merge. Enforces criterion §11.5 mechanically rather than as a Go-decision document obligation. | #6 (14-day data), #8a |
| 9 | Dialectic baseline pinning artifact | `docs/handoffs/wave-3-dialectic-baseline-<date>.md` with mean + σ for resolution rate and reassignment rate over trailing 30 days from `core.dialectic_sessions`; **executed 2026-06-11** — also shipped the reassignment-rate event stream (`_apply_reviewer_reassignment` emits `dialectic_reviewer_reassigned`; the metric previously had no `audit.events` source) + tests; §11 criterion-10 pin records the volume-halt | — |
| 10 | SDK consumer retry-on-503 (Watcher/Sentinel/dispatch worker/plugin) + 503-emission measurement | Consumer-side retry logic honoring `Retry-After` or `retry_after_seconds`, `src/mcp_transport.py` emits `measurement.governance_mcp.503_emission` on every 503, sliding-window 503-rate aggregator + breach event emitter | #1, #6 |

PR #1 lands first (it's the disconfirmer A.1 anchor AND the §5.2 boundary-cost audit anchor). PRs 2–7, 8a, 9–10 land in dependency order shown. PR #8b is gated on PR #6 producing ≥14 days of `measurement.lease_plane.request` rows; CI lint `check-wave3-prereq-data-window.sh` enforces this mechanically rather than relying on Go-decision documentation. If the Wave 3 implementation gate is reached before PR #6 has 14 days of data, both criterion §11.5 AND the CI lint halt the impl branch.

Per disconfirmer (E): if these ten + their council passes consume more than (Wave 1 elapsed × 3) calendar-weeks, halt and re-evaluate. Wave 1 elapsed time is concretely named in PR #9's gate document.

---

## §15 Council pass — pending v0.3

Three lanes scheduled in parallel per `feedback_design-doc-council-review.md` and `feedback_council-adversarial-prompt.md`:

- **dialectic-knowledge-architect** — adversarial on §0's disconfirmer set (does each threshold actually anchor to a measurement source?), the §10 ETS-pivot rigor (is it actually free of substrate-tax pile-on, or did v0.3 introduce a new bias signature?), the §5.3 boundary-cases reclassification.
- **feature-dev:code-reviewer** — adversarial on §8 comparator (every column accounted for?), §9 saga state machine + new partial unique index (every crash point recoverable + no concurrent-INSERT footgun introduced?), §10 ETS pattern (BEAM startup race? PG-write-then-ETS-update vs ETS-write-then-PG-write trade-off?), §3.2 503 halt mechanism (counter durable, halt direction unambiguous, retry policy implementable?).
- **live-verifier** — adversarial on every named file:line, endpoint, schema column, table, plist, and runtime claim. Cross-checks against running governance-mcp + lease-plane + audit.events schemas. Specifically: confirm every column referenced in §8 matches live schema; confirm `core.agent_behavioral_baselines` shape is `(agent_id, stats, updated_at)` per §10's read query; confirm no `version` column is referenced anywhere in v0.3 (vs v0.2's surfaces).

If the v0.3 council pass returns BLOCK on any item, the discipline is not another amendment fold — v0.4 is the next step. Each redraft cycle must produce a structurally cleaner doc; if v0.3 also surfaces a new bias signature, that's evidence the substrate question itself needs re-litigation rather than redraft mechanics.

---

## §16 Open follow-on (not Wave 3 scope)

The substrate-tax bug class is structural to anyio + asyncio + asyncpg / Redis on a shared event loop (per `CLAUDE.md` §"Substrate Tax: anyio-asyncio Coupling"). Wave 3 dissolves it on the Wave 3 surfaces; remaining Python surfaces (governance_core compute, LLM SDK paths, Watcher, MCP transport) still live on the same substrate. Post-Wave-3 measurement decides whether to continue porting or pause.
