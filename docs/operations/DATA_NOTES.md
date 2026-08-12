# UNITARES Governance — Data Notes

*Operational data dictionary for the production governance database as of 2026-04-10. Written to make the current state of the data legible before any reproducibility analysis or paper evidence compilation.*

---

## Database

**DSN:** `postgresql://postgres:postgres@localhost:5432/governance`

| Schema | Status | Summary |
|--------|--------|---------|
| `core` | Active (14 tables) | Agent lifecycle: state records, sessions, identities, baselines, calibration, dialectic, discovery embeddings |
| `audit` | Active (19 tables incl. partitions) | Event log, outcome events, tool usage — the primary observational record |
| `governance_graph` | Active (9 tables) | Apache AGE graph extension for agent relationship graph |
| `knowledge` | Active (6 tables) | Knowledge graph layer (entries, embeddings, tags) |
| `ag_catalog` | Active (2 tables) | AGE internal catalog; do not modify |
| `public` | Empty, unused | — |

---

## Primary tables for behavioral / governance analysis

### `audit.events`

- **Rows:** 43,627
- **Date range:** 2025-12-11 → 2026-04-10
- **Partitioning:** Monthly range partitions — `events_2025_12` through `events_2026_05`
- **Indexes:** `events_pkey (ts, event_id)`, `idx_audit_agent_time (agent_id, ts)`, `idx_audit_ts (ts DESC)`, `idx_audit_type (event_type)`
- **Purpose:** Every governance check-in event across all agents. The primary observational record; one row per automated governance decision.

**Event type distribution:**

| event_type | Count | Description |
|---|---|---|
| `auto_attest` | 21,535 | Automated governance check-in (primary verdict mechanism) |
| `cross_device_call` | 8,476 | Cross-device/cross-session MCP tool calls |
| `complexity_derivation` | 6,273 | Complexity estimation vs. reported discrepancy tracking |
| `eisv_sync` | 3,661 | Lumen sensor state → governance layer sync |
| `lambda1_skip` | 2,340 | λ₁ update skipped (confidence below threshold) |
| `lifecycle_archived` | 1,072 | Agent session archived |
| `identity_claim` | 221 | Identity binding / claim events |
| `lifecycle_paused` | 35 | Agent session paused |
| `auto_resume` | 32 | Automatic session resume after pause |

### `audit.outcome_events`

- **Rows:** 10,789
- **Date range:** 2026-02-24 → 2026-04-10
- **Partitioning:** Monthly range partitions — `outcome_events_2026_02` through `outcome_events_2026_05`
- **Purpose:** Pairs an EISV snapshot with a measurable outcome (task pass/fail, tool rejection, etc.), enabling post-hoc validation of whether EISV state predicts real-world results.
- **Key distinction:** `audit.events` records every governance check-in; `audit.outcome_events` records only events where an outcome can be measured and scored.

Outcome types: `drawing_completed`, `drawing_abandoned`, `test_passed`, `test_failed`, `tool_rejected`, `task_completed`, `task_failed`

Columns include an embedded EISV snapshot at outcome time: `eisv_e`, `eisv_i`, `eisv_s`, `eisv_v`, `eisv_phi`, `eisv_verdict`, `eisv_coherence`, `eisv_regime`.

### `core.agent_state`

- **Rows:** 224,192
- **Purpose:** Per-check-in state snapshots for each agent. Stores EISV coordinates, coherence, risk, decision history, and derived complexity for every governance update call.
- **Note:** Approximately 96,000 rows are Lumen sensor-driven updates (coming through `eisv-sync-task`). These dominate the row count and reflect physical sensor polling, not LLM interaction.

### Other tables (brief pointer)

| Table | Purpose |
|---|---|
| `core.agents` | Agent registry — one row per registered agent, stores agent metadata and registration timestamp |
| `core.agent_sessions` | Per-agent session records; maps session IDs to agents and timestamps |
| `core.agent_baselines` | Per-agent EISV baselines — the "ground truth" reference state for drift detection |
| `core.agent_behavioral_baselines` | Behavioral baselines (EMA of observed E/I/S behavior vectors) |
| `core.identities` | Identity v2 records; maps agent fingerprints to resolved identity claims |
| `core.sessions` | Session-level metadata including continuity tokens and bind status |

---

## The `payload` JSON in `audit.events` (auto_attest events)

These keys appear in `payload` for `event_type = 'auto_attest'`:

| Key | Count | Type | Meaning | Code reference |
|---|---|---|---|---|
| `risk_score` | 21,552 | float [0,1] | Composite risk score produced by the governance layer. Higher = more risk. | `src/audit_log.py:73`, computed in `src/governance_monitor.py` |
| `ci_passed` | 21,552 | boolean | "Continuous integrity" check result. In the current production path (`governance_monitor.py:960`), this is hardcoded `False` with the comment "CI status not available in governance_monitor". All live auto_attest rows have `ci_passed=false`. | `src/audit_log.py:72`, `src/governance_monitor.py:960` |
| `decision` | 21,552 | string | Operational decision: `proceed` or `pause`. This is the coarse binary outcome written to the payload. | `src/audit_log.py:74` |
| `reason` | 21,493 | string | Human-readable explanation for the verdict/decision, e.g. "UNITARES high-risk verdict (risk_score=0.82)". | `src/governance_monitor.py:964` |
| `unitares_verdict` | 21,435 | string | Verdict class: `safe`, `caution`, or `high-risk`. This is the primary governance signal. See **Verdict vocabularies** below. | `src/monitor_decision.py:36`, `src/governance_monitor.py:1014` |
| `coherence` | 21,435 | float [0,1] | Compatibility scalar at attestation time. Interpret only with `state_json.coherence_form` (and response `coherence_source` / `coherence_role`): default `legacy_tanh_v` is directional ODE control feedback, not a health score. Historical rows without provenance are unknown-source. | `src/governance_monitor.py` |
| `void_active` | 21,435 | boolean | Whether the void accumulator (E-I imbalance accumulator) is active. When `true`, the agent is in an imbalanced state where engagement diverges from inhibition. | `src/governance_monitor.py:966` |
| `continuity` | 6,719 | object | Nested object capturing complexity continuity metrics: `E_input`, `I_input`, `S_input`, `self_cx`, `overconf`, `underconf`, `derived_cx`, `divergence`. Present only when continuity metrics are computed (not on every check-in). | `src/governance_monitor.py:969-979` |
| `beh_obs` | 6,719 | array of 3 floats | Behavioral observation vector `[beh_E_obs, beh_I_obs, beh_S_obs]` — the behavioral EMA of observed engagement, inhibition, and stability. Present alongside `continuity` when behavioral state has sufficient confidence. | `src/governance_monitor.py:968` |
| `k` | 1 | unknown | Appears exactly once in the dataset. Definition not found in a quick scan — see code. Likely a one-off logging artifact. |

**Note on `ci_passed`:** The field was designed to record a continuous-integrity check (analogous to a CI pipeline pass/fail), but the production `governance_monitor` path always writes `False` because that check runs in a different code path. Any analysis of `ci_passed` from the live DB will see all-false values and should treat this field as currently non-informative.

---

## Verdict vocabularies

Two distinct verdict vocabulary sets exist in the system:

**1. `unitares_verdict` — coarse 3-class verdict (`safe / caution / high-risk`)**

Used in:
- `audit.events.payload->>'unitares_verdict'`
- `audit.outcome_events.eisv_verdict`

Computed by the phi-based ODE path (`src/monitor_phi.py:27`) and optionally overridden by the behavioral assessment path (`src/governance_monitor.py:933-934`). Defined in `src/monitor_decision.py:36`.

**2. `verdict` — fine-grained operational verdict (`proceed / guide / pause / reject`)**

Used in:
- `src/services/runtime_queries.py:99-124` (described as "PRIMARY for verdicts. Observation-first EMA of actual agent behavior. Determines proceed/guide/pause/reject decisions.")
- Decision dict `action` field (e.g., `decision['action']` in `governance_monitor.py:962`)

**Relationship:** The coarse verdict (`safe/caution/high-risk`) maps onto the fine-grained action via `src/monitor_decision.py`. `high-risk` → `pause`. `caution` at low risk → `proceed` with `sub_action='guide'`. `safe` → `proceed`. The fine-grained verdict `guide` is a sub-action of `proceed`, not a top-level value — it does not appear in `audit.events.payload->>'decision'` (which stores only `proceed` or `pause`).

The exact mapping between the behavioral `verdict` field in `runtime_queries.py` and the `unitares_verdict` written to the audit log is not fully documented in a single place; `governance_monitor.py:928-934` is the merge point.

---

## Agent taxonomy

| Agent category | Example IDs | Role | Independent? |
|---|---|---|---|
| `test_stress` | `test_stress` | Synthetic stress-test fixture used to validate the governance system under intentional high-drift conditions. Not production traffic. | No — synthetic |
| `eisv-sync-task` | `eisv-sync-task` | Periodic cron task that pushes Lumen's physical sensor-derived EISV into the governance layer. Runs on Lumen's behalf. | Yes — independent ground truth |
| UUID-named LLM dev agents | `69a1a4f7-...`, `7d9966bb-...`, `85e15f04-...` | Claude Code sessions that were actively constructing UNITARES during the deployment window. The system governed its own construction crew. | No — co-constructed |
| Lumen | `eisv-sync-task` (via sync) + direct Pi agent | Embodied agent on Raspberry Pi with physical sensors. Sensor readings provide independent ground truth regardless of the governance layer. | Yes — clean instrument |

For the seesaw caveat (the circular dependency between the governed population and the governing system), see `docs/operations/DEPLOYMENT_DATA_CAVEAT.md`.

---

## Known gaps in documentation

1. **`k` payload key:** Appears exactly once in `audit.events` for `event_type='auto_attest'`. Origin unknown; likely a one-off logging artifact.
2. **`ci_passed` is always `False` in production:** The field was designed for a CI check that runs in a different code path. All live `auto_attest` rows have `ci_passed=false`. This is noted in `governance_monitor.py:960` but not prominently documented elsewhere.
3. **`verdict` vs. `unitares_verdict` exact mapping:** The two vocabularies are defined separately and merged at `governance_monitor.py:928-934`. No single authoritative mapping table exists; `guide` is a `sub_action` of `proceed`, not a top-level decision value.
4. **UUID-to-date mapping for LLM dev agents:** The four UUID-named agents likely correspond to specific development sessions but no explicit session-to-date log exists. The date ranges could be reconstructed from `audit.events WHERE agent_id = '<uuid>'` but have not been documented.
5. **`beh_obs` interpretation:** The vector `[beh_E_obs, beh_I_obs, beh_S_obs]` is confirmed at `governance_monitor.py:968` but the normalization, scale, and what constitutes a "normal" range are not documented outside the code.
6. **`eisv_regime` field in `outcome_events`:** The possible values and how regimes are classified are not documented in the migration file or in this notes file; see `governance_core` (compiled package).
