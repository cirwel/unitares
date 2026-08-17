# Dormant Capability Registry

**Purpose.** This registry distinguishes *built-but-unwired capability* from *genuine
cruft*, so cleanup is a deliberate act, not a guess. UNITARES has a recurring failure
mode: a capability gets built, never wired to a consumer, comes to *look* dead by a
usage audit, gets deleted — and later gets rebuilt. Almost every item below would fail a
naive "is it called? no → delete" sweep, yet most are on the vision path. This document
is the record that keeps them from being deleted by accident.

**Source.** Three-lens inventory, 2026-06-16 (KG/search · identity/lineage/trajectory ·
dialectic/synthesis/agents/MCP), each cross-checked against the live `governance` DB,
the running MCP process, and the BEAM listeners. ~25 dormant items found; only ~6 are
genuine cruft.

## How to use this registry

1. **Before deleting any "dead" code, check here.** If it's listed `KEEP-DORMANT` or
   `WIRE`, it is parked deliberately — do not cut it without an explicit decision that
   updates this file.
2. **Verify "it's dead" against live runtime, not grep.** This inventory's adversarial
   pass caught **5+ false "it's dead" claims** (see *Verified-wired / false-dead* below).
   A function with zero static callers may still be (a) invoked via MCP/dynamic dispatch,
   (b) a delegate behind a consolidated tool, (c) data-starved but correctly wired. Check
   row counts / edge counts / logs before concluding absence.
3. **A caller count opens an investigation; it does not close one.** A zero count is a
   perfectly good *lead* — it is how anything gets onto this list at all, and nobody is
   asking for it to stop being collected. What it cannot do is carry a verdict, and `CUT` is
   the verdict that matters, because it is the only irreversible one.

   Three rows in this file said `0 callers` about code with live call sites, and two of them
   said *Cut* (2026-08-13, all three corrected below). The shapes that defeated the scans,
   none exotic:

   | Miss | Example |
   |---|---|
   | Method called on an instance | `audit_logger.log_auto_attest(...)` |
   | Call in the same file as the definition | `check_reviewer_stuck` |
   | Group claim covering a member that is used | `calibration_health_check_async` |
   | Reached only through a package `__init__` | `identity/core.py`, `process_binding_handler.py` |
   | Consumer is a test, by design | `trust_contract_lint.py` |

   So keep the count and record what produced it. "No static reference outside its own module
   (does not resolve instance-method or string-dispatch calls)" says the same thing as
   "0 callers" while stating its own reach, so a reader can see what it missed and rebut it.
   A bare number invites the reader to treat a search result as a fact about the world.

   The count is enough to move a row to `DECIDE` or `KEEP-DORMANT` — both reversible, both
   just parking. Only a checked call site is enough for `CUT`.
   `docs/dev/TOOL_EDGE_INDEX.md` settles the delegate-behind-a-tool case directly, since it
   is generated from the live registries rather than from source.
4. **New capability should not merge unwired.** Ship it with one wired consumer, *or* add
   a `KEEP-DORMANT` entry here naming what would wire it. "Wire-on-build."
5. **Status legend:** `WIRE` = should be connected, has clear payoff · `KEEP-DORMANT` =
   deliberately parked (roadmap frontier or external-dependency gated) · `DECIDE` = needs
   an operator wire/cut call · `CUT` = genuine cruft, safe to remove *after* its call sites
   are checked (rule 3).

---

## Theme 1 — The graph is written but never reasoned over

The write side lays AUTHORED / RELATED_TO / TAGGED / SPAWNED edges (all live), but the
higher-order graph *reasoning* over them is absent or hand-rolled in SQL/Python. This is
the highest-value cluster and the core of the AGE-canonical direction. The recurring
false-archival bug lives here.

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| SPAWNED lineage DAG — written, never traversed | `src/db/age_queries.py:222` (creator); `mcp_handlers/identity/handlers.py:2642` | 296 SPAWNED edges (vs **473** relational `parent_agent_id` — the fire-and-forget, non-fatal bg writer has drifted ~37%) + 599 Agent vertices; **zero** `MATCH …SPAWNED…` traversal anywhere; no code reads it for any decision | **KEEP-DORMANT** (was WIRE; re-statused 2026-06-17 after live grounding) — the archival liveness gate this was meant to feed was instead wired to the **lease plane** relationally (#796/#797, `agent:/<uuid>` presence), so the WIRE payoff is already captured elsewhere. The `live_descendant_reachable` traversal it needs is **blocked by AGE 1.7**, which cannot causal-filter variable-length paths (KG dual-store verdict). Lineage stays relational-canonical; keep this edge dormant for the day AGE gains variable-length causal reachability — do not cut, do not invest until then. |
| Lineage liveness reasoning hand-rolled, single-hop | `mcp_handlers/lifecycle/stuck.py:173/215/578` | Reads `parent_agent_id` single-hop; multi-hop SPAWNED reachability deliberately NOT adopted (see above) | **RESOLVED via lease plane** (was "WIRE to the above") — the false-archival bug (#720/#721/#725/#726/#779) was closed by sourcing liveness from the `agent:/<uuid>` presence lease (#796/#797), not by AGE multi-hop. This relational path is the live, intended design — not dormant, not a WIRE item. |
| `supersedes=` store param never creates the SUPERSEDES edge | `mcp_handlers/knowledge/handlers.py:879-884` | Store path sets the SQL `superseded_by` field but never calls `supersede_discovery()`; **0 SUPERSEDES edges** | **WIRE** — one line; auto-activates the inert ranking penalty below |
| SUPERSEDES connectivity ranking penalty | `storage/knowledge_graph_age.py:1720,1740` | Coded into every search blend but always 0 (no edges exist) | auto-fixes once the edge is wired |
| RESPONDS_TO edges + `get_response_chain` | `storage/knowledge_graph_age.py:538,558`; handler `:2107` | Read path wired behind `include_response_chain`; **0 callers set it; 0 edges ever written** | **DECIDE** — have the respond/dialectic flow set `response_to`, or cut the chain |
| Search "graph expansion" reads a SQL field, not the graph | `retrieval.py:100`; handler `:1197` | Gated off (`UNITARES_ENABLE_GRAPH_EXPANSION` unset); even when on, reads `related_to` SQL field, not the 2186 RELATED_TO Cypher edges | **DECIDE** — repoint to a Cypher neighbor fetch, or stop calling it graph |
| Cross-agent knowledge-flow query (collaboration DAG) | `src/db/age_queries.py:330` | Pure graph-native Cypher; **0 consumers**; data exists (1765 AUTHORED + 2186 RELATED_TO) | **WIRE** — the showcase "graph-native" query; runs today on live data |
| Orphaned AGE analytics queries (entropy↔work, unresolved-questions-with-entropy, etc.) | `src/db/age_queries.py:309/352/359/379/406` | Zero callers outside the module (only `query_tags_with_discoveries` is wired) | **DECIDE** per query — dashboard-panel-shaped vs superseded |
| `link_discoveries` manual typed-edge API | `storage/knowledge_graph_age.py:2034` | no static reference found, no MCP tool exposes it | **KEEP-DORMANT** — useful if a manual-curation surface lands |

## Theme 2 — The synthesis loop is built end-to-end but nothing fires it

The detector and the actuator both exist; the wire between them does not. This is the
"accumulation / graveyard" problem (the KG re-discovers the same findings and never
consolidates them).

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| `knowledge action=synthesize` (topic rollups) | `mcp_handlers/knowledge/synthesis.py`; routed `consolidated.py:99` | **0 calls in 324MB tool_usage.jsonl; 0 `rollup::` rows ever**; only a manual action, nothing fires it | **WIRE** — scheduled daily `synthesize` from a resident/background task |
| `consolidation_hint` ("found 3×, all open") | producer `mcp_handlers/knowledge/handlers.py:934` | Computed, returned in the store payload, **discarded — 0 consumers** | **WIRE** to `synthesize` — cheapest wire, biggest payoff |
| Concept extraction → Concept / ABOUT / RELATES_TO graph layer | `concept_extraction.py`; bg task `background_tasks.py:163,1802` (daily) | Task fires but **0 Concept vertices**; errors swallowed at `logger.debug`; no handler reads Concepts | **WIRE+verify** (capture the swallowed error) **or DECIDE to disable** — currently burns a daily cycle for nothing |

## Theme 3 — Feeders are starved (wired, but the input never arrives)

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| `auto_ground_truth` objective-outcome grader | `auto_ground_truth.py:330` | Task fires (6h) but gated on `has_exogenous_signals()`; **0/200 recent rows carry exogenous signal** — updates ≈0/cycle | **WIRE** — attach outcome_events / tool-results to the `auto_attest` payload (those signals already flow elsewhere) |
| R1 over-claim detector / `demote_lineage` | `identity/lineage_lifecycle.py:408-445` | **0 demote events ever** (vs 23 promotes); R1 only ever returns `plausible` or `inconclusive@p=0.0`, never `unsupported` — EISV trajectories too sparse | **KEEP-DORMANT + fix feeder** — the logic is correct, it's the lineage-integrity backstop; don't cut |
| R1 calibration lifecycle (`seeded`→`earned`) | `identity/trajectory_continuity.py:189`; `core.r1_calibration_state` | 1 row, 44d stale; all 5,112 R1 scores stamped `seeded`, 0 `earned` — scores produced but never made authoritative | **DECIDE** — wire the promotion, or declare R1 advisory-only |

## Theme 4 — Deliberate dormancy (roadmap frontier / external-dependency gated)

These are intentionally parked. **They are the most at risk of being deleted as "dead"
and the registry's primary protectees.**

| Capability | Location | Why parked | Status |
|---|---|---|---|
| Agent Orchestrator on BEAM (ephemeral spawn + lineage provisioning) | `elixir/agent_orchestrator/lib/.../agent_runner.ex` | Complete + provisioning shipped (#581/#590/#648); nothing spawns through it yet — Wave-3b frontier | **KEEP-DORMANT** — tag so it survives cleanup |
| Resident-validation authority/canary framework | `resident_validation.py`, `_runner.py`, `_invocation.py` | Complete + tested; awaits a supervisor tick. Encodes governed-resident capability boundaries (forbidden deploy/merge/force-push) | **KEEP-DORMANT** — the authority model; do not cut |
| `verify_trajectory_identity` + `trajectory_step` middleware | `trajectory_identity.py:885`; `mcp_handlers/middleware/trajectory_step.py` | Wiring correct, gated on a caller `trajectory_signature`; 0/30k `agent_state` rows carry one — awaits anima/embodied submission | **KEEP-DORMANT** — embodied-only |
| `trajectory_shape_similarity` / `_eisv_trajectory_similarity` (DTW discrimination primitive) | `trajectory_identity.py:363/215` | no static reference found; central to the trajectory-identity paper; nothing emits the per-dim arrays it needs | **DECIDE** — wire `compute_behavioral_trajectory` to emit + call, or cut. The measurement-only [legacy coherence identity ablation](../proposals/legacy-coherence-identity-ablation-v0.md) is the current evidence-gathering step. |
| Lineage credit-assignment aggregation | `identity/provenance_chain.py:83/170` | Read/scoring half orphaned; write half produces empty chains (0/1056) | **DECIDE** — depends on whether discovery→lineage attribution is still a goal |
| S22 H5 cross-harness coverage assessor | `identity/s22_h5_comparison.py:110/190/277` | Diagnostic-script-only; input data live (30k provenance rows) but no MCP surface reads the gate | **DECIDE** — surface via `get_governance_metrics`, or keep as a script |
| `backfill_calibration_from_historical_sessions` | `mcp_handlers/dialectic/calibration.py:193` | One-shot admin migration util; no scheduled caller (by design) | **KEEP-DORMANT** — document as manual-only |
| Cross-device / orchestration audit API (`AuditLogger.log_orchestration_request` / `log_orchestration_complete` / `log_cross_device_call` / `log_device_health_check` / `log_eisv_sync`) | `src/audit_log.py:229/265/294/331/361` | 0 in-repo callers — the consumer (Mac→Pi orchestration) was extracted to the external `unitares-pi-plugin` package in the Phase B1 Lumen decoupling (see `src/mcp_handlers/__init__.py:133`, `src/services/runtime_queries.py:849`). This repo owns the writer surface; the caller lives cross-repo. Same external-API shape as `register_extra_schemas` (Theme 5) | **KEEP-DORMANT** — cross-repo audit API; removal is a deprecation decision coordinated with `unitares-pi-plugin` |

## Theme 5 — Mechanical singletons (vulture cross-pass, 2026-06-16)

> **Known blind spots in this pass (found 2026-08-13).** Its "zero static references" claim
> missed at least two call shapes: a **method called on an instance**
> (`audit_logger.log_auto_attest(...)`) and a **call in the same file as the definition**
> (`check_reviewer_stuck`). One row here was also wrong for a single member of a grouped
> claim (`calibration_health_check_async`). Treat a zero-reference reading in this theme as a
> lead, not a finding, and confirm the call sites before acting on it.

A second, mechanical pass (vulture → cross-referenced every flagged symbol against the
whole repo, py + json + plist + elixir + js + md, to catch string-based dispatch) found
these smaller built-but-unwired functions the three-lens capability inventory above did
not enumerate. Each has **zero static references repo-wide** after excluding this codebase's
dynamic-dispatch patterns — the 73 reflectively-loaded `*Params` schemas, the 9
`@enrichment(order=N)`-registered `enrich_*` functions, the 4 pydantic
`@model_validator`/`@field_validator` methods, and the `importlib`-over-list-literal schema
loader. Those exclusions are *why a naive vulture sweep is unsafe here* and are recorded so
they are not re-flagged.

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| `create_indexes` AGE index-DDL builder | `src/db/age_queries.py:567` | no static reference found; AGE indexing is done by inline `CREATE INDEX` DDL in `storage/knowledge_graph_age.py` | **CUT** — dead duplicate, sibling of `query_response_chain` |
| `create_temporally_near_edge` | `src/db/age_queries.py:537` | TEMPORALLY_NEAR edge writer, no static reference found | **DECIDE** — same per-query call as the orphaned analytics in Theme 1 |
| `calibration_db.py` async wrapper layer (`get_calibration_async` / `update_calibration_async`) | `calibration_db.py:12/21` | No static reference found for these two; live calibration writes go through `calibration.py` / `sequential_calibration.py` / `src/db/mixins/calibration.py`. **`calibration_health_check_async` was in this row and is NOT dead** — `services/runtime_queries.py:725` calls it from `get_health_check_data` (corrected 2026-08-13). | **DECIDE/CUT** for the remaining two — NB the *store* `core.calibration` is busy (see false-dead list); these *wrappers* are the dead path, not the store |
| `_get_pg_db` private accessor | `calibration.py:110` | no static reference found | **CUT** |
| `check_idle_agents` / `get_recent_events_for_agent` | `event_detector.py:451/495` | no static reference found | **DECIDE** |
| `list_restartable_tasks` | `background_tasks.py:1758` | no static reference found | **DECIDE** |
| `reset_pin_match_scope` | `mcp_handlers/context.py:263` | no static reference found | **DECIDE** |
| `get_reviewer_stuck_recovery` | `mcp_handlers/dialectic/responses.py:43` | no static reference found; same dead stuck-reviewer chain as `check_reviewer_stuck` (CUT below) | **CUT** — fold with `check_reviewer_stuck` |
| `reranker_available` | `reranker.py:190` | no static reference found (`rrf_fuse`/`apply_tag_boost` are live; this flag-check is not) | **DECIDE** |
| `register_extra_schemas` / `register_extra_descriptions` plugin entry-point API | `tool_schemas.py:20`; `tool_descriptions.py:145` | Published `governance_mcp.plugins` hook; **0 consumers** (incl. the plugin repo) | **KEEP-DORMANT** — extension hook, removal is a deprecation decision |
| `gateway_server.py` `main()` script entrypoint | `src/gateway_server.py:99` | Has `__main__`; launched out-of-band via the gateway plist *template* (`scripts/ops/com.unitares.gateway-mcp.plist.template`), not by any import — install state is deployment-specific | **DECIDE** — confirm the gateway plist is installed on the target deployment |

## Genuine cruft — CUT candidates (check the call sites before deleting)

> **This table was headed "the only delete-safe set" until 2026-08-13, when two of its rows
> turned out to be live code.** `log_auto_attest` is called from the governance monitor's
> decision path and `check_reviewer_stuck` from a registered dialectic handler; both said
> "0 callers" and both said Cut. Nothing was deleted, but the label was doing work it had
> not earned. Treat a row here as a *candidate* and confirm the call sites yourself —
> Rule 2 applies in this table more than anywhere else in the file.

| Item | Location | Note |
|---|---|---|
| ~~backfill embeddings script~~ | Removed legacy migration script | Hardcoded to the legacy 384d `core.discovery_embeddings` table, which live search no longer reads — broken against the active bge-m3 model. **CUT** (script-cleanup sweep): removed; recoverable from git history if the legacy table is ever backfilled |
| Legacy `core.discovery_embeddings` table (1887 rows, 384d) | DB | Superseded by `_bge_m3` (1056, clean). **Cut after** concept-extraction confirmed reading the active table |
| `query_response_chain` builder | `src/db/age_queries.py:309` | Dead duplicate; `get_response_chain` uses its own inline Cypher. **Cut** |
| ~~`log_auto_attest` typed helper~~ | `audit_log.py` | **Withdrawn 2026-08-13 — it is called.** See *Verified-wired / false-dead*. |
| Quorum `ESCALATE` resolution branch | `dialectic_protocol.py:196`; handler `:1526` | Retired by design ("0 of 47 sessions ever escalated"). **Cut the enum/dead branch** |
| ~~`answer_question` handler~~ | Removed | **CUT** — it was `register=False` and unrouted; linked answers remain available through `knowledge(action="store", discovery_type="note", response_to={..., "response_type": "answer"})` |
| ~~`check_reviewer_stuck`~~ | `mcp_handlers/dialectic/handlers.py` | **Withdrawn 2026-08-13 — it is called, on a registered request path.** See *Verified-wired / false-dead*. |
| CIRS announce tools (void_alert / state_announce / coherence_report / …) | 7 `register=False` handlers | **Verify before cut** — the CIRS *monitor* path is live (26 `cirs_resonance` events/14d); only these agent-facing announce tools are dark |

## Verified-wired / false-dead (do NOT re-flag these)

The adversarial pass confirmed these are live, correcting plausible "looks dead" reads:

- **`core.calibration`** is busy (version 3958, updated today) — the *real* calibration
  store. Only `core.r1_calibration_state` (the R1 seeded/earned flag) is stale.
- **The AGE graph IS read** — inbound-link scoring in `_blend_with_connectivity` runs on
  every semantic/FTS search. Graph *traversal/synthesis* is dormant; graph *scoring* is wired.
- **The dialectic engine IS firing** — 49 sessions (latest 2026-06-16), auto-resolve sweeper
  every 10 min, reviewer selection/calibration/resolution all live. (#563 heterogeneous
  reviewer is Ollama-contingent, latent-not-unwired.)
- **WAVE_3A `health_check` IS live on BEAM** (:8770, pid 24460 carries the flag). A stale
  second listener (pid 2284) lacks the flag and false-reads "dark" — verify the right pid.
- **The 24 `register=False` handlers** behind the 7 consolidated mega-tools are *delegates*,
  not unwired surface.
- **`retrieval.py` `rrf_fuse`/`apply_tag_boost`**, `find_similar`, `semantic_search`,
  `full_text_search`, `knowledge_graph_lifecycle` (daily task) — all live.
- **`log_auto_attest` IS called** — `governance_monitor.py:1505` invokes it on the
  `audit_logger` singleton (`audit_log.py`, `AuditLogger.log_auto_attest`) while recording an
  un-suppressed decision. Listed as `0 callers` → **Cut** in the CUT-candidates table until
  2026-08-13. The call is a method on an instance, which a symbol-level caller scan does not
  resolve.
- **`check_reviewer_stuck` IS called** — `mcp_handlers/dialectic/handlers.py:1489`, inside
  `handle_get_dialectic_session`, which the generated
  [`TOOL_EDGE_INDEX.md`](../dev/TOOL_EDGE_INDEX.md) shows as the delegate for
  `dialectic(action="get")`. So it sits on a live request path, not a dead one. Listed as
  `0 callers` → **Cut or fold** until 2026-08-13. The call is in the same file as the
  definition, which the same scan also missed.
- **`calibration_health_check_async` IS called** — `services/runtime_queries.py:725`, inside
  `get_health_check_data`. Its two siblings in that row (`get_calibration_async`,
  `update_calibration_async`) do appear unreferenced, so the row was right about the layer and
  wrong about this member; a per-symbol claim cannot be made for a group.
- **`src/trust_contract_lint.py` IS wired**, and its consumer is a test *by design*.
  `tests/test_trust_contract_lint.py` runs `lint_response` against real
  `get_governance_metrics_data` output across every verbosity, for an uninitialized and an
  initialized agent (15 tests, no DB required, standard suite → CI). The module's own
  docstring names "a contract test or a CI gate" as its intended consumers. A response-shape
  validator has no production call site by construction — asserting on live output *is* the
  wiring. Flagged `WIRE` by the Theme 8 sweep on 2026-08-13 and corrected the same day; see
  that theme's false-positive count.

---

## Theme 6 — Shadow-gated validation surfaces (2026-06-26/27)

Capabilities that run in **shadow** (measure, don't act) or are **flag-gated off**,
each with an explicit **wake condition** — the test that separates deliberate dormancy
from avoidance. The rule for this theme: *awaken the eyes (shadow), not the hands
(apply), until the wake condition is met.* This theme exists because "built-but-unwired"
caught one of its own as a silent no-op — #1092's grounding ran **after** its persist +
response consumers and was discarded since it shipped (fixed by #1095, behind flags).

| Capability | Flag / PR | Status | Wake condition |
|---|---|---|---|
| **Autonomous merge execution** — serial PR review/arming by the resident merge conductor | `UNITARES_MERGE_CONDUCTOR_EXECUTE=0` (report-only default); `UNITARES_MERGE_CONDUCTOR_REVIEW=0` controls model calls during shadow | **KEEP-DORMANT (execution); deterministic shadow wired** | Install the SHA-bound `agent-review` required status context; complete a 24h classification-only soak; run at least one low-risk opposite-host review in report-only mode; confirm no PR is readied/armed while a surface claim, conflict, unresolved thread, red CI, stale head, or `merge:hold` exists; then explicitly set EXECUTE=1. Rollback is EXECUTE=0 plus `gh pr merge --disable-auto <n>` for any armed PR. Root/control surfaces remain gated by `merge:root-approved` even after wake. |
| **EISV logprob-grounding** — tier-1 S from model output-distribution entropy | `#1092` Stages 1+2 shipped; Stage 3 (proxy supply) NOT built | KEEP-DORMANT | Build Stage 3 only if `grounding_shadow` shows `s_source="logprob"` S out-discriminates the heuristic **on outcomes**. Off-Claude only (Claude API exposes no logprobs — verified via claude-api skill). Depends on the grounding-apply path (#1095) working first. |
| **Grounding apply** — grounded E/I/S/coherence replace ODE/heuristic in canonical metrics | `UNITARES_GROUNDING_APPLY` / `#1095` | KEEP-DORMANT (apply); shadow available | Run `UNITARES_GROUNDING_SHADOW=1` first → read `grounding_shadow` audit events → set APPLY only if the **fleet-wide** coherence/E/I/S shift (manifold coherence + re-derived E/I fire for *every* agent) causes no harmful verdict flips. LIVE-AFFECTING. |
| **Behavioral-EISV basin** — kernel-split WS1 option b | `UNITARES_BASIN_SHADOW` (RETIRED 2026-06-27; flag + shadow code **removed**) / `#1089` | **DROPPED (verdict D)** | An adversarial design review found the shadow **structurally mis-designed**: `classify_basin` eats `state.coherence = C(V,θ)` (the V-driven thermo tanh, `governance_monitor.py:682`), and the shadow fed behavioral V into the *same* tanh (`:1311`) — so it compared two flavors of one suspect function and could not adjudicate basin honesty. Disagreement was **bifurcated** (Vigil 100% agree / Lumen+Steward+Sentinel 0%), not uniform; `coherence<0.40` was the universal LOW trigger, not "V undamped"; and the premise rode the **latency axis the operator ruled out** (2026-06-24) for ~3 interactive agents/16h with **zero** lock-contention (per-agent locks; residents check in at 5–30min ≫ ODE). **Decision: keep the ODE, don't swap; flag retired.** If basin honesty is ever revisited, the real test is a **manifold-coherence shadow** (the V-free E/I/S coherence already canonical in `src/grounding/coherence.py`), NOT the V-thermo one. |
| **Φ → telemetry-only** — behavioral verdict authoritative; Φ stops flooring risk | `UNITARES_PHI_TELEMETRY_ONLY` (default off) | DECIDE | A values call aligned with the design north-star (Φ = RLHF-shape → demote to predictor). No automated trigger: wake when making the behavioral/residual verdict fully authoritative is the intended posture. |
| **Cold-start risk confirmation actuation** — require adjacent evidence before fallback-owned `risk_pause` can actuate | `GOVERNANCE_COLD_START_RISK_CONFIRMATION_SHADOW=true`; `GOVERNANCE_COLD_START_RISK_CONFIRMATION_ACTUATION=false` | **KEEP-DORMANT (actuation); shadow wired** | Do not awaken from historical labels. First implement durable atomic confirmation state and prove restart/hydration/gap resets fail closed; then collect prospective shadow counts (`shadow_would_defer`, `shadow_confirmed`, ineligibility/reset strata), verify the independent-override bypass end to end, and obtain explicit operator approval. Until all conditions hold, even setting the actuation flag leaves pauses unchanged and reports `actuation_ready=false`. Dialectic review `8539c516649a08af` rejected default-on promotion. |
| **Non-authored Phi cold-start authority guard** — prevent a substrate interpretation/observation or prediction from hard-pausing an identity solely on the non-discriminative fallback before authored/behaviorally ready evidence exists | `GOVERNANCE_NON_AUTHORED_COLD_START_GUARD=true` | **LIVE (default-on, stateless provenance guard)** | This is deliberately not the two-confirmation actuator above: it has no counter and cannot suppress agent-authored, behaviorally ready, independently verified, structural, CIRS, or loop-safety pauses. Prospective trigger: the first 3/3 `shadow_would_defer` rows were `substrate_interpretation` and delivered circuit breakers; a fourth fresh-process reproduction auto-paused on the same epistemic class before its first authored check-in. Roll back on any audit showing the guard applied outside its exact provenance contract by setting the flag false and restarting. Reviewed recovery requires the persisted legacy circuit-breaker envelope and still gates on ownership, reflection, risk, and no-void; legacy `C(V)` is diagnostic context only. |
| **Local-model dogfood fleet** — K free Ollama agents driven through varied real tasks + check-ins *with logprobs* | not built; one-agent seed proven (`dogfood-gemma4` 2026-06-27: first live `s_source="logprob"`, S 0.19→0.48) | DEFERRED | Local models are the ONLY cohort that is free (no paid-API per the operator constraint) AND has logprobs (so tier-1 logprob-grounding actually fires — Claude residents never can) AND is non-resident (fills the grounding-shadow coverage gap). **Wake when there is a real corpus/validation pull** (v7 corpus-maturity, EISV-validation trajectory diversity) — NOT to accelerate the GROUNDING_APPLY decision (that's inventory-ahead-of-demand: no demand, no deadline). Grounding/logprob coverage falls out as a byproduct. Build-gates: real varied tasks + honest self-reports spanning the uncertainty range, and **validate the dogfood EISV distribution matches the real target population** before trusting its flip-profile (the basin-shadow lesson). |

**Counter-example (awakened, for contrast).** `UNITARES_GOVERNED_EFFECT_EXECUTE_AGENT_SPAWN=1`
is a flag-gated capability that *was* deliberately awakened — operator flip 2026-06-25,
a standing execute/RCE surface, persistent. That is what a met wake condition looks like:
a named operator decision, not a default drift-on. Rollback = remove the plist key +
bootout/bootstrap.

**The test — apply to every `KEEP-DORMANT` entry, in this theme and above.** A dormancy
with a written wake condition is discipline; a dormancy with none is avoidance wearing
discipline's clothes — and becomes the looks-dead → deleted → rebuilt cycle this registry
exists to stop. Treat dormancy as a **fermata, not a deletion**: a held note that resolves
on its condition, not one held until the music stops.

## Theme 7 — Unregistered and regressed dormancy (2026-07-31)

**Why this theme exists.** The registry's discriminator — built-but-unwired vs genuinely
unwanted — is made by hand, once, from the 2026-06-16 three-lens inventory. That leaves two
blind spots, and a session on 2026-07-30/31 walked into both. In each case a zero-usage
reading was interpreted as *no demand* when the true cause was *not reachable*.

**The rule.** Zero usage is not evidence. It has at least three causes that are
indistinguishable in a usage metric: (a) no demand, (b) built but broken, (c) built but never
wired. Never retire, re-scope, or de-prioritise a capability on a usage count alone —
establish reachability first. A count of zero from an unreachable surface is not a measurement.

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| **Agent Identity Credential (AIC)** — third-party-verifiable attestation | `src/identity/agent_identity_credential.py` | **Not previously in this registry.** `UNITARES_AIC_SIGNING_KEY` is **unset on the live process** (PID 68291) and the module deliberately treats a missing key as "attestations disabled" rather than auto-generating (`:120-130`, correctly — an ephemeral key would silently invalidate every previously-issued AIC). **Zero non-self callers**: the only external reference is a comment in `src/effect_grant.py:24-37` stating it is *not* the AIC. | **KEEP-DORMANT — wake condition: a consumer that needs third-party verification of an identity claim.** Do not read its zero usage as evidence about demand; it cannot be called. Note the ordering constraint this creates: any decision about what the AIC would attest (e.g. whether compute-derived trust tier 3 may be called "verified") is decidable *now*, precisely because nothing false has been signed yet. |
| **Dialectic review-request channel** — `request_review` / `dialectic(action="request")` | `src/mcp_handlers/dialectic/auth.py:37-49` | **Regressed into unreachability after the 2026-06-16 inventory**, which listed only dialectic *sub*-features (`check_reviewer_stuck`, quorum `ESCALATE`, calibration backfill) and assumed the channel itself worked. Dogfooded 2026-07-31: the auth check resolves the auto-injected `agent_id` (the **public structured handle**, documented `is_identity_key: false`) via `_agent_exists_in_postgres`, which expects a **uuid**. Auto-injection makes the working `if not provided:` branch unreachable for every session-bound caller. Leaves an unsubmittable orphan session (`allowed_agent_ids: []`). Issue **#1414**. | **BROKEN, not dormant — FIX.** The prior "usage = 0, re-open needs a new premise" reading of this channel was measuring the bug. Re-measure adoption only after #1414 lands. |

**The gap this theme names, and the thing worth building.** This registry is a point-in-time
snapshot with no freshness mechanism, so it cannot notice (1) a capability added after the
inventory, or (2) one that regressed into unreachability since. The sibling of
`signal_degeneracy` in `scripts/dev/unitares_doctor.py` — which asks *"can this metric still
move?"* — is a reachability check asking *"can this path still be called?"* Two decidable
queries would have caught both rows above: surfaces with zero calls that appear nowhere in
this registry, and surfaces this registry marks live/RESOLVED that have zero calls.

## Theme 8 — Import-reachability sweep (2026-08-13)

**Method, and why it is weaker than the 2026-06-16 inventory.** Transitive import
reachability from every real consumer — the server entrypoints, each resident's `agent.py`,
and every script under `scripts/` — over `src/`, `governance_core/`, and `agents/`. This is a
*static* pass, so it answers "is there an import edge" and **not** the registry's actual
question, "can this path still be called". It is a cheap first filter, not the reachability
check Theme 7 asks for.

Result: **363 of 381 library modules reachable.** Of the 18 that were not, 12 are
`agents/sdk/` — a package published to PyPI whose consumers are external by design — and one
is `agents/watcher/hook_input.py`, driven by `watcher-hook.sh`. Of the remaining five, one was
a false positive corrected below, leaving the three rows at the end of this theme. All three
are settled: two `KEEP-DORMANT` with wake conditions, and one already carried by Theme 7. This
theme opens no new operator decision.

**The method's own false positives, recorded as a warning. Three in a set of six.** Two came
from the mechanism: `src/mcp_handlers/identity/process_binding_handler.py` and
`src/mcp_handlers/identity/core.py` are both imported by
`src/mcp_handlers/identity/__init__.py` (lines 13 and 18) and are fully wired — the sweep's
relative-import resolution missed them. Caught only by reading the importer by hand.

The third came from the *premise*, and is the more instructive one. The sweep treated "no
importer outside its own tests" as evidence of dormancy, which silently assumes a capability's
consumer must be production code. `src/trust_contract_lint.py` was flagged `WIRE` on that
basis and is not dormant at all: it is a response-shape validator, so it *has* no production
call site by construction, and asserting on live output from a contract test is what wiring it
means. `tests/test_trust_contract_lint.py` already does exactly that, in CI. Moved to
*Verified-wired / false-dead* above.

Both rules bear repeating. Rule 2: **do not act on a static reachability claim without
confirming it at the import site.** And its sharper form, which this sweep had to learn:
**before calling a capability unwired, name the consumer it was built for.** If that consumer
is a test — as it is for a validator, a linter, or a schema guard — then a test importing it
is the wiring, not the absence of it.

`docs/dev/TOOL_EDGE_INDEX.md` covers the complementary surface — the MCP dispatch edges that
are made at import time and are invisible to a static pass like this one.

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| **Orchestrator-vouched identity** — strong cross-process credential for an ephemeral child | `src/substrate/vouch.py` | Zero importers outside its own tests, and the module says so itself (`:1-8`: "INERT proof-of-concept seam… NOT WIRED INTO IDENTITY RESOLUTION"). Pure-logic seam for `docs/proposals/orchestrator-vouched-identity-v0.md`, deferred at the 2026-06-24 Wave-3 gate read. | **KEEP-DORMANT — wake condition: the BEAM Agent Orchestrator landing on a live surface** (Wave 3 decision A). Parked correctly and self-documented, but it had no row here, which is what Theme 7 names as the blind spot: a dormancy documented only inside the dormant file is invisible to anyone auditing from this registry. |
| **Unified configuration manager** — single read-through access point over the four config sources with per-value source/changeability metadata | `src/config_manager.py` | Zero importers outside its own tests, and one commit in its entire history (a repo-wide sweep, not authorship) — so no maintenance drag either. It exposes a strict superset of the live surface: the `config` tool has exactly two actions (`get`/`set` thresholds → `src/mcp_handlers/admin/config.py` → `src/runtime_config.py`), while `config_manager` adds static config, core `DynamicsParams`, server constants, and a `ConfigSource(value, source, changeable, description)` per value. **It is also partly broken, which zero usage had hidden:** the module-level `def get_thresholds` (`:246`) shadows the `from src.runtime_config import get_thresholds` at `:22`, so `ConfigManager.get_thresholds()` calls the module wrapper, which calls `get_config_manager().get_thresholds()`, and recurses. Confirmed 2026-08-13 — `get_thresholds` and `get_all_config` (the flagship read-through) raise `RecursionError`; the other five methods work. `tests/test_config_manager.py:24-34` documents the bug and patches around it, so the suite is green *despite* it, not because it is absent. | **KEEP-DORMANT — wake condition: an operator-facing surface that needs per-value config provenance (where a value came from, and whether it can be changed at runtime). Fix the shadowing first; this cannot be wired as it stands.** Wire this rather than rebuilding the inventory. Not WIRE today: the operator question it answers is already served by the generated `docs/FLAGS.md` plus the doctor, so wiring it would create a second answer to one question. Not CUT: at zero maintenance cost, deleting it is the built → looks-dead → deleted → rebuilt cycle this registry exists to stop. **Axis warning — do not wire this to the trust contract.** `ConfigSource.source` is `static`/`runtime`/`core`/`server`/`env`, i.e. *which layer supplied the value*. `docs/trust-contract.md` §1 provenance is `measured`/`derived`/`prior-default`/`unknown`, i.e. *epistemic status*. They are different axes; presenting the former as the latter would put a provenance-shaped label on a claim it does not support, which is the failure mode §1 exists to prevent. |
| **Agent Identity Credential (AIC)** | `src/identity/agent_identity_credential.py` | Independently rediscovered by this sweep. | **Already listed — see Theme 7.** No new decision; recorded here only so the sweep's output reconciles against the registry. |
