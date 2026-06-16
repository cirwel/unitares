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
3. **New capability should not merge unwired.** Ship it with one wired consumer, *or* add
   a `KEEP-DORMANT` entry here naming what would wire it. "Wire-on-build."
4. **Status legend:** `WIRE` = should be connected, has clear payoff · `KEEP-DORMANT` =
   deliberately parked (roadmap frontier or external-dependency gated) · `DECIDE` = needs
   an operator wire/cut call · `CUT` = genuine cruft, safe to remove.

---

## Theme 1 — The graph is written but never reasoned over

The write side lays AUTHORED / RELATED_TO / TAGGED / SPAWNED edges (all live), but the
higher-order graph *reasoning* over them is absent or hand-rolled in SQL/Python. This is
the highest-value cluster and the core of the AGE-canonical direction. The recurring
false-archival bug lives here.

| Capability | Location | Live evidence | Status |
|---|---|---|---|
| SPAWNED lineage DAG — written, never traversed | `db/age_queries.py:222` (creator); `mcp_handlers/identity/handlers.py:2642` | 294 SPAWNED edges + 594 Agent vertices; **zero** `MATCH …SPAWNED…` traversal anywhere | **WIRE** — add `descendants_of`/`live_descendant_reachable`; route the archival liveness gate through it (AGE-canonical step 2) |
| Lineage liveness reasoning hand-rolled, single-hop | `mcp_handlers/lifecycle/stuck.py:173/215/578` | Reads `parent_agent_id` as a flat attr over in-memory metadata; cannot do multi-hop reachability; ignores the SPAWNED DAG | **WIRE** to the above — the false-archival bug is the symptom |
| `supersedes=` store param never creates the SUPERSEDES edge | `mcp_handlers/knowledge/handlers.py:879-884` | Store path sets the SQL `superseded_by` field but never calls `supersede_discovery()`; **0 SUPERSEDES edges** | **WIRE** — one line; auto-activates the inert ranking penalty below |
| SUPERSEDES connectivity ranking penalty | `storage/knowledge_graph_age.py:1720,1740` | Coded into every search blend but always 0 (no edges exist) | auto-fixes once the edge is wired |
| RESPONDS_TO edges + `get_response_chain` | `storage/knowledge_graph_age.py:538,558`; handler `:2107` | Read path wired behind `include_response_chain`; **0 callers set it; 0 edges ever written** | **DECIDE** — have the respond/dialectic flow set `response_to`, or cut the chain |
| Search "graph expansion" reads a SQL field, not the graph | `retrieval.py:100`; handler `:1197` | Gated off (`UNITARES_ENABLE_GRAPH_EXPANSION` unset); even when on, reads `related_to` SQL field, not the 2186 RELATED_TO Cypher edges | **DECIDE** — repoint to a Cypher neighbor fetch, or stop calling it graph |
| Cross-agent knowledge-flow query (collaboration DAG) | `db/age_queries.py:330` | Pure graph-native Cypher; **0 consumers**; data exists (1765 AUTHORED + 2186 RELATED_TO) | **WIRE** — the showcase "graph-native" query; runs today on live data |
| Orphaned AGE analytics queries (entropy↔work, unresolved-questions-with-entropy, etc.) | `db/age_queries.py:309/352/359/379/406` | Zero callers outside the module (only `query_tags_with_discoveries` is wired) | **DECIDE** per query — dashboard-panel-shaped vs superseded |
| `link_discoveries` manual typed-edge API | `storage/knowledge_graph_age.py:2034` | 0 callers, no MCP tool exposes it | **KEEP-DORMANT** — useful if a manual-curation surface lands |

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
| `trajectory_shape_similarity` / `_eisv_trajectory_similarity` (DTW discrimination primitive) | `trajectory_identity.py:363/215` | 0 callers; central to the trajectory-identity paper; nothing emits the per-dim arrays it needs | **DECIDE** — wire `compute_behavioral_trajectory` to emit + call, or cut |
| Lineage credit-assignment aggregation | `identity/provenance_chain.py:83/170` | Read/scoring half orphaned; write half produces empty chains (0/1056) | **DECIDE** — depends on whether discovery→lineage attribution is still a goal |
| S22 H5 cross-harness coverage assessor | `identity/s22_h5_comparison.py:110/190/277` | Diagnostic-script-only; input data live (30k provenance rows) but no MCP surface reads the gate | **DECIDE** — surface via `get_governance_metrics`, or keep as a script |
| `backfill_calibration_from_historical_sessions` | `mcp_handlers/dialectic/calibration.py:193` | One-shot admin migration util; no scheduled caller (by design) | **KEEP-DORMANT** — document as manual-only |

## Genuine cruft — CUT candidates (the only delete-safe set)

| Item | Location | Note |
|---|---|---|
| `backfill_embeddings.py` | `scripts/migration/backfill_embeddings.py` | Hardcoded to the legacy 384d `core.discovery_embeddings` table, which live search no longer reads — broken against the active bge-m3 model. **Cut or repoint to `get_active_table_name()`** |
| Legacy `core.discovery_embeddings` table (1887 rows, 384d) | DB | Superseded by `_bge_m3` (1056, clean). **Cut after** concept-extraction confirmed reading the active table |
| `query_response_chain` builder | `db/age_queries.py:309` | Dead duplicate; `get_response_chain` uses its own inline Cypher. **Cut** |
| `log_auto_attest` typed helper | `audit_log.py:206` | 0 callers; real rows written by a different `log_event` path. **Cut** |
| Quorum `ESCALATE` resolution branch | `dialectic_protocol.py:196`; handler `:1526` | Retired by design ("0 of 47 sessions ever escalated"). **Cut the enum/dead branch** |
| `answer_question` handler | `mcp_handlers/knowledge/handlers.py:2342` | `register=False` AND unrouted in `consolidated.py` — unreachable. **Cut or route** |
| `check_reviewer_stuck` | `mcp_handlers/dialectic/handlers.py:115` | 0 callers; auto-resolve handles reassignment. **Cut or fold** |
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
