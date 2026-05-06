# Identity Ontology — Resolution Plan

**Companion to:** `docs/ontology/identity.md` (v2)
**Purpose:** Organize the open questions, research agenda, and system implications from the ontology doc so each item has a state, a dependency map, and a definition-of-done.
**Status:** Draft. No item below is a commitment until its row is explicitly accepted.

---

## Ledger

Every item from `identity.md` that requires work, what "resolved" means for it, and what it depends on.

### Open questions

| ID | Question | Depends on | Resolved when |
|---|---|---|---|
| Q1 | Trajectory portability — inheriting identity or data? | R2 (honest memory integration must be defined) | A mechanical definition of "integration" exists; Q1 answerable as a function of whether a given inheritance path uses that mechanism. |
| Q2 | Subagent ephemerality — principled or pragmatic? | R1 (behavioral-continuity verification — sets the "N observations" threshold) | Once R1 defines a minimum observation count for earned lineage, subagents measurably fall below it; parent-verification substitute is then formally principled. |
| Q3 | Paper positioning — v7 thesis or implementation detail? | Nothing. Pure re-read. | **Resolved 2026-04-21** — recommendation (v7 animating thesis) at `docs/ontology/paper-positioning.md` accepted by Kenny. Downstream work: v7 outline draft in `unitares-paper-v6` repo, timing TBD. |

### Research agenda (inventive stance)

| ID | Item | Depends on | Resolved when |
|---|---|---|---|
| R1 | Behavioral-continuity verification as primary identity primitive | None (design from scratch) | **Design doc v3 landed 2026-04-24** at `docs/ontology/r1-verify-lineage-claim.md` after two council-review passes. Implementation shipped through foundation, core scoring, calibration/provisional helpers, trust-tier provisional gate, onboard marks-policy wiring, and public KG emission. **2026-05-05 maintenance follow-up:** operator-facing promotion + public KG 30-day archival sweeps added (`src/identity/r1_maintenance.py`, `scripts/migration/r1_lineage_maintenance.py`); live promotion sweep evaluated 3 provisional rows, confirmed 1, left 2 blocked as inconclusive, and reported 0 orphan candidates; public KG TTL dry-run found 0 stale score nodes. **R1 v3.4 shipped 2026-05-05:** unsupported scores stay report-only as `orphan_candidate` by default, and explicit operator demotion is now available via `sweep_provisional_lineage(apply_orphans=True)` / CLI `promote-provisional --apply-orphans`, using R2's existing `demote_lineage(successor_id, reason="r1_unsupported")` primitive plus a storage-confirmed `lineage_demoted` audit event. **Still open:** remaining downstream consumer wiring after real reader exists (#341 deferred R3 baseline consumer). Handoff snapshot at `docs/handoffs/2026-05-03-r1-implementation-handoff.md`. |
| R2 | Honest memory integration | R1 (verification underpins integration checks) | **Design doc v2 landed 2026-05-02** at `docs/ontology/r2-honest-memory-integration.md` after parallel three-agent council pass (`dialectic-knowledge-architect` + `feature-dev:code-reviewer` + `live-verifier`). v1→v2 driven by convergent forcing items: (1) recommendation A retroactive trust-tier crediting violated the asymmetry claim at the *interpretive* layer — switched to **forward-only chain crediting** in v2 (chain counter starts at 0 at promotion; no retroactive credit of parent's accrued history); (2) v1 conflated R1's trajectory-similarity score with axiom #12's "memory operative on behavior" — v2 explicitly frames R1 as **necessary-but-not-sufficient** with R5 as the load-bearing follow-on for replay discrimination; (3) multi-generation chains specified per-link only (no transitive trajectory windowing or transitive trust-tier inheritance); (4) R1 confirmed entirely unimplemented at runtime — v2 added an explicit implementation-status table, updated 2026-05-05 after runtime ship; (5) `provisional_lineage` column confirmed nonexistent at design time — R1 later shipped it in migration 031 and R2 reused it; (6) check-in-triggered eval would deadlock under anyio — v2 specifies `create_tracked_task` deferred dispatch pattern. v2 also adds cross-role pre-check at declaration (rejects with `lineage_cross_role_rejected` instead of silently demoting downstream), completes the FSM (cross-role rejection branch + confirmed→demoted clawback), pairs onboard-response addition with identity-response addition, and renames `lineage_archive_grace_expired → lineage_grace_expired` for naming consistency. **Operator decisions 2026-05-02** stand: (a) cross-role lineage = reject at declaration; (b) KG cite-and-extend = defer to v1.1 after Phase 1 telemetry; (c) confirmed→demoted = clawback in v1. **Phase 1 implementation shipped 2026-05-05 in #357** from `docs/handoffs/2026-05-04-r2-implementation-plan.md`: migration **036** + storage helpers (`declare_lineage`, `demote_lineage`, `archive_lineage`, `increment_chain_obs_count`, `stamp_lineage_eval`, `read_lineage_state`, `select_lineage_eval_candidates`), `src/identity/lineage_lifecycle.py` FSM + audit events, cross-role pre-check + `lineage_state`/`provisional_lineage` response fields, 30-minute `lineage_eval_sweeper_task`, and `process_agent_update` trigger. Reconciliation surfaced: R1 PR #306 (migration 031) already shipped `provisional_lineage`, `provisional_score_id`, `provisional_recorded_at`, and `confirmed_at`; `confirmed_at` satisfies what the design doc called `lineage_promoted_at`; migration slot 035 was taken by coordination events, so R2 uses 036. **Phase 2** (downstream consumers: trust-tier S6, KG provenance S7, R3 baselines, dashboard) is a separate telemetry-gated row after ≥4 weeks Phase 1 data per design doc §"Shadow-mode calibration path" (≥50 promoted pairs + ≥10 demoted pairs + ≥1 rejected-cross-role event observed). |
| R3 | Statistical lineage (identity as integral) | None. Partly already present in trust-tier logic. | **Annotation pass landed 2026-04-21** — `src/trajectory_identity.py` audited; classification + migration path in this file's Appendix entry "2026-04-21 — R3: Trust-tier annotation". Math primitives are subject-agnostic; storage + tier computation assume UUID continuity. Migration unblocks S6/S7/S10. |
| R4 | Substrate-earned identity (Lumen's pattern, formalized) | None. Tractable first. | **Draft v1 landed 2026-04-21** as appendix of `docs/ontology/identity.md` ("Pattern — Substrate-Earned Identity"). Three conditions (dedicated substrate, sustained behavior, declared role); test cases (Lumen passes; synthetic fakes fail); open questions on N, envelope width, substrate migration. Open for revision. |
| R5 | Memory-deepening-reality tooling (axiom #14) | R2 (integration must be defined before deepening it) | **Design doc v0.1 landed 2026-05-05** at `docs/ontology/r5-memory-deepening-reality.md`. First channel selected: KG cite-and-extend, because existing KG `response_to` / provenance plumbing gives an orthogonal signal to R1 trajectory similarity without a new storage prerequisite. **Shadow scorer landed 2026-05-06** in `src/identity/memory_integration.py`, with synthetic coverage in `tests/test_memory_integration.py` and read-only operator diagnostic `scripts/diagnostics/score_r5_memory_integration.py`. It returns seeded verdicts (`integrated_candidate`, `weak_signal`, `absent`, `insufficient_parent_memory`, `inconclusive`) and raw counts; no schema migration, audit write, or R2 hot-path call. **Batch sampler + first live sample also landed 2026-05-06:** provisional pairs = 4 scored (3 insufficient parent memory, 1 absent); confirmed pairs = 3 scored (3 insufficient parent memory). Audit table/R2 conjunct remain unwarranted until the corpus produces non-sparse shadow evidence. Deferred channels remain forced re-derivation, behavioral backtests, and self-knowledge reflection. |
| R6 | Harness-substrate plurality — model identity across variable harness/model/transport/tool/memory layers | None; informs S7/S22 and gives R2 better vocabulary | Draft plan landed 2026-04-29 at `docs/ontology/harness-substrate-plurality.md`. **Episode-fork response-shape decision v2 landed 2026-05-02** at `docs/ontology/r6-episode-fork-response-shape.md` after parallel three-agent council pass (architect + code-reviewer + live-verifier; all three returned "withhold pending v2" on v1's 5-value enum + spawn_reason allowlist + onboard-side scope). v2 narrowed initial scope to `process_agent_update` enrichment while onboard-side rebuild waited on a `build_fork_context` call-site bug and `force_new` thread-position policy. The call-site prerequisite shipped 2026-05-02 in PR #284 (`eedf5203`) and is pinned by `tests/test_thread_identity.py::test_handler_call_site_signature_contract`. **The `force_new` thread-position prerequisite shipped 2026-05-05**: explicit `thread_id` wins, otherwise declared parent thread wins, otherwise session-derived thread; `resolve_session_identity` now persists `thread_id/thread_position` through `core.agents`, `core.identities.metadata`, and eager metadata hydration, with regression `tests/test_identity_handlers.py::TestHandleOnboardV2::test_onboard_force_new_parent_joins_parent_thread`. **R6 v2.1 shipped 2026-05-05**: `onboard()` rich `thread_context` now gets the same top-level `episode_fork_kind`, `identity_lineage_fork`, and R6 honest-message semantics as `process_agent_update`; existing rich keys (`spawn_reason`, `predecessor`, `thread_size`, `is_root`, `is_fork`) are preserved. Shared helpers live in `src/thread_identity.py`; `process_agent_update` reuses them via `src/mcp_handlers/updates/enrichments.py`; `onboard()` passes the current `agent_uuid` to distinguish child UUID forks from self-parent substrate restarts. Classifier uses structural rule (`has_child_uuid AND parent_uuid → identity_lineage`) with narrowed sync-race fallback (`subagent`/`compaction` only); parentless `new_session`/`explicit` are descriptive, not lineage evidence. Lumen substrate-earned restart remains an intentional collapse to `sibling_locus`. Honest-message language passes through R2 v2's axiom-#12-aware filter. **Operator decisions 2026-05-02** stand: (a) build_fork_context call-site fix separate prerequisite — resolved by #284; (b) sync-race detection = warning log only in v1, promote to audit event in v1.1 if Phase 1 shows non-trivial frequency; (c) `substrate_restart` enum value = defer; intentional collapse stands. **Next R6 work:** H1/H3 dogfood + broader candidate envelope still gated on more controlled fresh-process/profile runs. |
| R7 | BEAM coordination kernel — operationalize live surface ownership, leases, typed absence, and handoff semantics across agents | R6, S22, coordination-leases dialectic | **Resolved 2026-05-02.** Draft plan landed 2026-04-30 at `docs/ontology/beam-coordination-kernel.md`. Repo-placement decision: keep it inside the `unitares` repo as a service boundary, not a standalone repo; current implementation path is `elixir/lease_plane/`, with `services/coordination_kernel/` only future packaging hygiene. Phase 1 in-memory OTP proof implemented in `elixir/lease_plane/lib/unitares_lease_plane/{surface_registry,lease_process}.ex`; tests in `elixir/lease_plane/test/surface_registry_test.exs` prove acquire/status, self-idempotency, typed conflict, release, non-holder rejection, TTL expiry, and 50-racer exactly-one-winner. |

### System implications (descriptive stance)

| ID | Item | Action type | Depends on | Resolved when |
|---|---|---|---|---|
| S1 | `continuity_token` as resume-credential | **Deprecate with grace period** — **plan doc shipped 2026-04-24** at `docs/ontology/s1-continuity-token-retirement.md` (reviewed by dialectic-knowledge-architect + code-reviewer council). Surfaces Part-C coupling (token is load-bearing for PATH 0 anti-hijack, 2026-04-18) that makes naive retirement unsafe. Options A (narrow TTL — recommended pragmatic first step, honestly labeled "performative, narrowed"), A′ (narrow + PID/nonce bind — ontology-clean follow-on), C (substrate-only). Option B (wait for R1) **withdrawn 2026-04-24** after R1 v3 scope clarification — R1 is plausibility scoring, not authentication, and does not replace the token's anti-hijack role. Forward-compat `ownership_proof_version` field designed to make A → A′ non-breaking. **Operator decisions accepted 2026-04-25** (path A → A′; TTL 1h; grace = one release cycle warning-only; `bind_session` let-propagate; Chronicler re-onboard-on-wake acceptable; field rename deferred to S1-d post-grace; ontology label "performative, narrowed"). Implementation sequencing (S1-a → S1-b → S1-c → S1-d → A′) stands per §9. **S1-a shipped 2026-04-29** — full chain merged in one PR after S21-a/b unblocked the wiring path: TTL shrink + opv injection (already in master pre-2026-04-29), shared `_emit_continuity_token_deprecation` helper landed, wired across `handle_onboard_v2` (refactored from inline), `handle_identity_adapter`, `handle_bind_session`; HTTP REST inherits transitively through `execute_http_tool` dispatch (item 3 verified obsolete). `_CLOCK_SKEW_TOLERANCE = 30s` added to `resolve_continuity_token` per §7.2 (bounded below 60s minimum TTL). 6 new regression tests (clock-skew window, expiry-mid-call, concurrent-possessors-with-expired, Chronicler-shape >1h-old token, plus 2 wiring-presence tests). Operator pairs ship with secret rotation per §11.7. | None. Scope clear. | S1-a: TTL shrink + deprecation-warning + `ownership_proof_version` ✅ shipped 2026-04-29 to `unitares` master. S1-b: onboard-helper + CLI startup-path migration to `force_new + parent_agent_id` shipped to plugin + unitares. S1-c (post-grace): cross-process-instance reject. S1-d (optional): field rename `continuity_token → ownership_proof`. A′: PID/nonce binding bumps `ownership_proof_version` to 2. |
| S2 | `.unitares/session.json` auto-resume | Retire (Claude Code channel) | S1 | Auto-resume removed from Codex plugin + Claude Code harness hooks. Fresh processes mint fresh identities with declared lineage. |
| S3 | Cross-channel token acceptance | Retire | S1 | Token's `ch` claim enforced; mismatch = force-new with lineage. |
| S4 | Label-as-identifier flows | **Mostly resolved** (2026-04-17 `resolve_by_name_claim` cleanup + 2026-04-21 audit in `audit-notes.md`) | None | Outstanding effective action narrows to S5; remaining sites are cosmetic label-to-UUID translation. Verify no regressions. |
| S5 | `resident_fork_detected` event | **Resolved 2026-04-23** | R4 | Event inverted in `src/mcp_handlers/identity/persistence.py:set_agent_label` — fires only when a persistent-label collision occurs *without* the new agent declaring `parent_agent_id=<existing_uuid>`. Lineage-declared restarts log at INFO with `[RESIDENT_LINEAGE]`; broadcast payload gains `declared_parent` for consumer taxonomy. Signal chosen: declared `parent_agent_id` (the substrate commitment available at onboard time — full `verify_substrate_earned` fails fresh processes on condition 2). Tests in `tests/test_resident_fork_detector.py`. |
| S6 | Trust-tier calculation (`compute_trust_tier`) | **Partially resolved 2026-04-23** — Option B routing + Q2 reseed primitive shipped; onboard-flow wiring follow-up shipped same day | R3 | `resolve_trust_tier` in `src/identity/trust_tier_routing.py` shortcuts substrate-earned agents (R4 three-condition pass) to tier=3; session-like agents continue through `compute_trust_tier` unchanged. Lineage-seeded genesis primitive `seed_genesis_from_parent` in `src/trajectory_identity.py` (Q2); wired into onboard via `_seed_genesis_from_parent_bg` in `src/mcp_handlers/identity/handlers.py`, scheduled alongside `_create_spawned_edge_bg` on both `created_fresh_identity` and `force_new` branches. Remaining: empirically recalibrate session-like thresholds once S8a tag-discipline lands. |
| S7 | KG provenance (`agent_id` stamping) | Audit + shift aggregation | R3 | Queries and aggregations that assume multi-session UUID continuity migrated to role or lineage-chain. Schema audit of `knowledge_graph_postgres.py` and `knowledge_graph.py`. |
| S8 | Orphan archival heuristics (`classify_for_archival`) | **Re-scoped** (2026-04-21 audit in `audit-notes.md` — thresholds are fine; real gap is tag discipline) | None urgent | Split into S8a (tag-discipline audit) + S8b (class-tag backfill). Heuristic thresholds remain as-is. |
| S8a | Tag-discipline audit — 96% of active agents lack class tags | **Resolved 2026-04-23** — findings doc shipped | None | Root cause: only the SDK resident-branch stamps tags via `update_agent_metadata`; onboard defaults to `metadata={}` and no inference fires. See `docs/ontology/s8a-tag-discipline-audit.md` for measurements, write-path trace, and phased recommendation (default-stamp at onboard + later promotion sweep). **Phase-1 rule set shipped 2026-04-23 (PR #121).** **Operator decisions accepted 2026-04-25**: (a) `session_like` class addition ratified — needs scale-map entry in `governance_config.py`; (b) Phase 2 sweep timing — wait until ≥1 week of Phase-1 data (~2026-04-30 earliest) before drafting promotion thresholds; (c) Phase 2 rule for `ephemeral → session_like` promotion — defer to post-data so thresholds are empirical not guessed; (d) backfill 3180 archived records — same rule, one-shot pass once Phase-2 ratifies. Phase-2 not yet opened. |
| S8b | Class-tag backfill on active agents | **Resolved 2026-05-05** | S8a findings | Operator data-op run from Codex: `scripts/migration/s8a_phase2_backfill.py --stamp-untagged` stamped 16 active untagged identities; `--promote --include-archived` then promoted 4 eligible identities at threshold 3. Follow-up dry runs for both operations returned zero candidates. |
| S8c | `spawn_reason` write-path repair — 0 of 19 lineage-declared active agents recorded one | **Surfaced 2026-04-25** by post-acceptance council pass | None urgent | The `spawn_reason` field is declarable at onboard but no live agent records it (S8a §"Gaps worth noting"). R1 v3.1's shadow-mode calibration partition keys on `spawn_reason ∈ {new_session, subagent, compaction}`; without the plumbing repair, R1's calibration is degenerate the same way S6's session-like partition was. **Hard blocker for R1 implementation row.** Root cause (found 2026-04-25): two surfaces — (a) `upsert_identity` never wrote `spawn_reason` to `core.identities` (the column existed in schema but was never plumbed through), and (b) `upsert_agent`'s ON CONFLICT clause omitted both `spawn_reason` and `parent_agent_id` from the update list. Resolved when: the onboard write path verifiably stamps `spawn_reason` whenever passed; ≥1 week of new agents records it across `new_session` + `subagent` cases for R1 to calibrate against. **Merged 2026-04-25** as [unitares#155](https://github.com/CIRWEL/unitares/pull/155) — fix to both surfaces + 6 regression tests. |
| S9 | PATH 1/2 anti-hijack machinery | Re-scope or retire | R1 | Under R1, external verification replaces continuity-enforcement. PATH 1/2 flip to lineage-plausibility checks or retire. **`bind_session` TTL-coupling parked here 2026-04-25** — under S1-a's TTL shrink (already shipped at `_CONTINUITY_TTL = 3600`), `bind_session`'s acceptance window silently changed from 30d to 1h. Must be addressed when S9 is scoped; should not silently propagate without a regression test asserting the new TTL. |
| S10 | Fleet calibration aggregation paths | Shift default unit | R3, S7 | Default aggregation unit shifts from UUID to role. Dashboards + external-consumer contracts updated. |
| S11 | SessionStart / onboard default behavior (the teeth of the ontology) | **Resolved 2026-04-21** | Audit + plugin PR | Landed as `unitares-governance-plugin#17` (commit `743952ab`) — banner inversion, cache becomes lineage-only (schema_version=2, no continuity_token write), S1 deprecation breadcrumb. Audit + duplicate-PR dogfood story in this file's Appendix entries "2026-04-21 — S11" (initial) and "2026-04-21 — S11 execution" (landing + lesson). |
| S11-a | Skill text drift from S11 contract — `commands/governance-start.md` still teaches `continuity_token` cache pattern | **Primary fix shipped 2026-04-25** as `unitares-governance-plugin@ad4dfef` (`fix(commands/governance-start): align with v2 identity ontology + S11/S20 cache contract`). Plan doc at `docs/ontology/s11a-skill-text-drift.md`. **S11 regression**, not a new concern. The `unitares-governance-plugin/commands/governance-start.md` was telling agents to (a) prefer/include `continuity_token`, (b) run `set session --merge --stamp` without `--slot`, (c) persist `continuity_token`/`continuity_token_supported` in cache. Agents following this guidance wrote v1-pattern flat caches the post-PR-19 hook deliberately ignored. Rewrite leads with lineage-candidate framing, declarative-lineage posture (`force_new + parent_agent_id`), slot-scoped write using just-returned `client_session_id`, and v2 cache schema. | None. S20 and S11-a ship independently. | ✅ (a) `commands/governance-start.md` rewritten 2026-04-25 (`ad4dfef`). ✅ (b) Lint test shipped 2026-04-25 as `unitares-governance-plugin@15afc91` (`unitares-governance-plugin/scripts/dev/lint-command-cache-contract.sh`) — two rules: rejects `set session` without `--slot`/`--allow-shared`, rejects `continuity_token` field in cache write payload. (c) Sibling `commands/checkin.md` + `commands/diagnose.md` audit retracted 2026-04-26 — earlier claim was based on commit `25b54b5` which Kenny reverted in `568bdb9` (subagent-driven `identity_assurance`-fabrication claim was wrong; field exists in update responses). Current state of both files retains in-process `continuity_token` use, which is the Part-C anti-hijack proof (load-bearing per S1 plan, not v1 drift). The cross-process-instance use S11-a fixed in `governance-start.md` is the actual v2-deprecation territory. Flat `session.json` reads in checkin/diagnose remain S20.2 reader-audit work. **Follow-up open:** (d) Operator cleanup of pre-existing flat token-bearing caches on disk — bundled into S20.5. **S11-a primary fix shipped + lint shipped**; sibling-audit category was a category mistake on my part (I conflated cross-process-instance and in-process token use). |
| S12 | FEP formalization of EISV (paper-v6 §3.1/§3.2 FEP-grounding claims) | **Blocked by channel geometry, not disproven** (2026-04-23) | v7-fhat spec Sessions 1a/1b | Session 1b (master `fdc2d180`) completed fit with EM convergence + SC1 pass; SC2 tripped (r = 0.9949) per `docs/ontology/v7-fhat-spec.md` §2.6, indicating the minimal generative model under v5 channel geometry (C1–C4 direct measurements + sparse C5; C6 dropped for lack of history) reduces $\hat{F}_t$ to a monotone transform of the one-step-ahead emission residual. Operator selected **R1 — accept path (b) early**: FEP demoted to adjacent/inspirational; $E$ and $V$ both phenomenological in v7 §3 per spec §5.1(b). **Unblock condition (not committed):** at least one asymmetric-information channel matures — C6 event stream ≥ 30 days of continuous history (earliest 2026-05-11), or per-agent calibration state shipped as a first-class audit channel, or `primitive_feedback`/`watcher_finding` added to the observation set. A re-dispatch of the Session 1a/1b protocol against the broader channel set would produce a new SC2 read. No v7 deliverable depends on this; treated as v7.1 / v8 instrumentation work. |
| S13 | Server-side complement of S11 (resolved 2026-04-21 plugin-only) — handler-default flip + onboard-teaching surface sweep | Server PR (default flip + workflow-surface sweep + STEP-1 demotion) | None (coordinate deprecation-breadcrumb language with S1 if S1-a ships first; structural work is independent of S1's A/A′/B/C decision) | **Counterpart in spirit (not mirror) of S5** — both replace silent re-association with explicit-declaration-required posture; S5 inverted event interpretation, S13 retires a resolution path. **Resolved when:** (a) handler defaults at `src/mcp_handlers/identity/handlers.py:781-782` invert to `force_new=True, resume=False`, so arg-less `onboard()` short-circuits at line 1231 before any session-key resume runs; (b) the two genuine onboard-teaching surfaces — `src/tool_descriptions.json:54` and `src/mcp_handlers/schemas/identity.py:55-65` (the `resume` field's "resume-preferred entry point" docstring) — match identity.md v2's posture; `src/mcp_handlers/introspection/tool_introspection.py:462` (UI label only) and `src/mcp_handlers/support/agent_auth.py:358-365` (not-registered error hint) get a lighter alignment sweep, not a workflow rewrite; (c) `unitares/CLAUDE.md` "Minimal Agent Workflow" rewritten to declarative-lineage form (`onboard(force_new=true, parent_agent_id=...)` lead, no `identity(agent_uuid=..., resume=true)` follow-up); (d) `derive_session_key` step 7 (`src/mcp_handlers/identity/session.py:419-438`) — the actual fingerprint/IP:UA pin-lookup site — emits a `concurrent_session_binding_observed` audit event when a pin match is found but does not auto-resume on the arg-less path (preserves the IPUA-pin `agent_id`-as-proof contract test); (e) regression tests: a fresh-instance log assertion (arg-less `onboard()` emits `[FRESH_INSTANCE]`) and a `bind_session` non-coupling test (verify shared resolution chain unaffected since `bind_session` consumes the same session-key plumbing). **Note for future readers:** a session-evidence finding that `session_resolution_source: pinned_onboard_session` returns even on explicit `force_new=true` is **honest labeling, not a bug** — `force_new` is correctly honored at `handlers.py:1231`; the field reflects key-derivation provenance, not identity-resolution outcome. Reordering key derivation to "fix" the label would break the unified key-derivation contract. **Scope boundary:** does NOT retire `continuity_token` server-side (S1) and does NOT change `bind_session` semantics or PATH 0/1/2 anti-hijack (S9). Single-concern sweep. Reviewed by `dialectic-knowledge-architect` + `feature-dev:code-reviewer` council pre-merge. **Merged 2026-04-25** as [unitares#156](https://github.com/CIRWEL/unitares/pull/156). Implementation note: rather than blindly flipping the schema/handler default to `force_new=True, resume=False` (which would silently break proof-signal callers — they don't pass explicit `resume=true`), the gate-pattern from `handle_onboard_v2:1197-1202` was mirrored into `handle_identity_adapter` so arg-less calls flip while proof-signal callers preserve resume semantics. (a) handle_onboard_v2's gate already shipped earlier; this PR adds the equivalent to handle_identity_adapter. (b) tool_descriptions.json was already v2-posture; schemas/identity.py docstrings already cite v2. (c) CLAUDE.md already shows declarative-lineage form. (d) the audit event is added in src/audit_log.py and wired in session.py at the pin-match site (separate from `identity_hijack_suspected`'s active-alert role; this is the passive-observation channel). (e) three regression tests in tests/test_identity_handlers.py: arg-less identity() FRESH_INSTANCE assertion, proof-signal bypass, bind_session non-coupling. |
| S14 | Option C feasibility re-evaluation — substrate-only continuity proof | **Deferred** — opened 2026-04-25 to prevent A→A′ muscle-memory foreclosure of C | S8a Phase 2 + 4 weeks of S1-a deprecation telemetry | The S1 council pass flagged that `ownership_proof_version` makes A→A′ frictionless, which silently forecloses Option C (substrate-only proof, the v2-ontology-pure endpoint per `identity.md:111`). This row is the explicit forcing-function so C does not die by neglect. Resolved when: post-S8a-Phase-2 with substrate vs session-like classes populated, the operator either ratifies A′ as the endpoint or reopens C. Concrete trigger condition: S1-a deprecation telemetry shows ≥4 weeks of cross-process-instance accept activity (well-defined volume, not zero), AND substrate-earned class has accumulated ≥10 agents passing R4 verification — at which point C becomes implementation-tractable. |
| S15 | Server-side skills surface — promote `skills/` from per-client artifact to server-authored MCP tool | **Operator decisions accepted 2026-04-25** (per §11): (a) Option A ratified (server-side `skills` MCP tool); (b) canonical-on-server with `unitares/skills/` as source of truth, acknowledged as one-way decision per §11.8; (c) §6 complementary cure shipped first as highest-leverage governance-literacy fix; (d) S15-a shipped before S15-b content consolidation, with explicit §11.9 caveat that the tool ships pointing at a known-divergent source-of-truth until S15-b lands. **§6 cure shipped 2026-04-25** at master `910c0edb` — `tool_descriptions.json` `identity` description rewritten to v2-ontology framing + explicit ANTI-PATTERN call-out against transport-layer auto-injection of `continuity_token`; matching call-out spliced into `onboard`; 5 regression tests pin v2-ontology language present + pre-Part-C language absent. **S15-a shipped 2026-04-25** as PR #157 (master `5a7dab73`) — server `skills` MCP tool at `src/mcp_handlers/introspection/skills.py` + Pydantic schema + 12 regression tests (including identity-blindness §4.5 contract test) + `__init__.py` / `rate_limit_step.py` / `tool_schemas.py` registration. Plan doc + council review at `docs/ontology/s15-server-side-skills.md`. | None. Scope clear; sequencing operator-decided per §11. | **S15-a:** ✅ shipped 2026-04-25 (PR #157). **§6 cure:** ✅ shipped 2026-04-25 (master `910c0edb`). **S15-b:** ✅ shipped 2026-04-25 (master `66286358`) — canonical content reconciled in `unitares/skills/`, `unitares-dashboard` promoted to canonical, lexicon decisions (Valence vs Void) preserved per memory. Closes the §11.9 known-divergent-source-of-truth caveat on the unitares side. **S15-d:** ✅ shipped 2026-04-25 (master `718725f9`) — `scripts/dev/sync-plugin-skills.sh` (rsync canonical → plugin, refuses to clobber dirty plugin tree) + `scripts/dev/ship.sh` parity gate (when staged files include `skills/`, run `--check`; fail with sync-then-retry instructions on mismatch). Realizes §11.5 'generated mirror + parity CI' as operator-tooling now; full plugin-side GitHub Actions CI deferred (plugin currently has no `.github/workflows/`). Gate is no-op for fleet agents without the plugin checkout (script skips); blocks operators with the plugin checkout if their unitares skill edit isn't paired with a plugin sync. **Live state at S15-d ship:** plugin has uncommitted WIP in skills/ from a separate session; gate will hard-block any future skill edit until that WIP resolves (commit, stash, or fold to unitares). **S15-c:** ✅ shipped 2026-04-27 as plugin [PR #26](https://github.com/CIRWEL/unitares-governance-plugin/pull/26) (fetch-from-server + offline mirror fallback, master `5560e5c`) + plugin [PR #27](https://github.com/CIRWEL/unitares-governance-plugin/pull/27) council-fix follow-up (clock-skew TTL floor, `os.replace` tmp cleanup, recursion depth cap on JSON unwrapping, master `1fcdfa3`). Closes §9 step 3 of `docs/ontology/s15-server-side-skills.md`. **S15-d:** plugin bundle becomes generated mirror via `scripts/dev/sync-plugin-skills.sh` + CI parity gate. **S15-e** (optional, Hermes-side): Hermes skill adapter — closes the originating incident. **S15-f** (optional, no-op this side): claude.ai surfaces skills via existing Cloudflare-tunneled MCP. |
| S16 | Audit-write fire-and-forget loss tolerance | **Resolved 2026-04-26** | None | Closed by option (a): `AuditLogger._write_entry` now documents the accepted tradeoff directly in its docstring (`src/audit_log.py:431-439`). JSONL remains the durable raw audit log, while Postgres audit writes are intentionally scheduled fire-and-forget so handler paths do not block on the derived relational sink. Future work can revisit a TaskGroup or persistent task set only with audit-volume/loss measurements; the current invariant is explicit rather than an unresolved Watcher finding. |
| S17 | Redis-from-handler deadlock risk in IP:UA pin lookup | **Resolved 2026-04-26** | None | Closed by the Option C escape hatch named in the original row: `lookup_onboard_pin` and `set_onboard_pin` wrap their Redis inner operations with `asyncio.wait_for(..., timeout=_PIN_REDIS_TIMEOUT)` at `src/mcp_handlers/identity/session.py:541-544` and `src/mcp_handlers/identity/session.py:598-608`, degrading lookup to `None` and pin-set to `False` on timeout so MCP handler paths do not hang on an anyio/Redis stall. Regression coverage lives in `tests/test_onboard_pin.py::test_lookup_pin_redis_hang_degrades_to_none` and `tests/test_onboard_pin.py::test_set_pin_redis_hang_degrades_to_false`; the analogous sticky-identity cache guard remains covered by `tests/test_sticky_identity.py::TestRedisRecoveryTimeoutGuard`. |
| S19 | Resume-time substrate attestation for R4-claiming agents | **✅ Shipped 2026-04-26** — full M3-v2 chain merged in a single day. PRs (all unitares): #164 PR1 (`substrate_claims` schema + `enroll_resident.py` CLI + `path_safety.py`), #166 PR2 (`peer_attestation.py` macOS backend — kernel-attested PID via `LOCAL_PEERPID`, launchd label, exec path, `start_tvsec`), #169 PR3a (`verification.py` + `VerifiedPairsCache`), #172 PR3b (`SessionSignals.peer_pid`), #175 PR3c (UDS listener + `PeerCredHTTPProtocol`, gated by `UNITARES_UDS_SOCKET`), #176 PR3d (`handler_gate.verify_substrate_at_resume`), #177 PR3e (PATH 0 wired — kernel-attested peer match substitutes for Part-C token), #184 PR4 (PATH 2.8 substrate-anchored HTTP rejection — closes Hermes leak), #181 PR5 (`GovernanceClient` UDS transport), #189 PR7 (E2E regression vs live DB), plus #185 / #191 platform-skip test fixes. Mechanism-selection gate satisfied 2026-04-25; M3-v2 ratified; scope narrowed to 3 residents (Watcher excluded). Council pass: parallel `feature-dev:code-architect` (mechanism review) + `feature-dev:code-reviewer` (adversary review). v2 proposal at `docs/proposals/s19-attestation-mechanism.md` is the landing artifact: M3-v2 = UDS + SO_PEERCRED + launchctl label match + `expected_executable_path` runtime match + `(uuid, pid, start_tvsec)` cache; operator pre-seed enrollment (no TOFU); macOS subprocess backend (no PyObjC); 6-step sequencing in v2 §Sequencing. Implementation correctness is a separate gate that lives in v2 §Sequencing step 7 regression suite. Original framing context retained below for historical reading. **Open 2026-04-25; B-strict preferred** — Hermes-incident-driven row. PATH 2.8 (token-only resume at `src/mcp_handlers/identity/resolution.py:679`) treats possession of a signed anchor token as continuity proof, but anchor files are substrate artifacts while same-UID file readability is only process-instance evidence. This creates apparent earned continuity without an earned process-instance gate, violating Axiom #3 ("build nothing that appears more alive than it is"). Scope is substrate-anchored resident agents (Vigil, Sentinel, Watcher, Chronicler) and the token-only resume path, **not full R4 verification** (condition 2 — sustained behavior — is structurally unavailable at fresh-process onboard, same reason S5 picked `parent_agent_id` over `verify_substrate_earned`). **Preferred remedy (Option B-strict):** server adds a substrate-claim verification path. Resident claims its hardcoded UUID via a new code path; server verifies a **server-verifiable or non-exportable** attestation against a substrate-claim registry. UUID stability for substrate-anchored residents is preserved (S6/S7/dashboard/KG paths still UUID-keyed). **Attestation must NOT be a plist secret under the same UID** — that repeats the token-leak bug at a different layer. Viable strict forms: server-observed launchd / process binding (e.g., kernel-attested PID + boot-id + executable hash that the server independently verifies, not client-declared); Unix-domain socket peer credentials if the transport can move there; non-exportable key (Secure Enclave / TPM-backed); or operator-signed one-time enrollment certificate per substrate slot. Exact mechanism deferred to a follow-up reviewer pass before code. **Emergency degradation only (Option B-relaxed):** drop hardcoded UUIDs for substrate-anchored residents; fresh UUID per restart via `onboard(force_new=true, parent_agent_id=<own prior UUID>)`. **Rejected as final remedy** because the trust-tier cost is real and concretely demonstrable: (a) SDK `_ensure_identity` at `agents/sdk/src/unitares_sdk/agent.py:279` treats stored `agent_uuid` as "resume myself," not "declare as parent" — switching the SDK semantics is a material change, not a config flip; (b) `update_current_signature` at `src/trajectory_identity.py:660-663` stores `trust_tier = compute_trust_tier(metadata)` from the raw per-UUID path before the S6 substrate-router at `src/mcp_handlers/updates/enrichments.py:727` (`resolve_trust_tier`) gets a chance to override (the router only fires when `trajectory_result.get("trust_tier")` is empty). One-shot launchd/hook residents (Vigil 30min cron, Watcher event-driven, Chronicler daily) would lose tier 3 every restart and never re-earn it. May remain available as a degradation mode under explicit opt-in with documented loss of UUID-stable resident trust semantics — not the default. **Fallback remedy (Option A) — only if B-strict attestation proves infeasible:** server-side process-start nonce pre-registration with resume-time nonce match. Must include independently verifiable pre-registration (server-observed, not client-declared) — `capture_process_fingerprint` (`agents/sdk/src/unitares_sdk/utils.py:159-266`) is client-declared and can be copied by an attacker the same way the token can. A keeps the bearer-token model and bolts attestation on top; B-strict makes attestation the primitive. If we're paying server-side feature cost either way, B-strict has cleaner ontology. | None — actionable now; coordinate with S1-a wire-up if A is chosen as fallback, independent of S1 if B-strict ships | **Resolved when:** (a) no resident can regain R4-style continuity solely by presenting a copied anchor token; (b) substrate-anchored residents claim their hardcoded UUID via an attested path that an external process holding only a copied anchor cannot reproduce; (c) attestation mechanism is server-verifiable or non-exportable — explicit non-goal: "another copyable secret in a config file"; (d) UUID-stable resident identity preserved end-to-end through the S6/S7/dashboard/KG paths that currently assume it; (e) regression test asserting that an external process presenting a copied resident anchor (token or any other anchor-file artifact) is rejected even if it forwards every client-declared field; (f) followup reviewer pass on the exact attestation mechanism before code lands. Rationale + invariant check + Codex weigh-in in appendix entry "2026-04-25 — S19 framing" below. |
| S21 | Session resolution bypasses `core.sessions` — explicit `client_session_id` silently mints ghost identity even when canonical DB row exists; 95.1% fleet-wide ghost-fork rate (30d) | **Surfaced 2026-04-27** dogfood incident; council pass diagnosed root cause (PATH 3 ratifies the ghost into Redis on every silent re-mint via `_cache_session(session_key, ghost_uuid)` — overwrites the legitimate binding without an "only-if-absent" guard). Plan doc at `docs/ontology/s21-session-resolution-bypass-incident.md`. **BLOCKS S1-a** — retiring `continuity_token` while `client_session_id` can't survive one process is removing a non-functional layer above an already-broken one. **Residents are clean** (substrate-anchored hardcoded UUIDs do the work the resolver should be doing); blast radius is session-like agents only. Chronic at 77–100% daily since at least 2026-04-01 per `core.identities` time-series; Apr 22–25 dip to 17–44% coincided with PR #192 (PATH 1 sync-path fingerprint cross-check) and PR #187 (S19 substrate HTTP rejection) then rebounded by Apr 26. Splits into **S21-a (stop-the-bleed)** + **S21-b (architectural cleanup)**. | None — actionable now. Sequencing: must ship before S1-a. | **S21-a (single PR, blocks S1-a):** (1) `_cache_session_redis_write` (`persistence.py:157+`) refuses to overwrite an active live binding for the same `session_key` — Redis `SET ... NX` or check-then-skip when existing binding's `agent_uuid` is `status='active'` in `core.identities`; (2) `resolve_session_identity` PATH 2 fall-through — return MISS instead of falling to PATH 3 when `resume=True` and `core.sessions` has no row (callers wanting to mint pass `force_new=True` explicitly per identity.md design principle KG `2026-04-06T02:34:27.323998`); (3) promote `resolution.py:661` `logger.debug` → `logger.warning [PATH2_DB_FAIL]` so silent fall-throughs become legible; (4) regression tests: 14-min idle resume returns same UUID; PATH 3 with same session_key as live binding asserts NX-preserved; PATH 2 forced exception fail-closed asserts no PATH 3 mint. **S21-b (separate PR, follows S21-a):** (5) consolidate the two `resolve_session_identity` calls per request (middleware `identity_step.py:414` + handler `handlers.py:890`); (6) `require_registered_agent` (`agent_auth.py:256`) consults `core.identities` not just in-memory `agent_metadata`; (7) honest-labeling — add `identity_resolution_outcome: "resumed" \| "minted_after_resume_miss" \| "minted_force_new"` field separate from `session_resolution_source` (input-lane vs outcome split); (8) audit emission on rejected explicit `client_session_id` mirroring S13 §(d)'s `concurrent_session_binding_observed`. **Council required pre-merge** for both PRs per memory entry "Council also for load-bearing implementation" — identity resolution is fleet-bricking territory. **Audit pass 2026-04-29:** items 7 + 8 closed (item 7 lives at `resolution.py:33-39 _created_identity_outcome` + handler propagation through `handlers.py`; values shipped are `minted_force_new` / `minted_after_resume_miss` / `minted_fresh` / `resumed` — adds `minted_fresh` for non-force_new mints over the spec's three values; item 8 lives at `_audit_session_resolve_miss` invoked from `resolution.py:679, 795` calling `audit_log.log_session_resolve_miss_observed` — fires on PATH 2 fall-through MISS which is the rejected-explicit-csi case; the spec's `concurrent_session_binding_observed` event also exists at `session.py:693` for the pin-match path). Items 5 + 6 still open: item 5 — both `resolve_session_identity` call sites still present (middleware-414/454 + handlers-480/797/1295/1489/1494/1534 + a third architectural location at `http_api.py:312`); item 6 — what shipped at `agent_auth.py:332-357` is an in-memory-status-gate fail-closed on `meta.status not in {active, paused, waiting_input}`, with a comment acknowledging the in-memory dict can be stale relative to `core.identities` — a mitigation, not the truth-source lookup the row specified. |
| S20 | Client cache scope narrowing — close helper allowance + umask gap on direct-writer (hook layer already shipped via PR #19) | **Open 2026-04-25** — plan doc shipped at `docs/ontology/s20-cache-scope-narrowing.md` (reviewed by `dialectic-knowledge-architect` + `feature-dev:code-reviewer` council; one-pass honesty re-review; **post-ship amendment 2026-04-25** correcting `hooks/session-start` claim — PR #19 `87affc9` already shipped slot-scoped read). Addresses cache *placement* (orthogonal to S2's *semantic* auto-resume retire and to S1's *token-format* cuts). The hook layer is already correct (slot-scoped read via Claude Code's SessionStart `session_id` stdin payload, KG-bug-driven). Remaining surfaces: (i) plugin helper `unitares-governance-plugin/scripts/session_cache.py:_cache_path` *allows* slotless writes that produce flat `session.json` — exploited by the `governance-start` command (S11-a) since the hook ignores flat files but other readers may not; (ii) `unitares/scripts/client/onboard_helper.py:98-101, 234-245` writes the cache *directly* via `path.write_text` (umask-default 0644) and includes `continuity_token` in the payload — bypasses the plugin helper entirely. Honestly labeled **convention-level, advisory** — bypassable by any caller writing JSON to the path; the earned defense is S1-A′ + S19. S20's job is to stop the system from *teaching* the shared-cache pattern, not to prevent a determined caller from re-creating it. **S20.0 answered 2026-04-25:** Claude session ID stable within session, fresh on `/clear` (~80 distinct slot files / 8 days observed) — slot-scope is correct writer key; scan-newest is correct cross-`/clear` lineage reader. | None — all sub-steps independent. | **~~S20.0:~~** Answered. **S20.1a (PR1) — Resolved 2026-04-26**, merged as [unitares-governance-plugin#23](https://github.com/CIRWEL/unitares-governance-plugin/pull/23). Hook write-path fixes: `post-checkin`, `post-edit:221`, `post-edit:192` literal-`default` fallback. Discovered 2026-04-26 pre-implementation audit: helper rejection without these fixes silently bricks the auto-checkin milestone pipeline (errors swallowed via `\|\| true`). Council-reviewed (`dialectic-knowledge-architect` + `feature-dev:code-reviewer`) before push; review surfaced (a) stale-cache-`slot`-field leak vector — fixed by making SLOT strictly stdin-derived, (b) flapping risk if slotless path fired checkin.py without atomic `last_checkin_ts` stamp — fixed by making slotless = full no-op, (c) silent-skip honesty gap — fixed by `[SLOTLESS_SKIP]` stderr breadcrumb so degraded state is legible per axiom #14. New helper `unitares-governance-plugin/scripts/_slot_from_stdin.py` deduplicates slot extraction across `post-checkin` + `post-edit`; full 5-site consolidation (`session_cache.py:_slot_suffix`, `_session_lookup._slot_filename`, `post-identity`, `session-start`) deferred. Tests: 17 new/updated assertions including stale-slot-no-leak, breadcrumb emit, full-noop on slotless. 114 plugin tests pass. **Concurrent-ship collision (2026-04-26):** a parallel session opened a redundant PR #24 (smaller scope — strict subset of #23) — closed without merge once #23 merged. Lesson: check `gh pr list` for in-flight matching scope before opening a code worktree. **S20.1b unblocked.** **S20.1b (PR2) — Resolved 2026-04-27**, merged as [unitares-governance-plugin#25](https://github.com/CIRWEL/unitares-governance-plugin/pull/25). Helper-side `cmd_set` rejection of slotless writes (`--allow-shared` opt-in for substrate-earned single-tenant); `cmd_set` rejects v2 payloads carrying `continuity_token`; new `cmd_list` for slot inventory. Closes §3a item from `docs/ontology/s20-cache-scope-narrowing.md`. **S20.1c unblocked.** **S20.1c (optional):** Warning-only grace period only if S20.1a surfaces an uncovered client path. **S20.2:** `hooks/session-start` audit + scan-newest secondary fallback (additive — slot-scoped read already in place via PR #19). **S20.3 — Resolved 2026-05-01**, merged as [unitares#276](https://github.com/CIRWEL/unitares/pull/276) (server-side `scripts/client/onboard_helper.py`) + [unitares-governance-plugin#29](https://github.com/CIRWEL/unitares-governance-plugin/pull/29) (plugin-side `unitares-governance-plugin/scripts/onboard_helper.py` + canonical `unitares-governance-plugin/scripts/session_cache.py:_write_json` tempfile-leak fix). Decision §3c: **C2** (mirror contract locally) over C1 (route through plugin helper) — C1 would couple unitares server repo to plugin repo for ~30 lines; C2 keeps both helpers self-contained at the cost of two parity points (mode 0600 + token absence, both mechanically testable). Atomic-write idiom (`tempfile.mkstemp` + `os.fchmod(0o600)` + `os.replace`) extended with BaseException unlink so failed writes don't leave `.tmp` turds; same fix applied to canonical `session_cache.py:_write_json` (the canonical had the same gap). `continuity_token` / `continuity_token_supported` no longer persisted to the cache file; fields stay in the in-process return value (transient, same-process use OK). Council-reviewed pre-commit (`feature-dev:code-reviewer` adversarial + `dialectic-knowledge-architect` ontology); BUG-85 (tempfile leak) found and fixed before merge. Honestly labeled per axiom #14 (convention-level, advisory; earned defense is S1-A′ + S19). **S20.4:** Codex equivalents. **S20.5:** Operator-runbook migration note for pre-PR-19 flat caches on disk. **S20.6:** Tests for helper rejection, hook slot-scope-only regression, direct-writer mode. |
| S22 | Harness context provenance — preserve harness/model/transport/tool-surface context on governance writes and KG provenance | Schema / response-shape enhancement | R6, S7 | Resolved when `process_agent_update` can record optional harness/model/transport/tool-surface metadata; KG writes expose that metadata in provenance; identity responses explicitly distinguish UUID, label, harness, and assurance; and Hermes, Claude Code, and Codex CLI each have one comparable recorded task entry. Draft framing at `docs/ontology/harness-substrate-plurality.md`. **Partial promotion 2026-05-02/05** via R6 v2/v2.1 (`docs/ontology/r6-episode-fork-response-shape.md`): two fields (`episode_fork_kind` enum + `identity_lineage_fork` boolean) promoted to `process_agent_update` thin `thread_context` and `onboard()` rich `thread_context`; remaining fields (harness/model/transport/tool-surface metadata, full candidate envelope) still gated on dogfood evidence per `harness-substrate-plurality.md` §"Design risks." S22 closure requires the broader envelope work; R6 v2/v2.1 is the first concrete promotion under it. |

## Status board (snapshot 2026-05-05)

Numerically ordered, with current state. Note: **S18 was never assigned** — no gap to fill.

```
RESOLVED (shipped or accepted)
  Q3       ✅ 2026-04-21  v7 animating-thesis decision accepted
  R3       ✅ 2026-04-21  trust-tier annotation pass landed
  R4       ✅ 2026-04-21  substrate-earned identity pattern v1 landed
  S4       ✅ 2026-04-21  label-as-identifier audit (mostly cosmetic; load-bearing site → S5)
  S5       ✅ 2026-04-23  resident-fork inversion shipped
  S6       ⚠️ 2026-04-23  Option B + Q2 reseed shipped; engaged-ephemeral recal can open after S8a Phase 2 backfill corpus exists
  S8       ✅ 2026-04-21  re-scoped → S8a/S8b (thresholds remain)
  S8a      ✅ 2026-04-23  Phase 1 shipped (PR #121); Phase 2 shipped 2026-05-01 (#252)
  S8c      ✅ 2026-04-25  spawn_reason write-path repair merged (#155)
  S11      ✅ 2026-04-21  plugin#17 banner inversion + cache v2
  S11-a    ✅ 2026-04-25  governance-start.md rewrite + lint
  S13      ✅ 2026-04-25  identity() fresh-instance gate merged (#156)
  S15-a    ✅ 2026-04-25  server-side skills MCP tool (#157)
  S15-b    ✅ 2026-04-25  canonical content reconciled
  S15-c    ✅ 2026-04-27  Claude Code skill adapter (plugin#26 + #27 council-fix)
  S15-d    ✅ 2026-04-25  sync-plugin-skills.sh + ship.sh parity gate
  S16      ✅ 2026-04-26  audit-write tradeoff documented
  S17      ✅ 2026-04-26  Redis timeout guard
  S19      ✅ 2026-04-26  M3-v2 full chain (PR1–PR7) — UDS + peer-cred + launchd label + exec path + start_tvsec cache
  S21-a    ✅ 2026-04-28  stop-the-bleed shipped — Redis NX guard, PATH 2 fall-through MISS, [PATH2_DB_FAIL] warning, regression tests
  S21-b    ✅ 2026-04-30  items 1-4 shipped via #231 + 8 hardening commits; items 7 + 8 audit-closed 2026-04-29; items 5 + 6 shipped via #245 (`e14544c0`). Middleware now threads resolved identity through `arguments` so `handle_onboard_v2` and `_try_resume_by_session_key` consume it instead of re-resolving; `require_registered_agent` consults middleware-threaded `core_agent_row_status` before falling back to `agent_metadata`. Two-pass council review at `docs/ontology/s21b-items-5-6-council-review-2026-04-30.md`; ontology debt + PATH0 pre-auth probe rate-limiting tracked in KG `2026-04-30T07:03:27.812659+00:00` + `2026-04-30T07:03:37.732240+00:00`.
  S1-a     ✅ 2026-04-29  full chain — TTL shrink + opv field + dep block + audit event + clock-skew tolerance wired across onboard/identity/bind_session via shared helper; HTTP REST inherits transitively. Operator pairs ship with secret rotation per §11.7.
  S1-b     ✅ 2026-04-30  onboard-helper + CLI startup-path migration to `force_new + parent_agent_id` shipped in unitares (`02775388`) and plugin; startup paths no longer prefer cached token/session as cross-process identity proof.
  S8a-Ph2  ✅ 2026-05-01  shipped as #252 (`741b088a`): `engaged_ephemeral` ScaleConstant + classify_agent branch; promotion rule `total_updates ≥ 3` via `class_promotion_sweeper_task` (30min cadence, single-CTE `FOR UPDATE SKIP LOCKED`, NULLIF cast guard); Phase-1 stamp-gap fixes via public `stamp_default_class_tags(meta=)` wired into `process_agent_update` auto-create paths; `stamp_untagged_identities` and `scripts/migration/s8a_phase2_backfill.py` for backlog/backfill. 50+ focused tests landed. Decision (d) data-op executed 2026-05-05; see S8b.
  S8b      ✅ 2026-05-05  data-op run complete — 16 untagged active identities stamped; 4 eligible identities promoted via `--promote --include-archived`; follow-up dry runs returned zero candidates.
  R7       ✅ 2026-05-02  in-repo placement decision + Phase 1 pure in-memory OTP spike implemented (`SurfaceRegistry` + `LeaseProcess`; acquire/conflict/release/expiry/concurrency proof)
  R2-Ph1   ✅ 2026-05-05  #357 shipped honest-memory Phase 1: migration 036 + storage helpers, FSM/audit, cross-role pre-check + response fields, sweeper, and process-update trigger

OPEN — actionable
  R1       remaining implementation — operator-facing promotion + public KG 30-day archival sweeps shipped 2026-05-05 (`src/identity/r1_maintenance.py`, `scripts/migration/r1_lineage_maintenance.py`); live promotion sweep evaluated 3 provisional rows, confirmed 1, left 2 blocked as inconclusive, and reported 0 orphan candidates; public KG TTL dry-run found 0 stale score nodes. R1 v3.4 added explicit unsupported-lineage demotion via `apply_orphans` using R2's `demote_lineage` primitive; remaining consumer wiring after real reader exists (#341 deferred R3 baseline consumer)
  R5       memory-deepening tooling — v0.1 KG cite-and-extend spec landed 2026-05-05; shadow scorer + synthetic tests + read-only diagnostic/batch sampler landed 2026-05-06, kept off the R2 hot path. First live sample: 4 provisional pairs (3 insufficient parent memory, 1 absent) + 3 confirmed pairs (3 insufficient parent memory); durable audit table deferred until non-sparse shadow evidence exists.
  R6       harness-substrate plurality dogfood track — v2 process_agent_update response-shape decision shipped 2026-05-02; `build_fork_context` prerequisite fixed by #284; `force_new` thread-position prerequisite implemented 2026-05-05; v2.1 onboard rich `thread_context` field shape shipped 2026-05-05. Remaining: continue H1/H3 controlled dogfood + broader candidate envelope evidence.
  S7       KG provenance migration (R3 audit done; lineage-chain schema decision open)
  S20      PR1 merged (plugin#23); S20.1b merged (plugin#25) 2026-04-27; S20.3 merged (#276 + plugin#29) 2026-05-01; S20.2 merged (plugin#30) 2026-05-01 — second-pass caught a heredoc-pipe stdin bug the council missed; S20.1c/4/5/6 still open
  S22      harness context provenance — depends on R6/S7; record optional harness/model/transport/tool-surface context on process updates and KG provenance

BLOCKED — by upstream work
  Q1, Q2   blocked on R1 / R2
  S1-c     post-grace cross-process-instance reject (blocked on grace-period telemetry, ≥1 release cycle from S1-a)
  S2, S3   blocked on S1-c (auto-resume retire follows reject)
  S9       blocked on R1 (also: bind_session TTL silently coupled to S1-a's 1h TTL — needs regression test asserting TTL on S9 scope)
  S10      blocked on S7
  S12      demoted to phenomenological in v7 §3; unblock requires asymmetric-info channel maturity (~2026-05-11+)

DEFERRED
  R2-Ph2   Trigger = ≥4 weeks Phase 1 telemetry plus design-doc gate (≥50 promoted pairs + ≥10 demoted pairs + ≥1 rejected-cross-role event observed); scope = trust-tier S6, KG provenance S7, R3 baselines, dashboard interpretation
  S14      Option C feasibility re-eval; trigger = ≥4w S1-a telemetry + ≥10 R4-passing agents
```

**Critical path now (2026-05-06 refresh):** S21 is closed end-to-end (S21-a 2026-04-28, S21-b via #231 + #245), S1-a/S1-b are shipped, S8a Phase 2 is shipped (#252), **S8b backfill is complete as of 2026-05-05**, **R2 Phase 1 shipped in #357 on 2026-05-05** (migration 036 + storage helpers, FSM/audit, cross-role pre-check, sweeper, process-update trigger), **R5 v0.1 KG cite-and-extend spec landed 2026-05-05 and its read-only shadow scorer/tests/diagnostic/batch sampler landed 2026-05-06**, with first live sample showing sparse parent KG memory rather than integration evidence, **R6 episode-fork response-shape decision v2 is shipped 2026-05-02**, the `build_fork_context` call-site prerequisite is resolved by PR #284 (`eedf5203`), the `force_new` thread-position prerequisite is implemented as of 2026-05-05, and **R6 v2.1 onboard rich `thread_context` field shape is shipped 2026-05-05**. R1 has shipped through §4.3 public KG emission (#306/#309/#314/#320/#321/#324), with #341 deferring the R3 baseline consumer until a real reader exists, and an operator-facing 2026-05-05 maintenance sweep now covers provisional promotion, unsupported-lineage demotion via explicit `apply_orphans`, and public KG TTL archival; the first live sweep confirmed 1 of 3 provisional rows. Remaining near-term work: (a) collect R2 Phase 1 telemetry before opening Phase 2 consumers, (b) repeat R5 shadow sampling after the KG has more response-linked parent memory and defer durable audit history for now, (c) continue R6/S22 broader harness/model/transport/tool-surface envelope after dogfood evidence, and (d) keep the `gh pr list` preflight discipline from the 2026-04-29 migration-drift collision before any new identity ontology or migration-slot work.

## Suggested sequencing

Three tracks that can progress independently.

### Track A: paper positioning + quick wins
*Goal: lock high-level framing before committing to code reforms.*

- **A1 (Q3):** Re-read paper v6.8.1 §6.7 against the v2 ontology; write 1-page positioning note. Recommend v7 thesis vs. implementation detail.
- **A2 (S4):** Grep-driven audit of label-as-identifier flows. Inventory only — no code changes yet.
- **A3 (S8):** Pull current orphan archival data; check whether existing thresholds still make sense under the new norm. No code yet.

**Exit criteria:** one positioning note + two inventories. Nothing committed to code.

### Track B: research primitives
*Goal: get the inventive-stance items far enough along that the descriptive-stance items can depend on them.*

- **B1 (R4):** ~~Write `patterns/substrate-earned-identity.md`.~~ **Done 2026-04-21** — landed inline as appendix of `docs/ontology/identity.md` ("Pattern — Substrate-Earned Identity"). Formalizes Lumen's pattern with three conditions, test cases, open questions.
- **B2 (R3):** Annotate `src/trajectory_identity.py compute_trust_tier` — what already implements statistical lineage, what assumes UUID-identity. Produces migration notes for S6/S7/S10.
- **B3 (R1):** Design spike for `verify_lineage_claim`. Signature + confidence output + threshold analysis. One-page design doc; no implementation yet.

**Exit criteria:** three documents. Enough signal to decide whether to invest in implementation.

### Track C: performative-machinery retirement
*Goal: once ontology is stable, the performative layer can start coming down — in the right order so external clients are not broken.*

- **C1 (S1):** ~~Plan doc for `continuity_token` retirement.~~ **Done 2026-04-24** — `docs/ontology/s1-continuity-token-retirement.md`. Key findings: (1) token is load-bearing for PATH 0 anti-hijack post-Part-C, not purely performative; (2) TTL-only mechanism does not achieve process-instance binding — Option A is honestly "performative, narrowed" rather than earned continuity; (3) Option B withdrawn — R1 v3 (shipped same day) is plausibility scoring, explicitly not authentication, does not replace the anti-hijack role; (4) forward-compat `ownership_proof_version` field makes A → A′ non-breaking; (5) external-client inventory: zero references in three known-repo greps, but silent-break consumers remain undetectable through grace-period warnings.
- **C2 (S2, S3):** Auto-resume + cross-channel removal. Depends on C1.
- **C3 (S9):** PATH 1/2 re-scoping. Depends on B3 having produced a verification alternative.

**Exit criteria:** retirement plan with external-client migration; no code changes yet.

## Decision points needing Kenny's input

> **Superseded 2026-04-27** — all four points below have been answered downstream and the answers are now embedded in the relevant rows. Retained for historical reading only. Current operator-decision pressure surfaces are listed in the Status board's "OPEN — actionable" section.

1. ~~**Which track to prioritize.**~~ Tracks ran in parallel; B and C merged in practice as research informed implementation.
2. ~~**Appetite for external-client breakage.**~~ Resolved 2026-04-25 — S1 path A → A′ accepted; one-release-cycle warning-only grace.
3. ~~**Paper v7 coupling.**~~ Resolved 2026-04-21 — v7 animating-thesis decision (Q3).
4. ~~**Owner for each track.**~~ Resolved in practice via process-instance dispatch + WIP-PR convention; council-review pattern shipped 2026-04-25.

## Definition of done for this plan

This plan is done when:
- Every row above has either been completed, explicitly deferred (with a reason), or explicitly cancelled (with a reason).
- The `identity.md` ontology document has no remaining open questions that are blocked by unfinished rows above.
- Paper v6.9+ glossary mirrors the ontology (cite-back only; no re-derivation).
- KG entry `2026-04-19T22:24:12.313223` (the metaphysics question) can be updated from `open` to `resolved` with a pointer to this plan's completion state.

## How to change this plan

Rows are cheap. Add one per surfaced item. Remove rows that are explicitly cancelled. Dependency graph should stay consistent with rows.

Do not promote items out of "research" (R) into "implications" (S) without first resolving the R item — otherwise we re-introduce performative stance.

### `WIP-PR:` field (parallel-work avoidance)

When a process-instance starts code work on a row, add a `WIP-PR:` line to that row's cell pointing to the open PR (or branch, pre-push). Example:

```
| S8a | Tag-discipline audit — ... | Resolved 2026-04-23 — findings doc shipped | ... |
      WIP-PR: unitares#118 (opened by agent-59e966aa-7b8)
```

Remove the field when the PR merges or closes. Before opening a new PR for any row, dispatcher (human or subagent) runs the pre-flight check in the handoff template below — this is the cure for the S11-execution dogfood where two agents independently wrote the same PR 7 hours apart.

---

## Appendix: Operational pattern (how to pursue)

This plan is a dispatch queue. Each row is a scoped task, each task is handled by a fresh process-instance with a handoff prompt, each session ships its output, the operator reviews on GitHub.

### The cycle

1. **Pick a row.** Consult the ledger above; prioritize by current leverage (see "Recommended priority" below).
2. **Spawn a fresh session** (Claude Code tab, Codex, Discord dispatch, or resident agent if appropriate). Paste the handoff prompt template, customized per row.
3. **The session works, ships, reports back.** Work lands on master (or an auto-merged PR, per `ship.sh` routing) with KG notes for anything non-obvious.
4. **Operator reviews the shipped artifact on GitHub.** Accept, reject, or flag for revision. Move on.

### What only the operator can do

- Accept / reject recommendations (e.g., the v7 animating-thesis decision was operator's call; downstream calls same).
- Decide which row to pick next.
- Handle external-client communication if/when S1 deprecation lands (unknown external users as of 2026-04-21).
- Decide when "the ontology is done enough" vs. needs another revision pass.
- Close the loop on this plan when it reaches its definition-of-done state.

### Recommended priority (snapshot 2026-04-23 — superseded)

> **Superseded 2026-04-27.** All four items below have shipped. For current priority, see the Status board's "OPEN — actionable" section and the critical-path note.

1. ~~**S6 follow-up: wire `seed_genesis_from_parent` into onboard**~~ — Shipped 2026-04-23 as PR #112 (`a8bf5d71`).
2. ~~**v7 outline draft**~~ — paper v6 stayed in v6.x; v7 corpus-maturity blocker recorded.
3. ~~**Pre-dispatch PR-scan checkpoint**~~ — Resolved 2026-04-23 as `WIP-PR:` convention + handoff-template pre-flight.
4. ~~**S8a follow-up: Phase-1 default-stamp at onboard**~~ — Shipped 2026-04-23 as PR #121.

### Handoff prompt template

```
Task: [row ID from plan.md, e.g., "S11 synthesized PR execution"]

Authoritative reference:
- docs/ontology/identity.md (ontology v2 + substrate-earned pattern appendix)
- docs/ontology/plan.md (this ledger)
- [any row-specific doc, e.g., docs/ontology/s11-consolidation.md]
all on master in the unitares repo.

Pre-flight (parallel-work avoidance):
1. Read plan.md row for this task — check for existing `WIP-PR:` field.
   If present and not stale (<48h), abort and report the existing PR
   rather than open a duplicate.
2. Run `gh pr list --state open --search "<row ID>"` in the target repo.
   If any open PR references this row ID in title/body/branch, abort and
   report the existing PR.
3. Before pushing a branch, stamp `WIP-PR:` into the plan.md row cell in
   the same commit as the first push (or in a preceding docs-only commit).

Scope: [specific bounds, e.g., "three changes per s11-consolidation.md §4;
ship via ship.sh runtime path for PR + auto-merge"].

Output: [shipped PR URL / shipped docs path / plan.md row update with
status and pointer to artifact]. On merge, remove the `WIP-PR:` field
and update the row's "Resolved when" cell.

Optional lineage declaration: parent_agent_id=[prior process-instance
UUID, if continuity of reasoning matters; otherwise omit].

Report back in under 200 words: what was shipped, what was surprising,
what the operator needs to decide.
```

### What this pattern does not license

- **Long-running sessions that accumulate multiple rows.** One session per scoped task, ideally. Accumulation invites session fatigue and the performative-continuity pattern S11 is retiring.
- **Silent lineage.** If the spawning reason is "continue where prior session left off," declare the prior UUID as `parent_agent_id`. Don't use `continuity_token` to resume — that's the retired path.
- **Operator-less work.** Someone has to review what ships. Automation closes the loop from branch to master; it doesn't close the loop from artifact to operator intent.

---

## Appendix: Audit notes

Running log of descriptive-stance findings. Inventories and measurements only — no code changes, no commitments.

### 2026-04-21 — A2: Label-as-identifier inventory (S4)

**Scope:** Grep for code paths that resolve agents by label rather than UUID; classify each by whether load-bearing (identity-conferring) or cosmetic (UX lookup).

**Live call sites** (post-2026-04-17 `resolve_by_name_claim` cleanup):

- `src/db/base.py:224` + `src/db/mixins/agent.py:158` — `find_agent_by_label(label) -> Optional[str]`. DB primitive.
- `src/mcp_handlers/identity/persistence.py:257` — `_find_agent_by_label` handler wrapper. Re-exported from `identity/{handlers,resolution,core}.py`.
- `src/mcp_handlers/observability/handlers.py:59-60` and `src/mcp_handlers/observability/handlers.py:171-172` — `observe_agent` target resolution fallback.
- `src/mcp_handlers/identity/persistence.py:463` — resident-fork detection (`structured_agent_id` collision check).
- `structured_agent_id` usages across 4 files / 8 sites: `identity_payloads.py`, `runtime_queries.py`, `agent_auth.py`, `identity/handlers.py`.

**Classification:**

| Site | Role | Classification |
|---|---|---|
| `find_agent_by_label` (DB + mixin + handler wrapper) | Primitive label → UUID translation | Cosmetic. Callers treat the returned UUID as identity. |
| `observe_agent` target resolution | Accepts label or UUID for "which agent" argument | Cosmetic. UX sugar; internals operate on UUIDs. |
| Resident-fork detection (`persistence.py:463`) | "Two agents with same label ⇒ fork" | **Load-bearing + performative.** Locus of S5 inversion. |
| `structured_agent_id` surfacings (payloads, queries) | Label carried through response layers | Mostly cosmetic; `agent_auth.py` flagged for B-track closer read. |

**Summary:** 4 of 5 active sites are cosmetic. Single load-bearing performative site is resident-fork detection, already scoped as S5. S4 downgraded; effective outstanding action is S5 alone.

### 2026-04-21 — A3: Orphan archival data check (S8)

**Scope:** Inspect `classify_for_archival` thresholds against production distribution; decide whether thresholds fit the new ontology's norm of many short-lived process-instances.

**Heuristic** (`src/agent_lifecycle.py:100-136`): `low_update_hours=3`, `unlabeled_hours=6`, `ephemeral_hours=6`, `ephemeral_max_updates=5`. Initializing agents (0 updates) never auto-archived.

**Production state (2026-04-21):**

| | Active | Archived |
|---|---|---|
| Total | 169 | 2761 |
| Persistent-tagged | 6 | — |
| Ephemeral-tagged | 0 | — |
| Embodied-tagged | 1 | — |
| Unclassified | 163 | 2761 |

Lifetime — unclassified archived avg 761hr (~32d, max 3449hr/~144d); unclassified active avg 273hr (~11d); persistent active avg 408hr (~17d).

Labels (active): 155 "other", 7 null, 5 residents (Lumen/Vigil/Sentinel/Watcher/Steward), 2 `Claude_*`.

Lineage: 81/169 active agents (48%) have `parent_agent_id`. Archived-rate 0.3% (pre-lineage-era agents).

**Observations:**

1. **Tag discipline is poor** — 96% of active agents lack class tags, despite paper §4 framework. The real gap.
2. **Lineage plumbing is actively used** — 48% of fresh agents declaring parent. Healthy.
3. **Archive aggressiveness matches new-ontology expectation** — 94% archival rate aligns with ephemeral-by-construction process-instances.
4. **Substrate-anchored agents underrepresented in calibration pool** — with only 1 `embodied`-tagged agent, Lumen's EISV norms are being averaged out.

**Action:** Thresholds remain. Real gap is tag discipline — see S8a/S8b.

### 2026-04-21 — S11: five-branch consolidation audit

**Scope:** The five WIP branches in `unitares-governance-plugin` named in S11's row (`fix/onboard-force-new-suggestion`, `claude/auto/skill-onboard-helper-honesty`, `feat/auto-onboard-flag`, `feat/flip-auto-onboard-default`, `refactor/delete-legacy-onboard`) — diff each against master, evaluate against v2 ontology, recommend next step.

**Finding:** all five are already in master. Tree-identical to PRs #6/#7/#8 (Part-C trio); `fix/onboard-force-new-suggestion` matches PR #16 content; `claude/auto/skill-onboard-helper-honesty` matches PR #14. Branches are squash-merge leftovers. "None merged" premise is wrong; "problem persists" is correct — the merges shipped Part-C scaffolding but none implemented S11's teeth.

**Per-branch ontology verdicts:**

| Branch | Master counterpart | Ontology verdict |
|---|---|---|
| `fix/onboard-force-new-suggestion` | #16 `44aaf41` | Compatible. Fixes `force_new` pin-resume footgun server-side. Silent on defaults. |
| `claude/auto/skill-onboard-helper-honesty` | #14 `73d4c58` | **Wrong direction.** Makes `continuity_token` flow smoother from cache → server. Pulls performative layer forward. Counter to S1. |
| `feat/auto-onboard-flag` | #6 `dbd45b4` | Partial. Adds `UNITARES_DISABLE_AUTO_ONBOARD` flag with default `0` — performative behavior is still default, ontology-compliant behavior is opt-in. Wrong-sided default. |
| `feat/flip-auto-onboard-default` | #7 `b57780f` | Partial alignment with S11. Agent must make own first MCP call (consistent with "first MCP call is sole identity source"). Does not retire token, does not invert banner, does not stop cache write. |
| `refactor/delete-legacy-onboard` | #8 `f83f2f4` | Aligned. Structural cleanup. Does not address defaults. |

**Current master gap:** `hooks/session-start` (44aaf41, 117–162) still frames the agent's first move as two peer alternatives — `onboard(continuity_token=…)` vs `onboard(force_new=true)` — with `force_new=true` presented as a footgun warning, not as ontology-default posture. `hooks/post-identity` still writes `continuity_token` to `./.unitares/session.json`, keeping the performative credential alive across process-instances.

**Recommendation — close all five branches; synthesize one new PR** (`feat/s11-force-new-lineage-default`) against current master with four changes:

1. **Banner inversion (`hooks/session-start`).** Lead with `onboard(force_new=true, parent_agent_id=<cached UUID>, spawn_reason="new_session")` as THE recommendation. Reframe workspace-cache hint: present cached UUID as *lineage candidate*, not resume credential ("this workspace was last run by `<UUID>` — if you inherit, declare `parent_agent_id=<UUID>`"). Drop the pin-resume warning (PR #16 repaired that footgun). Cite `docs/ontology/identity.md` in the banner.
2. **Workspace cache becomes lineage-only** (in `unitares-governance-plugin`: `hooks/post-identity` + `session_cache.py`). Stop writing `continuity_token`. Write `uuid`, `agent_id`, `updated_at`, `parent_agent_id` only. Version-bump schema; legacy v1 token is ignored on read. Intersects S2 but ship under S11 since S2's prerequisite (S1 external-client grace) applies to server-side emit, not plugin-internal cache.
3. **S1 deprecation breadcrumb** (in `unitares-governance-plugin`: `onboard_helper.py`). One-line comment where `continuity_token` is read: "compatibility surface for external clients; plugin-internal flows should declare lineage." No behavior change — S1 owns the full deprecation window.
4. **Tests.** Banner shape, token-free cache write, v1-cache-read ignores token.

**What this PR does NOT do:** retire `continuity_token` server-side (S1), change `bind_session`, implement R1 verification, touch S6/S7/S10. Single-concern.

**Concrete next step:**

```bash
cd ~/projects/unitares-governance-plugin
git push origin --delete fix/onboard-force-new-suggestion claude/auto/skill-onboard-helper-honesty \
  feat/auto-onboard-flag feat/flip-auto-onboard-default refactor/delete-legacy-onboard
# then in a fresh worktree from master: open feat/s11-force-new-lineage-default per scope above
```

Branch deletion not executed without operator approval — shared remote, reflog-only recovery.

**Dogfood note:** this audit was produced by a process-instance that onboarded via `continuity_token` resume from `.unitares/session.json` — the performative path §4.1 proposes to retire. The SessionStart banner *suggested* the token path; §4.1 would have suggested `force_new + parent_agent_id=da300b4a`. Same author-by-behavior, different author-by-ontology. The plan is self-instantiating evidence of the gap it closes.

### 2026-04-21 — R3: Trust-tier annotation

**Scope:** Read `src/trajectory_identity.py` end-to-end against ontology v2; classify each meaningful piece as statistical-lineage compatible, UUID-identity assuming, or mixed; sketch migration path for shifting aggregation unit from UUID to role.

**Functions / sections inspected:**

- `src/trajectory_identity.py:34-73` — `bhattacharyya_similarity`
- `src/trajectory_identity.py:76-116` — `_det`, `_inv` linear-algebra helpers
- `src/trajectory_identity.py:119-172` — `homeostatic_similarity`, `_viability_margin`
- `src/trajectory_identity.py:175-234` — `_dtw_distance`, `_dtw_similarity`, `_eisv_trajectory_similarity`
- `src/trajectory_identity.py:237-373` — `TrajectorySignature` dataclass + `.similarity()` + `.trajectory_shape_similarity()` + `_cosine_similarity`
- `src/trajectory_identity.py:376-449` — `store_genesis_signature`
- `src/trajectory_identity.py:452-583` — `update_current_signature`
- `src/trajectory_identity.py:586-681` — `compute_trust_tier` (the named target)
- `src/trajectory_identity.py:684-725` — `get_trajectory_status`
- `src/trajectory_identity.py:728-800` — `verify_trajectory_identity` (paper §6.1.2 two-tier)

**Classification:**

| Site | Category | Notes |
|---|---|---|
| `bhattacharyya_similarity`, `_det`, `_inv` | Statistical-lineage compatible | Pure math on two distributions. Subject-agnostic. Survives any aggregation regrouping. |
| `homeostatic_similarity`, `_viability_margin` | Statistical-lineage compatible | Pure math. Compares two `eta` dicts; doesn't care whose. |
| `_dtw_*`, `_eisv_trajectory_similarity` | Statistical-lineage compatible | DTW on two EISV trajectories; subject-agnostic. Already shaped like a "is this trajectory consistent with that fingerprint" primitive — exactly what R1 (`verify_lineage_claim`) needs. |
| `TrajectorySignature` dataclass + `.similarity()` + `.trajectory_shape_similarity()` | Statistical-lineage compatible | Pairwise comparison of two signatures, no UUID dependency. The paper's six-component model lives here; it operates on signature-pairs, not on subject-identity. This is the load-bearing primitive that survives ontology migration intact. |
| `compute_trust_tier` (pure function) | **Mixed** | Takes a single `metadata` dict, reads `trajectory_genesis` + `trajectory_current` + prior `trust_tier` from it, returns tier. The math (compare current to genesis, threshold on observation_count + confidence + lineage similarity) is subject-agnostic *if* genesis and current are both honest signatures of the same subject. The function itself doesn't reach across UUIDs — it's its **input** (`metadata`, scoped to one `agent_id`) that bakes in the UUID assumption. Re-key the input and the function survives. |
| `compute_trust_tier` thresholds (200 obs / 50 obs) | UUID-assumes | The thresholds were calibrated against a world where one UUID = one long-lived subject. Under v2, most process-instances die before accumulating 50 observations, let alone 200. The numbers are honest only for substrate-earned cases (Lumen) or for role-aggregated input. Already flagged in identity.md §"Implications" — "window norms change since most process-instances will never accumulate 200+ observations." |
| `store_genesis_signature` | UUID-assumes | Σ₀ is keyed by `agent_id` and gated by per-UUID immutability rules (immutable at tier ≥ 2; reseed allowed at tier ≤ 1). Under v2, "this agent_id's genesis" conflates substrate-anchored agents (where Σ₀ is honest across restarts) with session-like agents (where Σ₀ should be the *role's* historical fingerprint, not this process-instance's first 10 samples). The reseed-when-lineage-low logic at lines 416-426 is a partial admission that genesis-by-UUID isn't quite right. |
| `update_current_signature` | UUID-assumes | Writes per-UUID `trajectory_current` + computes lineage vs. that UUID's stored Σ₀. Anomaly detection ("trajectory drift") fires when one UUID's behavior diverges from its own past — meaningful for substrate-earned agents, but for session-like agents under one role, a "drift" event may just be a fresh process-instance whose behavior is closer to another sibling under the role than to its own ten-sample genesis. The drift-event broadcast at lines 524-545 will be noisy in the role-aggregated world. |
| `verify_trajectory_identity` (paper §6.1.2 two-tier) | UUID-assumes | This is the canonical "behavioral-continuity-by-UUID-match" case identity.md §"Performative" calls out by name. Submitted signature is verified against `metadata[trajectory_genesis]` and `metadata[trajectory_current]` keyed by `agent_id`. Under v2, the same verification against role-aggregated norms would be more honest; the math (comparing two signatures) is unchanged, only the reference distribution changes. |
| `get_trajectory_status` | UUID-assumes (read-only) | Read-side mirror of `update_current_signature`. Same caveat. Cosmetic to migrate. |

**Mixed / unclear:** `compute_trust_tier` is the only genuinely mixed case — its body is honest math but its input shape encodes the UUID assumption. The function is small and simple; the load-bearing decision is upstream (what gets put in `metadata`).

**Migration path (UUID → role aggregation):**

1. **Math survives unchanged.** All six similarity primitives (Bhattacharyya, DTW, cosine, homeostatic, recovery-tau, valence-L1) are pairwise on signatures. Re-keying the storage layer doesn't touch them.
2. **Storage layer is the migration surface.** `store_genesis_signature` and `update_current_signature` currently write `metadata.trajectory_genesis` and `metadata.trajectory_current` on the agent record (UUID-keyed). Migration: introduce a parallel role-keyed store (`role_trajectory[role].genesis`, `role_trajectory[role].current_distribution`) where "current" is a distribution over recent process-instances under the role rather than one process's snapshot. Per-agent storage stays — substrate-earned agents (Lumen) keep using it; session-like agents read from the role pool.
3. **`compute_trust_tier` gets a second input mode.** Today: `compute_trust_tier(metadata)`. Tomorrow: `compute_trust_tier(metadata, role_baseline=None)` where `role_baseline` (when provided) replaces or augments the per-UUID genesis. Logic stays — thresholds compare current sig to *some* baseline. Identity of the baseline-source is the open call.
4. **Threshold recalibration is independent of the keying change.** 200 obs / 50 obs were chosen for long-lived subjects. Even under role-aggregation, the right thresholds depend on per-role data (how fast does a typical role accumulate 50 honest observations across its process-instances?). Empirical work, blocked on tag-discipline (see S8a) — without class tags, role-aggregated calibration has no clean partition.
5. **Anomaly semantics flip.** Under UUID-keying, "drift" = this subject changed. Under role-keying, "drift" = this subject's behavior left the role's envelope (fresh-process atypicality), which is a different signal entirely. The `trajectory_drift` audit event at line 528 needs its taxonomy revisited; today it implies a single subject changed, tomorrow it might mean "this process-instance is an outlier under its declared role" — those are not the same incident.
6. **Substrate-earned escape hatch.** Per R4 (substrate-earned identity pattern), agents passing the three conditions keep UUID-keyed trust-tier semantics intact. Migration needs a routing decision at the input layer: substrate-earned → per-UUID metadata path (current); session-like → role-baseline path (new). The `embodied` / `persistent` / `ephemeral` class tags are the routing key — tag-discipline gap (S8a) is the precondition for this routing to work in production.

**What breaks:** `verify_trajectory_identity` callers currently get a per-UUID verdict; under role-aggregation they'd get an under-role verdict, semantically distinct. Drift event consumers (audit + dashboard broadcast) would need to re-interpret incident type. Genesis immutability rules at tier ≥ 2 stop making sense for session-like agents — there's no "this UUID's first signature" in the role-aggregated world; the equivalent is "this role's accumulated distribution," which is by construction not immutable.

**What stays the same:** All six similarity primitives. The `TrajectorySignature` dataclass. The hysteresis margins in `compute_trust_tier`. The threshold *shape* (count + confidence + lineage triple-gate), only the numbers and the input source change.

**Action — what S6 actually needs to do:**

1. **Don't rewrite `compute_trust_tier`.** It's small, pure, honest within its scope. The work is at the storage and routing layers.
2. **Add a role-baseline storage path** in `store_genesis_signature` / `update_current_signature` — write role-aggregated EISV distributions in parallel with per-UUID metadata. Schema-add, not schema-change.
3. **Add a routing layer** at handler entry (where these functions are called from MCP) that picks per-UUID vs. role-baseline based on class tag — substrate-earned routes to per-UUID; session-like routes to role-baseline. Default to per-UUID (current behavior) when class tag is absent — preserves backward compatibility during the S8a/S8b tag-discipline rollout.
4. **Recalibrate thresholds per-class** once tag-discipline is in place. Substrate-earned thresholds can stay near 200/50; session-like thresholds drop to whatever number reflects "enough observations under this role to trust the fingerprint" — empirically derived, not chosen.
5. **Re-taxonomize `trajectory_drift`** into two events: `subject_drift` (per-UUID, current semantics, fires for substrate-earned agents only) and `role_outlier` (per-role, fires when a process-instance under a role exhibits behavior far from the role's distribution). Audit consumers and dashboard updated. Inverts cleanly with S5's `resident_fork_detected` flip.
6. **Document `verify_trajectory_identity` as substrate-earned-only** in its docstring until role-aggregated alternative is built. Today it can't honestly verify a session-like agent's identity because there's no honest baseline.

S6 as scoped above is consistent with identity.md §"Implications" — "Re-interpret (not re-derive)" — and consistent with the "math survives within a process lifetime; window norms change" framing. No new math. No deletion. Additive routing + recalibration. Substrate-earned agents get a separate calibration pool (per R4), which the routing layer makes structural rather than ad-hoc.

**Callers of `compute_trust_tier`** (all UUID-aggregated read-side sites):

| Site | File:line | Role |
|---|---|---|
| Update enrichment — tier + risk adjust | `src/mcp_handlers/updates/enrichments.py:729-773` | Recomputes tier post-signature, stamps `meta.trust_tier`, adjusts current update's `risk_score` ±0.05 / ±0.15 by tier and drift flag |
| Batch tier load | `src/agent_metadata_persistence.py:173-192` | Fleet metadata load: batch-fetches identities, populates `meta.trust_tier_num` per record |
| Lifecycle query | `src/mcp_handlers/lifecycle/query.py:413-433` | Backfills missing `trust_tier` when listing agents |
| Identity status endpoint | `src/mcp_handlers/identity/handlers.py:1713-1717` | `get_trajectory_status` response decoration |

**KG provenance — `agent_id` stamping and aggregation** (S7's territory; tightly coupled to R3):

| Site | File:line | Class |
|---|---|---|
| `DiscoveryNode.agent_id` | `src/knowledge_graph.py:81` | UUID-aggregated stamp; load-bearing for attribution |
| `DiscoveryNode.provenance_chain` | `src/knowledge_graph.py:97-99` | Mixed — exists, role-friendly, but underused (open question 5) |
| PG `kg_add_discovery` / `query` / `get_stats(by_agent)` / `get_agent_discoveries` | `src/storage/knowledge_graph_postgres.py:56-208` | UUID-aggregated; `total_agents` stat = "distinct process-instances that authored this epoch", not "total agents" under v2 |
| AGE backend mirror + rate limiter | `src/storage/knowledge_graph_age.py:937-1006` | UUID-aggregated; rate-limit-by-UUID means a churning role gets `N x budget` writes (feature or leak — open question 4) |

**Fleet-level aggregation** (S10's territory):

| Site | File:line | Class |
|---|---|---|
| `handle_aggregate_metrics` | `src/mcp_handlers/observability/handlers.py:671-771` | **Already statistical** — UUID is iteration key, not aggregation key; mean/count over observations is identity-agnostic in spirit |
| `calibrate_class_conditional.py` | `scripts/calibrate_class_conditional.py:90-134` | **Already statistical and role-aware** — groups by `classify_from_db_row(label, tags)`; integral-over-role semantics R3 calls for |
| `classify_agent` | `src/grounding/class_indicator.py` | Role-aware primitive; class tags map to known residents + ephemeral/persistent/embodied/default |
| `get_recent_cross_agent_activity` | `src/db/mixins/state.py:120-148` | UUID-aggregated (`GROUP BY i.agent_id`) |
| `SequentialCalibrationTracker` | `src/sequential_calibration.py:104` and `:204-252` | Mixed — `global_state` (statistical) parallel to `agent_states[agent_id]` (UUID-aggregated); callers choose |

**Audit machinery** (UUID-aggregated at stamp time): `audit_log.log_*(agent_id=...)` across `src/audit_log.py:20-170`; `trajectory_drift` event at `src/trajectory_identity.py:527-543`; `identity_assurance_change` broadcast at `src/trajectory_identity.py:561-574`.

**Open questions for S6/S7/S10 owner:**

1. **Substrate-earned: class-arg or parallel path?** `compute_trust_tier` could take a `calibration_class` argument and branch internally, or substrate-earned could bypass `compute_trust_tier` entirely (tier=3 by R4's three-condition check). Latter is cleaner; requires a parallel "substrate-earned path" alongside the per-UUID metadata path.
2. **Reseed ceiling.** `store_genesis_signature` already has a partial-admission patch (lines 416-426): reseed when lineage drops. A principled extension: "at tier ≤ 1 with declared parent, seed genesis from parent's `trajectory_current`." Module-scope win that may obviate the heavier role-aggregation lift.
3. **`observation_count >= 200` as substrate-earned-only.** Under v2, this threshold is unreachable for session-like agents. If we keep it, tier 3 ("verified") becomes substrate-earned-only by accident — intentional? Maybe yes (consistent with R4); if so, name it.
4. **AGE rate limiter — feature or leak?** Per-UUID budget means churning roles exceed the limit `N` times. Intended elasticity or a hole.
5. **`provenance_chain` is dormant.** Field exists, is serialized, is role-friendly. Few writers populate it. Is this the cheapest place to start role-aware write-stamping, or is there a reason it's underused.

**Unblocking table:**

| Row | Unblocked? | What remains |
|---|---|---|
| **S6** (`compute_trust_tier` re-interpret) | Yes, partially | (a) operator answer to question 1 above, (b) empirical window-size for `default` class (likely from S8 archival data) |
| **S7** (KG provenance audit) | Yes, largely | (a) lineage-chain column schema decision, (b) `total_agents` stat semantics migration |
| **S10** (fleet aggregation paths) | Partially | (a) dashboard + external-consumer contract reshape, (b) coordination with R4 (substrate-earned visible as N=1 classes); still blocked on S7 for KG slice |

**Re-scope observation:** R3's bulk is at the storage layer (KG stamping/query/stats — S7 territory) and audit (G section above). The trust-tier function itself is small. **R3 and S7 are tightly coupled** in a way the dependency graph treats as one-directional. A joint pass may be more coherent than sequential. The pure-trust-tier slice (B-tier item 3 above) can ship module-scope without S7/S10, but consumers will want KG primitives migrated soon after.


### 2026-04-21 — S11 execution: landing + duplicate-PR lesson

**Landed:** `unitares-governance-plugin#17` merged 2026-04-21 23:55 UTC as squash commit `743952ab`. Implements all four S11 spec items per the prior appendix entry (banner inversion citing identity.md; lineage-only cache write with `schema_version: 2`; S1 deprecation comment in `onboard_helper.py`). Author: Codex auto-shipped via plugin's `ship.sh` runtime path.

**Dogfood lesson:** an independent agent (Claude Code subagent dispatched 7+ hours after #17 was opened) wrote the same PR from scratch as #18, file-for-file overlap on the same six files. Both PRs converged on the same banner posture (force_new lead, bare UUID surface for `parent_agent_id` declaration). #18 was closed in favor of #17 (smaller diff, earlier author, equivalent ontological outcome).

**The pattern that was not prevented.** S11's audit was scoped to consolidate the 5 prior parallel WIP branches that triggered this row. The execution session then **spawned a 6th parallel attempt** without first running `gh pr list --state open` to scan for in-flight work. The audit identified the symptom (multiple agents independently reaching for the same problem with no cross-visibility) but did not produce a code-level cure (no pre-dispatch PR-scan checkpoint).

**Followup candidate (not committed):** a small pre-dispatch hook for parallel-work avoidance — when an agent is about to open a PR, scan open PRs in the same repo for matching scope tags and require explicit acknowledgment before proceeding. Lighter alternative: the plan.md row format gains a `WIP-PR:` field that owner-of-row updates when work is in flight, and the dispatcher (Claude/Codex subagent) is instructed to grep for that field before opening a new PR. Either way, the durable fix lives in the dispatcher's pre-flight, not in operator vigilance.

### 2026-04-23 — S5 shipped: resident-fork inversion

**Landed:** `src/mcp_handlers/identity/persistence.py:set_agent_label` + `tests/test_resident_fork_detector.py` on master via this session's ship. Under ontology v2, a resident restart (Watcher, Sentinel, Vigil, Steward, Lumen) is the expected case and should declare `parent_agent_id=<existing_uuid>` at onboard. The event now fires only when that declaration is missing or points elsewhere; lineage-declared restarts log at INFO (`[RESIDENT_LINEAGE]`) and rename silently. Broadcast payload gains `declared_parent` so dashboard/Discord consumers can distinguish unlineaged forks from lineage-mismatch cases.

**Signal choice.** The full R4 `verify_substrate_earned` predicate (three conditions: dedicated substrate, sustained behavior, declared role) was considered but rejected as the inversion signal — condition 2 (observation_count ≥ N=5) is structurally false for a fresh process. The substrate commitment available at onboard time is the lineage declaration itself; R4's full check is for *post-facto* earned-identity verification, not restart admission. Documented in the S5 row.

**What this does not do.** No change to the *existing* agent's tagging or tier. No change to ephemeral-collision path. `resident_fork_detected` event name unchanged (payload additive). Downstream consumers (dashboard, Discord bridge) receive the same event shape plus one new field — no breaking change.

### 2026-04-23 — S6 options: substrate-earned routing

> **Superseded 2026-04-23.** PR #107 picked Option B and shipped the Q2 reseed primitive (`seed_genesis_from_parent`) in the same commit. Onboard-flow wiring for the primitive followed the same day. Appendix retained for historical reading of the A/B tradeoffs; no decision is outstanding.

**Context:** The open question from the 2026-04-21 R3 appendix ("Q1: substrate-earned as class-arg or parallel path?") needs an operator decision before S6 implementation. This appendix lays out both options with tradeoffs; decision unblocks the R3-appendix "Action — what S6 actually needs to do" work.

**The choice.** `compute_trust_tier(metadata)` today takes one metadata dict (per-UUID) and returns a tier. Under v2, substrate-earned agents (Lumen and eventually verified residents) have honest per-UUID semantics; session-like agents need role-aggregated baselines. Two ways to route:

**Option A — class-arg branch inside `compute_trust_tier`.**

- Signature becomes `compute_trust_tier(metadata, *, calibration_class=None, role_baseline=None)`.
- When `calibration_class == "substrate_earned"` or class tag is `embodied`: run current per-UUID logic against `metadata["trajectory_genesis"]`.
- When `calibration_class == "session_like"` or class tag is `ephemeral`/absent: compare `metadata["trajectory_current"]` against `role_baseline` (role-aggregated distribution).
- Thresholds parameterize per class — substrate-earned keeps 200/50, session-like uses empirically-derived numbers (blocked on S8a).

Pros:
- Single entry point. All four callers (`enrichments.py:729`, `agent_metadata_persistence.py:173`, `lifecycle/query.py:413`, `identity/handlers.py:1713`) stay on one function. Routing decision is internal.
- Small blast radius. Existing callers that don't pass `calibration_class` get current behavior (backward-compat default).
- Migration is in-place — no new functions to wire up.

Cons:
- Conflates two different questions inside one function: "has this subject behaved consistently?" (per-UUID) and "does this process-instance fit its role's envelope?" (role-aggregated). The math is similar but the interpretation differs — keeping them inside one function masks the semantic split.
- `metadata` parameter carries two different meanings depending on class. Callers that get it wrong silently pick the wrong path.
- Threshold table balloons — per-class tuples inside the function, hard to audit.

**Option B — parallel path; substrate-earned bypasses `compute_trust_tier`.**

- New function: `tier_from_substrate_earned(verify_result) -> int`. Takes the `verify_substrate_earned()` dict, returns tier=3 when `earned=True`, tier=2 when two-of-three conditions met, tier ≤ 1 otherwise.
- `compute_trust_tier` keeps its current body but gets documented as "session-like / per-UUID path"; its callers pick which path based on class tag.
- Routing layer at handler entry (the four call sites) — cheap dispatch: class tag `embodied` → substrate path; otherwise → existing `compute_trust_tier`.

Pros:
- Semantic split is structural. Two paths, two stories. Readers can't confuse them.
- R4's three-condition check becomes the *source of truth* for substrate-earned tier — no duplication with `trajectory_current` heuristics. Matches the R4 spec direction.
- Session-like recalibration (blocked on S8a) is isolated to one function. Substrate-earned agents get tier=3 immediately once tagged; no empirical threshold work required for them.
- Future retirement of UUID-keyed trust-tier logic (when role-aggregation is fleet-wide) leaves the substrate-earned path intact — it was never UUID-keyed in spirit.

Cons:
- Four call sites need the routing decision. Each caller grows a branch.
- Two functions to maintain — divergence risk if the trust-tier semantics drift.
- `tier_from_substrate_earned` has its own ramp-up: need to decide what 2/3 conditions earns (tier 2? tier 1? refuse?).

**Tradeoff.** Option A is faster to ship and has smaller call-site churn. Option B matches the ontology's semantic split and makes the R4 pattern the authoritative path for substrate-earned agents. The R3 appendix's Q1 framing ("Latter is cleaner; requires a parallel substrate-earned path alongside the per-UUID metadata path") leans B.

**Secondary question (regardless of A vs B).** Reseed ceiling from the R3 appendix Q2 — "at tier ≤ 1 with declared parent, seed genesis from parent's `trajectory_current`" — is a module-scope win that may obviate part of the heavier role-aggregation lift. Worth implementing independently of the A/B call; can ship in the same PR as whichever path wins, or before.

**Recommendation.** B, with Q2's reseed as part of the same ship. B aligns with the R4 spec direction, isolates the empirical-threshold work (S8a-blocked) to the session-like function alone, and matches how `verify_substrate_earned` was designed (predicate-first, not metadata-augmenter). The call-site churn is four branches — small enough. Substrate-earned `tier_from_substrate_earned` starts with `earned → tier 3; otherwise → fall through to compute_trust_tier` (skip the 2/3-conditions question initially; defer until we see real counterexamples).

**Operator decision needed.** A or B (or a third framing). Once picked, fresh session can implement per the R3 appendix's "Action — what S6 actually needs to do" list, scoped to the chosen path.

### 2026-04-25 — Operator-decision sweep: R1, S1, S8a Phase 2

Three rows had been waiting on operator input since 2026-04-23/24. Resolved in one sitting today.

**R1 (`score_trajectory_continuity` v3) — accepted.** Three doc-level open questions resolved:
- Caller-policy list (onboard=marks; promotion=blocks; orphan=blocks) accepted as-is.
- Shadow-mode cutoff bumped from "≥50 pairs OR ≥2 weeks" to **"≥100 pairs OR ≥4 weeks, whichever later"** — Schmidt n=15/CI 36–80% caution generalizes; cheap to wait for tighter signal before threshold work.
- DB helper (`reconstruct_eisv_series`) ships in the R1 implementation row, not split.

Implementation row not yet opened — Track-B handoff template applies. R1 ships in shadow-mode only; load-bearing only when R2 consumes it.

**S1 (`continuity_token` retirement) — Path A → A′ accepted.** Six sub-decisions:
- Path: A as immediate landing, A′ as ontology-clean follow-on.
- TTL: 1h (operator did not push back on the threat-model-vs-cadence caveat in §11.2; flagged for revisit if the deprecation telemetry surfaces a real attacker-window concern).
- Grace period: one release cycle, warning-only.
- `bind_session` TTL: let-propagate. Flag for S9.
- Chronicler re-onboard-on-wake: acceptable (correct v2 behavior for launchd-daily).
- Field rename `continuity_token → ownership_proof`: deferred to S1-d post-grace.
- Ontology label "performative, narrowed": accepted; A is honest narrowing, not earned continuity. A′ is the path if the operator later wants the earned-continuity claim.

Sequencing per §9 stands: S1-a → S1-b → S1-c → S1-d → A′. S1-a is the next ship target — single-concern PR to `unitares` master per §4 + §7 regression-test list.

**S8a Phase 2 — accepted with deliberate hold.**
- `session_like` class addition ratified. Needs `governance_config.py` scale-map entry alongside the `class_indicator.py` add.
- Phase 2 sweep timing: wait until ≥1 week of Phase-1 data (~2026-04-30 earliest) before drafting promotion thresholds. Don't guess numbers; let the `ephemeral` distribution inform.
- `ephemeral → session_like` promotion rule deferred to post-data.
- Backfill of 3180 archived records: same rule, one-shot pass once Phase-2 ratifies. Unblocks S8b.

**Cross-row implications.**
- S1-a is the only one of the three with a code-shippable scope today. R1 implementation and S8a Phase 2 are both deliberately gated on data accumulation (R1 on shadow-mode pairs; S8a on Phase-1 distribution).
- S6 session-like threshold recalibration remains blocked on S8a Phase 2; nothing changes there until ~late April / early May.
- S2/S3 stay blocked on S1-a → S1-c sequencing landing.
- The "operator decisions pending" backlog is now empty for these three rows. Next operator-decision pressure surface: A′ ship-or-defer call (post-S1-c), and S6/S8a Phase-2 once data lands.

**No code changes this session.** Decisions recorded in plan.md; implementation rows open next session.

### 2026-04-25 — Post-acceptance council pass: forcing items + scope correction

After the operator accepted R1 v3.1, S1 path-A→A′, and S8a Phase 2 in one sitting, a five-agent council pass (dialectic + code-reviewer × R1, S1-a; dialectic-only × trajectory) ran in parallel to audit the implementation surface. It surfaced material gaps in all three docs and one new piece of context the docs hadn't captured.

**Forcing items added to the ledger as new rows:**

- **S8c** — `spawn_reason` write-path repair. The trajectory agent surfaced this: R1's shadow-mode plan partitions plausibility distributions by `spawn_reason ∈ {new_session, subagent, compaction}`, but S8a measured `spawn_reason` recorded on **0 of 19** lineage-declared active agents. If R1 starts accumulating without the repair, the 4-week clock runs on uncalibratable data. Hard blocker for R1 implementation row.
- **S14** — Option C feasibility re-evaluation. The S1 dialectic flagged that `ownership_proof_version` makes A→A′ frictionless, which silently forecloses C. Explicit deferred row added so C doesn't die by neglect.
- **S9 amended** — `bind_session`'s TTL-coupling now silently inherits S1-a's 1h TTL. Parked under S9 with explicit note rather than left as "flag for S9" deferral.

**Scope correction — S1-a is mostly already shipped.** Code-review agent found that what the S1 doc §12 lists as "must update in S1-a" was largely already in master:
- `_CONTINUITY_TTL = 3600` at `src/mcp_handlers/identity/session.py:32` (TTL shrink shipped)
- `_OWNERSHIP_PROOF_VERSION` injected at lines 42, 48, 54 (forward-compat field shipped)
- `build_token_deprecation_block` at line 59 (deprecation infrastructure built)
- `log_continuity_token_deprecated_accept` at `src/audit_log.py:368` (audit event shipped)
- Onboard handler wires the deprecation block + audit event at `src/mcp_handlers/identity/handlers.py:1702-1720`

What's actually left for S1-a:
1. Wire deprecation block + audit event into `handle_identity_adapter` (the `identity()` tool path) — currently only in `handle_onboard_adapter`
2. Wire same into `handle_bind_session`
3. Wire same into HTTP onboard direct-tool path (`src/http_api.py`)
4. Add clock-skew tolerance to `resolve_continuity_token` (currently zero drift accepted; §7.2 clock-skew test requires the tolerance to exist first)
5. Three regression tests per §7.2 (token-expiry-mid-call, clock-skew-near-boundary, concurrent-possessor-with-expired-token)
6. Chronicler regression test (>1h-old-token resident force-re-onboards correctly)

The S1 doc has been updated in this commit to reflect this honest scope.

**Operator decisions on S1-a framing (2026-04-25):**

- **TTL = 1h.** Operator picked under hygiene framing. Reasoning: long enough that all resident cadences with rolling-refresh stay covered (Vigil 30min, Steward 5min, Sentinel/Lumen continuous); short enough to not claim "long-lived credential"; not so short (5min) that clock-skew false positives are created without proportional security gain.
- **Hygiene framing + secret rotation at ship.** Operator picked hygiene over security after the dialectic surfaced the doc's §4.1 honest label ("performative, narrowed") and that the project doesn't have a threat model that would justify "1h vs 5min vs 1d." Pair with rotating `UNITARES_CONTINUITY_TOKEN_SECRET` at S1-a ship to invalidate all pre-S1-a 30d-TTL tokens — collapses §7.5 grace-window concern with a one-line operational move rather than a philosophical claim.

**R1 v3.2 amendment (this commit):** four sections added to `r1-verify-lineage-claim.md` covering issues the council found that v3.1 didn't address:
- Telemetry-as-lineage-leak surface — KG discovery of `trajectory_continuity_score` per pair must NOT publish full per-dim `components` to readable KG (lowers adversarial-forgery cost). Mitigation: per-dim values go to audit-only table; public discovery shows verdict + plausibility only.
- `provisional=true` read-side contract — flag lives in identity metadata, S6/S7/R3 each documented as either ignoring provisional records or counting them with a discount factor (default: ignore until promoted).
- `calibration_status: seeded|earned` flag — every score record + dashboard surface gated on this; `earned` only flips after shadow-mode calibration completes.
- KG discovery TTL/cap — bounded analogous to Watcher's `FINDINGS_TTL_DAYS = 14`; extra: dedupe per `(parent_id, successor_id)` pair (update, not append).

Plus one inline correction: the helper SQL needs `AND s.epoch = $current_epoch` to avoid conflating pre-grounding and grounded data on the live database.

**R1 implementation row sequencing.** Now blocked on three things, in order:
1. S8c (`spawn_reason` plumbing repair) merged + ≥1 week of clean partition data
2. S8a Phase 2 (`session_like` class added) so script-driven daily-cron classes are filterable from R1's signal
3. R1 v3.2 amendment council-pass confirmation (we just did the amendment in-context; one more dispatch may be advisable before opening implementation row)

**Critical-path summary post-council:**
```
Now → 1 week:    S13 ship (single-concern, sequencing-required before S1-a)
                 + S8c: spawn_reason write-path repair (1 PR, blocks R1 impl)
                 + S8a Phase 2 hold runs out → ship session_like + scale-map entry
1 week → 2 weeks: S1-a ship (after S13; secret rotation + hygiene PR copy)
                  R1 v3.2 council pass (light)
2 weeks → 4+ weeks: R1 implementation row opens; shadow-mode telemetry begins
                    R2 design can start in parallel (not blocked)
```

The hidden coupling the trajectory agent named: S1-a forces more `force_new` re-onboards which APPEARS to feed R1's pair-count target — but Chronicler-style daily-cron pairs would score high deterministically (same script behavior) rather than because of behavioral lineage. R1 will under-discriminate unless filterable by class tag (`session_like` vs script-driven daily-cron — currently no class for the latter). Captured as known limitation in R1 v3.2.

### 2026-04-25 — S19 framing (Hermes incident → substrate-attestation gap)

**Forcing event.** A third-party CLI agent (Hermes from NousResearch), running as the same OS user on the development Mac, inadvertently inherited Sentinel's identity by reading `~/.unitares/anchors/sentinel.json` and presenting its `continuity_token` to `onboard()`. Server accepted the resume via PATH 2.8. `identity()` reported Mnemos (the Hermes session's onboarded identity); `get_governance_metrics()` reported Sentinel — because the metrics call carried the leaked token / explicit Sentinel `agent_id`, and the cross-agent reference path (`agent_auth.py:202-208`) honored it intentionally per the "never silently substitute identity" invariant. The server is doing the right thing per its current contracts; the gap is at the layer above.

**Why this is forcing evidence for S19, supporting evidence for S14, and not a trigger to promote S14.** S14's trigger is two-pronged: ≥4 weeks of cross-process-instance accept activity AND ≥10 substrate-earned agents passing R4 verification. Lumen + four launchd residents = 5 candidates max; population blocker unchanged. What the incident *does* change is the pressure: Option C's "substrate-only continuity proof" is no longer a far-future ontology-cleanliness item, it's the named cure for an observed exfiltration class. Reopening S14 to absorb Hermes-as-forcing-evidence in its rationale is appropriate; jumping the trigger to ship C now would disrespect §11's deliberate sequencing and the "let class data inform thresholds" pattern S8a is currently teaching.

**S19 addresses the narrow operational gap:** substrate anchors were defined as disk artifacts (per `identity.md` "Layered taxonomy of continuity" — substrate is *"persistent hardware, disk, DB, configuration"*) but never given a resume-time verification rule. The R4 appendix specifies *what* must be true for substrate-earned identity to be claimable, but the resume mechanism (anchor file read → bearer token presentation) enforces none of those conditions at the call site. The category error: a process-instance-layer capability (filesystem read at mode 600) is being treated as substrate-layer evidence. S19 closes that specific gap; S14 remains the broader endpoint.

**Council convergence (2026-04-25).** Parallel review by `dialectic-knowledge-architect` + `feature-dev:code-reviewer` confirmed:
- Code review: PATH 0 (`identity(agent_uuid=X, continuity_token=Y)`) has the Part-C `aid`-match gate via `_partc_owned`; PATH 2.8 (token-only resume via `_token_agent_uuid` extraction at `handlers.py:~1268`) has no equivalent gate — possession of the signed token IS the proof. `capture_process_fingerprint()` exists in `agents/sdk/src/unitares_sdk/utils.py:159-266` but is never invoked from `_ensure_identity()` or any client call site, and never read server-side. The `identity.md` "declaration-only" claim is true by code, not just by intent.
- Code review further found: A′ as currently scoped (PID + start-time in token claims) is also declaration-only unless the server can independently verify those values. A well-behaved client sends accurate values; a leaky/malicious one sends what it wants. Without server-side pre-registration (e.g., a nonce minted at process-start, stored Redis-side, matched at resume), Hermes-style leaks remain possible — the leaking process just copies the PID claim too. This sharpens S19 Option A: "narrow A′" must include server-side pre-registration to be earned rather than performative.
- Steward's anchor (`steward.json`, 1 field, no token) is the empirically observable endpoint of S19 Option B. Steward is in-process within governance-mcp so it bootstraps differently, but functionally it is already operating in the "no cross-process-instance bearer-token resume" posture that B prescribes for the full substrate-anchored class.
- Dialectic confirmed Axiom #3 violation is precise: the anchor-resume mechanism *appears* to grant earned cross-process continuity but the earning reduces to *"could open a 600-mode file."* This is the canonical failure mode `identity.md` "Earned vs. performative today" already names ("Cross-process-instance identity via continuity_token / auto-resume — violates #3 and #5") — anchor files are a special case; the doc named the violation, the implementation didn't close it for substrate-earned agents.

**Operator decision (2026-04-25):** Open S19 as a new row, not an amendment to S1. Option B preferred. Option A retained as fallback only if code inspection shows resident re-onboard breaks identity/audit invariants. Pre-code check: verify `force_new=true + hardcoded UUID + parent_agent_id=<self/prior UUID>` preserves trust-tier accumulation, S5 resident-fork inversion semantics (handles self-lineage), audit log integrity, and Steward-style restart pattern.

**What S19 explicitly does not do:**
- Promote S14 (the broader fleet-wide Option C feasibility re-evaluation).
- Change PATH 0 (`identity` with both `agent_uuid` and `continuity_token`) — that path is gated by Part-C and is not where the leak lives.
- Touch the cross-agent reference contract in `agent_auth.py:202-208` — that contract is correctly honoring the "never silently substitute identity" invariant; the fix belongs at the substrate-attestation layer, not the substitution layer.
- Affect non-substrate-anchored agents — session-like agents continue under the S1-a/A′ trajectory unchanged.

**Pre-code invariant check (2026-04-25) — surfaces that B as originally stated lacks a code path.**

Operator's pre-code question was whether `force_new=true + hardcoded UUID + parent_agent_id=<self/prior UUID>` preserves trust-tier accumulation, S5 inversion semantics, and audit invariants. Code inspection finds:

1. **`force_new=true` always mints fresh UUID; supplied `agent_uuid` argument is not honored on this path.** `handlers.py:1227` reads force_new; lines 1453-1469 force the path through `resolve_session_identity(force_new=force_new)` which generates a new UUID. There is no current code path where a substrate-anchored resident can claim a hardcoded UUID at onboard without presenting a continuity_token (PATH 0) or going through the existing token-resume (PATH 2.8 — the leaky one). The "hardcoded UUID + force_new" formulation contradicts itself in the current code.

2. **Steward is not empirical evidence that B transfers to separate-process residents.** Steward's anchor is `{agent_uuid}` only because Steward is in-process within governance-mcp (per `src/identity/trust_tier_routing.py:6` listing it as substrate-earned alongside Lumen/Watcher/Sentinel/Vigil; no `agents/steward/` module exists — its UUID is server-side state, never traversed the MCP transport). Steward's no-token pattern is a deployment artifact of in-process bootstrapping, not a working separate-process pattern.

3. **S5 inversion (`persistence.py:434-511`) does support the "lineage-declared restart" case.** When a fresh agent's `parent_agent_id` matches an existing label-holder's UUID, the path at lines 487-505 silently renames + logs INFO with `[RESIDENT_LINEAGE]` instead of firing `resident_fork_detected`. Self-lineage is supported semantically. **But this requires the new resident to accept a fresh UUID** — it doesn't unlock hardcoded-UUID claims.

**The B/A choice therefore unfolds into three real options, not two:**

- **B-strict.** Server adds a substrate-claim verification path (e.g., launchd label match via `process_fingerprint`, plist-derived pre-shared secret per resident, or operator-signed enrollment certificate). Resident calls a new path `claim_substrate_uuid(uuid=<HARDCODED>, attestation=<X>)`; server verifies attestation against a substrate-claim registry. UUID stability preserved. **Server-side feature work; equal cost to A done right.**
- **B-relaxed.** Drop hardcoded UUIDs for substrate-anchored residents (Vigil/Sentinel/Watcher/Chronicler). Each restart mints fresh UUID via `onboard(force_new=true, parent_agent_id=<own prior UUID>)`. Identity continuity is fully via lineage chain. **Smallest server-side change; downstream cost:** (a) trust-tier resets per restart — S6's `seed_genesis_from_parent` (Q2 reseed) already shipped and partially mitigates, but tier 3 (≥200 observations) must re-earn each restart; (b) KG provenance keys change per restart — R3/S7 migration to role-keyed or lineage-chain aggregation is still in flight (S6 session-like recalibration is blocked on S8a Phase 2, ~2026-04-30 earliest); (c) dashboard "agent" continuity becomes role-keyed not UUID-keyed. Lumen explicitly excluded — its hardcoded UUID is endorsed by `identity.md` "Pattern — Substrate-Earned Identity §Declarative form" and its embodied substrate is qualitatively distinct.
- **A (with server-side nonce pre-registration).** Keep UUID-stable resume via PATH 2.8, but bind the token to a server-minted nonce stamped at process-start (Redis-side or DB-side), required at resume. Without pre-registration, A is also declaration-only (`utils.py:159-266` `capture_process_fingerprint` is client-declared; nothing prevents Hermes from sending the matching PID). **Server-side feature work; equal cost to B-strict.**

**Resequenced recommendation:**

- **B-relaxed is the smallest server-side change** but inherits the S6/R3 dependency for honest trust-tier under role aggregation. If operator accepts a tier-reset window per restart (or if Vigil/Sentinel/Watcher don't currently rely on tier 3), B-relaxed ships with minimal new code: drop bearer-token resume from SDK `_save_session()`, residents call `onboard(force_new=true, parent_agent_id=<read from anchor>)` on each start, anchor stores only `{agent_uuid, parent_agent_id_to_declare_next_time}`. Audit/S5 invariants preserved.
- **B-strict and A are roughly equal cost.** Both require server-side per-process attestation infrastructure. The architectural difference: B-strict makes substrate-claim attestation a first-class concept (matches R4's spirit); A keeps the bearer-token model and adds binding (matches S1-A′ trajectory). B-strict is closer to S14's Option C; A is closer to S1's stated direction.
- **Steward's pattern can be made the empirical baseline if B-relaxed wires through `agents/sdk` to all four launchd residents.** Currently Steward is sui generis (in-process); under B-relaxed it becomes the documented model for the substrate-anchored class.

**Operator decision pending:** acceptance of trust-tier reset cost per restart (B-relaxed) vs. willingness to invest in server-side attestation infrastructure (B-strict or A). The original "B is cleaner" lean depends on which B is meant. The check has reframed the choice; row text above stands but the implementation row will need this disambiguation before any code.

**Codex weigh-in + operator decision (2026-04-25): B-strict preferred. B-relaxed rejected as final remedy.**

Codex's argument was that the trust-tier cost of B-relaxed is not theoretical — verified by code inspection of all four citations:

- `agents/sdk/src/unitares_sdk/agent.py:279-301` — `_ensure_identity` reads stored `agent_uuid` and resumes via `client.identity(agent_uuid=..., resume=True)`. The SDK semantics treat saved UUID as "resume myself," not "declare as parent." Switching to B-relaxed isn't a config flip; it's a material change to the SDK's identity contract.
- `src/trajectory_identity.py:660-663` — `update_current_signature` calls `compute_trust_tier(metadata)` directly (raw per-UUID path) and stores the result before any substrate-routing happens.
- `src/mcp_handlers/updates/enrichments.py:727-742` — the S6 substrate router (`resolve_trust_tier`) only fires when `trajectory_result.get("trust_tier")` is empty (`if not trust_tier:` at line 733). On the standard check-in path, the trajectory pipeline has already populated tier from `compute_trust_tier`, so the substrate-router is bypassed for the most-trafficked path.
- `src/mcp_handlers/identity/resolution.py:679-694` — PATH 2.8: token-derived agent_uuid + `_agent_exists_in_postgres` + `status == "active"` is sufficient to rebind the session and return. No process-instance gate. This is the actual leak.

**Implication:** under B-relaxed, one-shot launchd/hook residents (Vigil 30min, Watcher event-driven, Chronicler daily) lose tier 3 every restart and never re-earn it before the next restart. Tier 3 requires ≥200 observations (`compute_trust_tier` thresholds, identity.md "Implications" §"window norms change"). This is not a tunable parameter — it's the documented threshold for "verified" identity. B-relaxed turns substrate-anchored residents into lineage chains, not stable resident identities.

**Operator decision text (Codex):** *"S19 implementation choice: prefer B-strict. B-relaxed is rejected as the final remedy until role/lineage aggregation and role-level trust tier are shipped; it may remain an emergency degradation mode with explicit loss of UUID-stable resident trust semantics. A remains fallback only if B-strict attestation proves infeasible, and must include server-side, independently verifiable pre-registration."*

**Caveat on B-strict's attestation mechanism (Codex):** *"B-strict must mean server-verifiable or non-exportable attestation, not 'another copyable secret in a plist.' A plist secret under the same UID repeats the token bug. Viable strict forms are things like server-observed launchd/process binding, Unix-domain peer credentials if transport can move there, or a non-exportable key/enrollment mechanism."*

**Implementation row not yet opened.** A reviewer pass on the exact B-strict attestation mechanism is appropriate before any code. Candidate dispatch: `feature-dev:code-architect` to inventory which attestation forms are tractable given the existing transport (HTTP MCP at port 8767, stdio for in-process, planned WebSocket) and the current launchd plist landscape; `feature-dev:code-reviewer` to stress-test the candidate against same-UID adversary, copied-anchor adversary, and process-impersonation adversary models.
