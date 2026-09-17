# EISV instrument-preservation harness and input inventory

**Created:** 2026-09-17
**Status:** Active, **partial**. Tests-only; no runtime change. This is the first
increment of the harness in
the EISV/core boundary proposal, §8.1 and §14
([#2278](https://github.com/cirwel/unitares/pull/2278); `eisv-core-boundary-v0.md` once merged). It does **not** yet satisfy the §13 gate for Stage 1: see
[Not yet pinned](#not-yet-pinned).
**Code:** `tests/test_eisv_instrument_preservation.py`, `tests/eisv_preservation_driver.py`,
`tests/fixtures/eisv_preservation/estimator_trajectories_v0.json`.

The harness pins what parts of the EISV check-in path do today, so that a later
change claiming to leave the registered instrument untouched can be checked
against them. It is a regression pin, not a validity claim: passing means "the
same as the code the golden file was recorded from" (commit, interpreter, and
numpy version are recorded in the file's `provenance` block).

It reads no production data and computes no outcome statistic. The one database
test runs against `governance_test`, asserts that database name before querying,
and limits the read to a one-day window. It calls the read's `fetch_rows`
function directly, outside the registered CLI and its access receipt, so its
separation from the registered read rests on those checks, not on the read
protocol's own guard.

## What is pinned

| Item | Pinned by | Limits |
|---|---|---|
| `UNITARESMonitor.process_update` outputs: action, sub_action, verdict, status, regime, risk, E, I, S, V, coherence, Φ, `trajectory_validation`, confidence source, continuity inputs, behavioral update count and baselined status | golden trajectories: 6 scenarios, 114 check-ins | drives the monitor directly, not `process_agent_update`; shipped default configuration only (below) |
| Warmup into self-relative scoring | `warmup_to_self_relative` | |
| A pause decision | `high_risk_first_checkins` | 2 of 114 check-ins; no refused follow-up, since refusal happens in the handler |
| Elapsed-time scaling, gap arming, and the >10 s calibration-recording gate | 30 s, 45 s, 60 s, 400 s, and 20 h spacing on an injected clock | fixed cadences only; sensitivity to spacing is not measured |
| Sensor EISV tagged `physical` and tagged `behavioral` | `embodied_sensor`, `behavioral_source_sensor` | sensor values are fixed; how the check-in path computes the behavioral sensor is not pinned |
| Behavioral-state serialization round trip | `behavioral_state_roundtrip` | not a restart (no ODE state, 30 s gap, process-global baseline carries over) |
| Post-update effect dispatch order and the early stop when the state row is not recorded | `test_post_update_effects_*` | replaces the effect bodies; says nothing about work inside them or about transaction boundaries |
| State writer → read join: selection at `lead` 0 and 30, the `<=` boundary, synthetic-row exclusion, E/I/S/V/risk mapping, prior-state age | `test_state_writer_rows_are_what_the_registered_read_selects` | timestamps set explicitly; outcome inserted directly; no anchor, fixture-rule, or harness-lane filters; dispersion and telemetry projections not asserted |
| Every real clock read made from repository code during the scenarios is injected or declared | `test_every_real_clock_read_during_the_scenarios_is_declared` | runtime trace, so it covers only code the scenarios execute |
| The pipeline detects an estimator change | `test_an_estimator_change_is_detected` | one mutation (a +0.01 behavioral E observation) |

Determinism was checked manually when the golden file was recorded: identical
output on Python 3.12 and 3.14, with an empty `HOME`, and in a clean export of
the tree with a local `data/calibration_state.json` present. Linux in CI is the
first cross-platform check.

## Configuration profile

The golden file pins the **shipped defaults**: the driver removes every
`UNITARES_*` and `GOVERNANCE_*` variable before configuration is read. A
deployment that sets flags such as the sensor coupling policy, the class
calibration overlay, or the Φ telemetry mode runs different branches, which this
harness does not pin. Recording one deployment's flag values would couple the
shipped tests to one operator's configuration, so a deployment profile needs its
own design (for example, a profile file supplied at run time).

## Not yet pinned

Each of these is required by §8.1 or §14 and is missing, so Stage 1 must not use
this harness as its gate until they are added or explicitly waived:

- the full `process_agent_update` path: `agent_state` assembly in `phases.py`
  (parameters, ethical drift, epistemic class, provenance context, the computed
  behavioral sensor, anomaly `noise_S`, the weak-identity confidence cap),
  monitor lookup and database hydration, locks, and the enrichment pipeline;
- tool responses and envelopes;
- `audit.outcome_events.eisv_*` from real outcome recording,
  `audit.outcome_prediction_bindings`, and `audit.events`;
- real `recorded_at` ordering produced by a check-in and its outcome, which is
  what decides lead-0 selection when both land close together;
- the registered cohort filters (anchor scope, fixture rule, harness lanes) and
  the columns they read;
- prediction registry contents, TTL expiry, and phase-5 prediction reuse;
- `epoch`, Welford and EMA baseline statistics, and the contents of the
  ethical-drift baseline cache;
- concurrent check-ins for one identity;
- failure paths: estimator exception, PostgreSQL or Redis unavailable;
- a real restart from the JSON snapshot or from database rows;
- deployment configuration profiles;
- latency and lock-hold time on a deployment.

§8.1's fixture of "several `core.identities` rows sharing one `agent_id`" cannot
exist: `core.identities.agent_id` is unique. The proposal is corrected separately.

## Estimator input inventory

Inputs `UNITARESMonitor.process_update` reads that the check-in report does not
carry. "Local" means the value is created inside `process_update` and discarded,
so no code outside the estimator can capture what was used (proposal §4.2).

| Input | Source | Local | Harness treatment |
|---|---|---|---|
| Elapsed time, `effective_dt`, gap arming | `datetime.now()` in `src/governance_monitor.py` | yes | injected |
| Dual-log latency and continuity inputs | `datetime.now()` in `src/dual_log/{continuity,operational,reflective,restorative}.py` | yes (`continuity_metrics`) | injected |
| Ethical-drift baseline timestamps | `governance_core/ethical_drift.py` | no | injected |
| Trajectory calibration recording gate (>10 s) | `_time.monotonic()` in `src/monitor_calibration.py` | no (`_prev_checkin_time`) | injected |
| Prediction registry TTL and timestamps | `_time.monotonic()`, `datetime.now()` in `src/monitor_prediction.py` | no | injected |
| Result timestamp | `datetime.now()` in `src/monitor_result.py` | yes | injected |
| Behavioral-state update stamp | `time.monotonic()` in `src/behavioral_state.py` | no | declared uninjected |
| Calibration JSON write debounce | `time.monotonic()` in `src/calibration.py` | no | declared uninjected; writes to a temporary file |
| Audit and drift-telemetry timestamps | `src/audit_log.py`, `src/drift_telemetry.py` | no | declared uninjected; redirected to a temporary directory |
| One-hour tool-usage window | `get_tool_usage_tracker().get_usage_stats` | yes (`tu_stats`) | fixed empty |
| Fleet-shared calibration metrics (S penalty; confidence calibration) | `calibration_checker`, loaded from `data/calibration_state.json` or the database | no (process-global) | empty, JSON-only checker |
| Per-agent ethical-drift baseline | `governance_core.ethical_drift._baseline_cache`, keyed by `agent_id` | no (process-global) | reset per run |
| Outcome history for the behavioral sensor | `monitor._cached_outcome_history`, set by `phases.py` | no | not set (none) |
| Submitted sensor EISV and source | check-in `agent_state` | no | scenario input |
| Configuration flags | `UNITARES_*`, `GOVERNANCE_*` via `config` | no | removed |

## Findings recorded while building it

1. **Clock thresholds, not the continuity rate term, are the estimator's live
   timing dependencies.** Two identical in-process runs first diverged at the
   second check-in through `continuity.E_input` (0.3 against 1.0). That was an
   artifact of runs milliseconds apart: `src/dual_log/continuity.py` clips the
   term to a constant 0.3 at any realistic cadence, as its own comment says. The
   timing dependencies that matter at production cadence are elapsed-time
   scaling and gap arming (`effective_dt`), the >10 s calibration-recording
   gate, and prediction TTLs. Before the calibration clock was injected, the
   harness never exercised calibration recording at all, while production
   reaches that branch whenever check-ins are more than 10 s apart.
2. **The ethical-drift baseline outlives the monitor.** `_baseline_cache` keeps
   an `AgentBaseline` per `agent_id` for the life of the process and hands it to
   any new monitor for that `agent_id`. A process restart clears it.
3. **The registered join has no tie case.** `core.identities.agent_id` is unique
   (`identities_agent_id_key`) and `core.agent_state` is unique on
   `(identity_id, recorded_at)`, in both the live and test schemas.

## Changing the golden file

The failure message does not suggest regenerating, on purpose: a changed pin is
an instrument change. Before the registered read's preservation horizon,
regeneration needs the operator decision recorded in the proposal (§12.1).
Otherwise, regenerate only with a change that is meant to alter EISV behaviour,
and say so in that change:

```bash
python -m tests.eisv_preservation_driver --write-golden
```

The driver removes configuration variables itself and stamps the `provenance`
block. Nothing mechanical ties regeneration to that decision: the fixture path is
listed in `.github/CODEOWNERS`, which only binds if code-owner review is required,
and the inventory lists in the driver can be edited in the same change. Reviewers
should treat any diff to the golden file or to `CLOCK_SOURCES`,
`UNINJECTED_CLOCK_READS`, or `PROCESS_GLOBAL_STATE` as an instrument change.
