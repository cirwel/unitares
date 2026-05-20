# S10 — Fleet calibration aggregation paths (plan)

**Status:** drafting, 2026-05-19. Unblocked 2026-05-06 when S7 closed.
**Plan row:** `docs/ontology/plan.md` row S10. "Default aggregation unit shifts from UUID to role. Dashboards + external-consumer contracts updated."
**Branch / worktree:** `s10-fleet-aggregation` at `.worktrees/s10-fleet-aggregation/`.

---

## 1. Audit baseline (the mechanical sweep)

Mechanical pass for every site in `src/` that aggregates calibration data by `agent_id` / `agent_uuid`. Ranked.

| # | Site | Shape | Role |
|---|---|---|---|
| 1 | `src/sequential_calibration.py:99` | `agent_states: defaultdict[agent_id → state_dict]` | **Only UUID-keyed aggregation in the calibration domain.** |
| 2 | `src/sequential_calibration.py:260-268` | `record_exogenous_tactical_outcome(agent_id=…)` writes through to `agent_states[agent_id]` and `global_state` | Writer. Hot path from `outcome_events.py:332`. |
| 3 | `src/sequential_calibration.py:311-362` | `compute_metrics(agent_id=Optional[str])` reads agent slice or global | Reader. Per-UUID branch is **dormant** — no live caller passes `agent_id`. |
| 4 | `src/sequential_calibration.py:104, 164-167` | Persist / restore `agents: {agent_id: state}` to disk | Lifecycle. |

**No other UUID-keyed calibration aggregator exists in `src/`.** `src/calibration.py` bins (`CalibrationBin`, `ComplexityCalibrationBin`) are confidence-bucket-keyed, not agent-keyed. `src/drift_telemetry.py` is event-keyed. The S10 surface is bounded to `sequential_calibration.py` and its readers.

### Readers (all pass `agent_id=None` today)

| Caller | Method | Field consumed | Class-relevant? |
|---|---|---|---|
| `src/calibration.py:1155` `_tactical_signal_age_days` | `compute_metrics()` | `last_updated` only | No |
| `src/calibration.py:683` | `compute_per_channel_health()` | `signal_source_outcomes` (not agent_states) | No |
| `src/mcp_handlers/admin/calibration.py:78` `handle_check_calibration` | `compute_metrics()` | `status`, `empirical_accuracy`, `last_updated` from global | **Yes — primary external surface** |

### Dashboard consumer

- `dashboard/dashboard.js:1198` → MCP `check_calibration` → renders calibration card.
- Reads: `calibration_status`, `tactical_staleness_days`, `per_channel_calibration`. No per-class field today.

### Class taxonomy

- `src/grounding/class_indicator.py::classify_agent(meta)` — canonical resolver.
- `src/grounding/onboard_classifier.py::stamp_default_class_tags` — write at onboard.
- `src/grounding/class_promotion.py::class_promotion_sweeper_task` — 30min cadence promotion sweep (S8a Phase 2, #252).
- Live class tags: `substrate`, `session_like`, `engaged_ephemeral`, `ephemeral`. S8b backfill complete 2026-05-05.

---

## 2. Honest re-framing of the plan row

The row reads "shift default aggregation unit from UUID to role." But the current default in every live reader is **global, not UUID** — `compute_metrics(agent_id=None)`. The per-UUID partition is structurally wired (write path populates it) but no reader consumes it.

So S10 is not "stop aggregating by UUID and start aggregating by role." It is:

> **Add per-class as the primary fleet view, with global preserved as the all-bucket sum and UUID retained as drill-down.**

This is the load-bearing read of the row. The dashboard pivots to per-class as its primary calibration card; the `check_calibration` MCP response leads with `by_class` breakdown; global stays available as a summary line; per-UUID stays available as drill-down on demand.

---

## 3. Design decisions (operator-confirmed)

### 3.1 Where per-class becomes default — Dashboard + MCP both pivot
`check_calibration` response leads with `by_class` breakdown; global drops to summary line. Dashboard renders the class strip as the primary calibration card. **External MCP consumers see a contract change** — this is the contract reshape the plan row promises.

### 3.2 Storage shape — Parallel `class_states`
Tracker maintains both `agent_states[uuid]` (authoritative per-UUID counters) and `class_states[class_tag]` (denormalized per-class rollup). Writes update both. Reads from `class_states` are O(1) per class.

**Reclassification drift** is the known cost of this choice (an agent promoted ephemeral → session_like silently strands old counters in the ephemeral bucket). Mitigation: a periodic rebucket pass that rebuilds `class_states` from `agent_states` joined to current `core.identities.class_tag`. Coupled to the existing `class_promotion_sweeper_task` (30min cadence) so rebucket fires whenever promotion fires.

### 3.3 Class source at write time
Three options on the table:

1. **(a) Caller fetches meta + classifies at `outcome_events.py:332`.** The site has `agent_id` only — no `meta` is in scope (live-verified 2026-05-19). S10.2 will add `meta = await load_agent_metadata(agent_id); class_tag = classify_agent(meta)` immediately before the tracker call. ExecutorPool makes the asyncpg read cheap, and outcome events are not a hot path like `process_agent_update`. On fetch failure or timeout, fall through to `UNKNOWN_CLASS_BUCKET` — calibration write stays durable, lookup failures surface as a deficit signal.
2. **(b) Tracker looks up `core.identities` per write.** Same DB hit but inside the tracker — couples a pure observational module to identity storage. Reject.
3. **(c) Tracker caches class-per-UUID, refreshes on sweep tick.** Adds a second source of truth with its own staleness story. Rebucket already covers bulk drift; layering a TTL cache on top reinvents that.

**Chosen: (a).** S10.1 ships the tracker plumbing with the `class_tag` parameter ready to receive. S10.2 wires the fetch at the outcome_events call site.

**Verification corrections (2026-05-19 council pass):**
- Class is stored in `metadata.tags` JSONB array, not as a column. `classify_agent(meta)` reads `meta.tags` to resolve one of: `embodied` / `engaged_ephemeral` / `ephemeral` / `persistent`+`autonomous`. Live distribution: ephemeral=218, engaged_ephemeral=202, persistent variants ~10, embodied=1, untagged=8 (~2% over 437 active agents).
- `session_like` is a derived class (not a literal tag) — `classify_agent` returns it as a fallback when no other tag fires. S10.2's `classify_agent(meta)` call returns the right string regardless.

### 3.4 Response shape (new `by_class` field on `check_calibration`)

```json
{
  "calibration_status": "tracking",
  "by_class": {
    "bootstrapped": true,
    "buckets": {
      "substrate":         {"eligible_samples": ..., "mean_confidence": ..., "empirical_accuracy": ..., "calibration_gap": ..., "signal_sources": {...}, "last_updated": ...},
      "session_like":      {...},
      "engaged_ephemeral": {...},
      "ephemeral":         {...},
      "unknown":           {...}
    }
  },
  "global": {"eligible_samples": ..., "empirical_accuracy": ..., "log_evidence": ..., "capped_alarm": ..., ...},
  "tactical_staleness_days": ...,
  "per_channel_calibration": ...
}
```

`global` retained for backward compatibility and as the all-bucket sum. Removing it is S10-deferred follow-up.

**Anytime-validity scope (S10 council finding).** Class-scope envelopes deliberately omit `log_evidence`, `capped_alarm`, `last_alt_probability`, `last_e_value`. A class bucket under live writes is its own e-process — valid in isolation, not multipliable against `global`/`agent` (same rule the module docstring at `src/sequential_calibration.py:42-45` already states for the global × per-agent product). After `rebucket_from_agent_states` runs, `class_states` is a sum of per-agent `log_e_values` across different `q`-trajectories, which has no martingale interpretation. Rather than expose a field that is sometimes anytime-valid and sometimes not, the class envelope is restricted to descriptive statistics that survive aggregation. Operators reading e-process alarms should read the `global` (or per-agent) envelope, not class.

**Bootstrap labeling.** `by_class.bootstrapped` is `false` until the first `rebucket_from_agent_states` run; this surfaces honestly to consumers during the gap between S10.1 deploy and S10.2 sweeper wire-up. Pre-S10 state files restored at server start retain prior `global` and `agent_states` history but have an empty `class_states` until rebucket runs. Dashboards should render a "by-class data sparse: bootstrap in progress" banner when `bootstrapped=false`.

---

## 4. Sequencing

Three PRs, smallest-first:

| PR | Scope | Touches |
|---|---|---|
| **S10.1** | Tracker plumbing | `src/sequential_calibration.py` — add `class_states`, accept `class_tag` on `record_exogenous_tactical_outcome`, add `compute_metrics_by_class()`. Add rebucket primitive. Persistence schema bump (additive). Tests. |
| **S10.2** | MCP response reshape | `src/mcp_handlers/admin/calibration.py` — surface `by_class`. Wire `class_tag` at call site in `src/mcp_handlers/observability/outcome_events.py:332`. Hook rebucket into `class_promotion_sweeper_task`. Tests + contract test that `by_class` appears. |
| **S10.3** | Dashboard pivot | `dashboard/dashboard.js` calibration card — render class strip as primary, global as summary. |

Each PR independently shippable. S10.1 leaves the by-class data available but unused; S10.2 surfaces it; S10.3 makes it the operator's primary view.

---

## 5. Open questions before code

1. **Persistence schema bump for `class_states`** — additive fields (`classes`, `class_states_bootstrapped`) on the existing JSON state file. Pre-S10 files load cleanly with `bootstrapped=false`. No migration row needed (file is gitignored runtime state).
2. **"unknown" bucket policy** — first-class row, so calibration starvation in the un-classified band reads as a deficit signal.
3. **Drop `global` after a release cycle?** Not in S10 scope; leave as a deferred row in `plan.md` if/when the dashboard read settles.

---

## 6. Council review (fired 2026-05-19, S10.1 revised)

Three-agent parallel adversarial pass per memory ("Council also for load-bearing implementation" + "Council prompts must invite adversarial bug-hunting"). Three findings, all addressed in the revised S10.1.

| Lane | Finding | Resolution |
|---|---|---|
| `dialectic-knowledge-architect` | Summing `log_e_value` across agents via `_merge_state` is **not** a valid e-process — same forbidden product the module docstring already warns against for `global × per-agent`. Class-scope `capped_alarm` would look anytime-valid but isn't. | Class envelope omits `log_evidence`, `capped_alarm`, `last_alt_probability`, `last_e_value` unconditionally (not just post-rebucket). `_state_to_metrics(scope="class", ...)` enforces. Tests pin the contract. |
| `feature-dev:code-reviewer` | (a) `except Exception` in rebucket silently swallows classifier bugs. (b) Pre-S10 files leave class_states sparse vs global with no honest labeling. (c) `last_alt_probability` on class envelope is single-agent provenance. | (a) Classifier exceptions log to stderr with type + agent_id + message, and increment a `classifier_errors` field in telemetry. (b) `class_states_bootstrapped: bool` added to serialized state + envelope; defaults `False` for pre-S10 files with non-empty `agents`; flipped to `True` on first rebucket. (c) Covered by architect's finding. |
| `live-verifier` | Verifier REFUTED two plan assumptions: no `class_tag` column on `core.identities`, and `outcome_events.py:332` has no `meta` in scope. | Cross-checked: class actually lives in `metadata.tags` JSONB array, classifier reads `meta.tags`. 99% of active agents have a recognized tag. Verifier read the wrong state file (worktree's `data/` was contaminated by test runs). Master's production tracker has 95 agents, 5525 global samples, no `classes` key — confirmed pre-S10 state. §3.3 updated: S10.2 fetches meta at the call site (verifier was right about no meta in scope there). |

**Memo lesson** (added to memory candidates): Pre-flagging a concern in the architect's prompt got it confirmed *and* extended (architect went beyond the rebucket case to the live-write case, which I hadn't fully thought through). Worth doing again when I sense a math/ontology issue at the boundary of the change.
