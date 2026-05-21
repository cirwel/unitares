---
status: SHIPPED — Phase 3 sites implemented; audit retained as historical control surface (see parent `onboard-bootstrap-checkin.md` SHIPPED status)
authored: 2026-04-25
of: onboard-bootstrap-checkin.md (v2.1)
phase: 3 (split into 3a + 3b per the operator's directive 2026-04-25)
council_threshold: ">10 distinct read sites = pause and escalate"
total_code_sites: 5 (4 in Phase 3a, 1 in Phase 3b)
total_invariant_sites: 3 (calibration, export, trust-tier — tests only, no code change)
escalation_status: "below threshold — proceed solo"
---

# Filter Audit — onboard-bootstrap-checkin

**Purpose:** This artifact is the control surface for Phase 3 of `onboard-bootstrap-checkin.md`. Per spec §4.1, every read path that aggregates `core.agent_state` rows must be classified here before any filter code lands. Code follows this list — it does not discover sites mid-patch.

**Threshold:** if the audit names >10 distinct code-change sites, Phase 3 stops being a contained filter pass and becomes a wider state-model migration that requires council escalation. **Current count: 5 code sites + 3 invariant-only items. Below threshold. Proceed.**

**Method:** exhaustive grep for `core.agent_state`, `mv_latest_agent_states`, and the DAO methods that read those tables (`get_latest_agent_state`, `get_agent_state_history`, `get_all_latest_agent_states`, `get_recent_cross_agent_activity`, `get_latest_eisv_by_agent_id`), plus their downstream callers. Verified against the live codebase as of master `311613a7`.

---

## Site classification matrix

### Phase 3a — load-bearing reads (user-visible / runtime-critical)

These are reads where a bootstrap row appearing as the most-recent measured state causes a **wrong answer** to a question a user, a Sentinel, or an outcome correlator is asking.

| # | Site | File:line | Decision | Enforcing test |
|---|------|-----------|----------|----------------|
| 1 | `get_latest_agent_state` (DAO) | `src/db/mixins/state.py:134` | exclude `synthetic = true` | `test_get_latest_excludes_bootstrap` |
| 2 | `get_all_latest_agent_states` (DAO, matview + base-table fallback) | `src/db/mixins/state.py:179` | **matview rebuilt as measured-only** (migration 019: `WHERE synthetic = false` in matview SELECT); base-table fallback adds query-time `WHERE s.synthetic = false`. Cleaner than query-time filtering over an unfiltered matview. | `test_all_latest_excludes_bootstrap` (covers matview path AND fallback path) + `test_matview_definition_excludes_synthetic` (matview rowset itself does not contain bootstrap rows) |
| 3 | `get_recent_cross_agent_activity` (DAO) | `src/db/mixins/state.py:210` | exclude `synthetic = true` | `test_cross_agent_activity_excludes_bootstrap` |
| 4 | `get_latest_eisv_by_agent_id` (DAO) | `src/db/mixins/tool_usage.py:106` | exclude `synthetic = true` | `test_outcome_correlation_excludes_bootstrap` |

**Downstream callers covered by Phase 3a (no per-caller change needed — they all delegate to the DAO):**
- `src/agent_storage.py:122, 467, 584` — `get_agent` / `list_agents` / module-level `get_latest_agent_state` use #1
- `src/temporal.py:93` — temporal narrator latest-state read uses #1
- `src/temporal.py:119` — temporal narrator cross-agent activity uses #3
- `src/mcp_handlers/admin/dashboard.py:24` — `dashboard.handle_dashboard_overview` uses #2
- `src/mcp_handlers/observability/outcome_events.py:94` — `outcome_event` correlator uses #4

### Phase 3b — audit-discovered reads + refuse-with-explanation paths

These are reads where bootstrap leakage is **either subtle (post-genesis trajectory priors) or transitive (self-recovery / dialectic via in-memory hydration)** rather than directly user-visible. Phase 3b lands before Phase 4 (hook).

| # | Site | File:line | Decision | Enforcing test |
|---|------|-----------|----------|----------------|
| 5 | `get_agent_state_history` (DAO) | `src/db/mixins/state.py:157` | **default include synthetic** — matches the audit/lineage rule and avoids silently hiding bootstrap rows from historical/debug reads. Add `exclude_synthetic: bool = False` parameter (visible in DAO signature + this audit doc); the choke point for "measured-only history" is the explicit parameter plus tests, not a hard DAO default. History is not one semantic thing — callers legitimately need both "full record" and "measured-only record." | `test_history_preserves_synthetic_by_default` + `test_history_with_exclude_synthetic_filters` |
| 6 | `hydrate_from_db_if_fresh` (in-memory monitor seeding from DB) | `src/agent_monitor_state.py:236` | call site uses `get_agent_state_history(..., exclude_synthetic=True)` so the in-memory monitor is never seeded from a synthetic row — this is the dialectic-flagged "trajectory integrator's prior-read at update time" | `test_hydration_excludes_bootstrap` (hydrating an agent with bootstrap-only history yields update_count=0) |

**Downstream paths covered by site #6 (refuse-with-explanation falls out structurally):**
- `src/mcp_handlers/lifecycle/self_recovery.py:253, 375, 564` — reads `monitor.state.coherence`, `monitor.state.void_active`, etc. After Phase 3b, a bootstrap-only agent has `monitor.state.update_count == 0`, so self-recovery's existing "no measured state yet" guard naturally fires. The refuse-with-explanation contract from spec §4 inclusion list #6 holds without additional code at the self-recovery site.
- `src/mcp_handlers/dialectic/handlers.py:505, 1849` — same pattern. `paused_agent_state = monitor.state.to_dict()` returns a dict with `update_count=0` and zeroed histories for a bootstrap-only agent. Dialectic's existing "needs measured trajectory" guard handles the refusal. Verify with test, no separate code change.

`src/temporal.py:105` calls `get_agent_state_history(identity_id, limit=50)` with the new default (`exclude_synthetic=False`), which preserves the bootstrap row in the temporal narrator's history view — that's intentional (temporal context is descriptive, not measured). Verified with test that the row carries `synthetic=true` for inspection.

### Invariant-only items (Phase 3a tests, no code change)

These are paths that *appear* to be filter sites but are safe-by-construction. The audit document records them so the invariant is explicit; tests prove the protection holds.

| # | Item | Why it's safe today | Test |
|---|------|---------------------|------|
| I1 | Calibration ingestion (`auto_ground_truth.collect_ground_truth_automatically`) at `src/auto_ground_truth.py:385,432` | Queries `audit.events WHERE event_type = 'auto_attest'`. Bootstrap writes via `record_bootstrap_state` (Phase 2) do NOT emit `auto_attest` events — they only INSERT a `core.agent_state` row. The exogenous-signal gate at line 432 also requires `tests`/`commands`/`files`/`lint` keys, which bootstrap state_json never carries. **Locked in by:** `test_calibration_excludes_bootstrap` (assert that even N bootstrap rows produce zero calibration entries). |
| I2 | Export bundle (`handle_get_system_history` at `src/mcp_handlers/introspection/export.py:52`) | Reads from `monitor.export_history()` — the in-memory monitor's history, NOT DB rows. The bootstrap row never flows into this export today. The spec §4 inclusion rule for "exports preserve `synthetic` flag" is **vacuous until export is wired to DB-sourced state rows.** Documented as a follow-up, not a Phase 3 deliverable. **No test added** — there's nothing to enforce in current code. |
| I3 | Trust-tier observation count (`compute_trust_tier` at `src/trajectory_identity.py:692`) | Reads `observation_count` from trajectory metadata, NOT from `COUNT(*)` over `agent_state` rows. The bootstrap write helper (`bootstrap_checkin.write_bootstrap`) does not call `store_genesis_signature` or otherwise touch trajectory metadata. **Locked in by:** `test_trust_tier_excludes_bootstrap` (assert a bootstrap-only agent's trust tier reflects zero observations). |

---

## What's NOT in this audit

- **`record_agent_state`, `record_bootstrap_state`, `get_bootstrap_state`, `is_substrate_earned`** — these are write paths or already-synthetic-aware reads. No filter needed.
- **`agents/sentinel/` aggregation** — Sentinel reads via `dashboard.handle_dashboard_overview` (covered by site #2) and `get_recent_cross_agent_activity` (site #3). It does not touch `core.agent_state` directly. Verified by `grep -rn "core.agent_state\|mv_latest" agents/sentinel/`.
- **Knowledge-graph reads** — KG never queries `core.agent_state`.
- **Audit log queries** — they read `audit.events`, never `core.agent_state`.
- **The matview-refresh background task** (`src/background_tasks.py:187`) — DDL operation, not a read.

---

## Phase split

**Phase 3a (this PR):**
- Sites #1, #2, #3, #4 — DAO-level filter changes.
- Tests for #1, #2, #3, #4 + invariant tests I1, I3.
- This audit document committed in the same PR as the filter changes.

**Phase 3b (next PR after 3a merges):**
- Site #5 — `get_agent_state_history` gains `exclude_synthetic: bool = False`.
- Site #6 — `hydrate_from_db_if_fresh` passes `exclude_synthetic=True` so the in-memory monitor never inherits bootstrap state.
- Verification tests that self-recovery and dialectic refuse-with-explanation for bootstrap-only agents fall out structurally.

**Phase 4 (hook integration) gates on both 3a AND 3b being merged.** Phase 2 is currently safe only because no caller writes `initial_state`. Once the SessionStart hook lands, any missed filter becomes semantic contamination.

---

## Caveats

- **The matview is rebuilt by Phase 3a (migration 019) as measured-only.** Phase 1 (migration 018) projected the `synthetic` column for filterability; Phase 3a goes one step further and bakes `WHERE synthetic = false` into the matview definition itself, so the matview rowset never contains bootstrap rows. This is cleaner than relying on query-time filtering over a mixed-content matview — a reader who never expects synthetic rows in the matview can't accidentally introduce a bug by writing a SELECT that omits the filter. The base-table fallback in `get_all_latest_agent_states` still adds a query-time `WHERE s.synthetic = false` because it queries the base table directly.
- **The default for `get_agent_state_history` stays "include synthetic"** because changing it would be a silent semantic change for every existing caller, and history-as-audit-record legitimately wants the full picture. The hydration call site at #6 is the only one that needs `exclude_synthetic=True`; everywhere else the synthetic flag travels as data (per spec §4 inclusion rule #2 "Identity audit / lineage queries").
- **`get_agent_state_history` is the only DAO method whose default behavior intentionally INCLUDES synthetic rows.** This is documented inline in the DAO docstring after Phase 3b lands.
