# EISV Live-Path Derivation and Calibration Ledger

Reference companion to `skills/governance-fundamentals/SKILL.md`. Load when
inspecting `reading_coverage`, `outcomes_in_e`, era flags (`SH1`/`HU1`/`SD1`),
cold-start stage math, or calibration ledger and corroboration mechanics.

## How the Live Path Reads E, I, S, and V

- **E (Energy)** blends complexity divergence and recent recorded outcomes
  (renormalized to 50/50 of the base weight by default under
  `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`, which excludes prior governance
  decisions and the legacy `legacy_tanh_v` coherence-level self-loop; setting
  `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=0` restores those legacy controller
  terms), with optional actor work-tool error rate from caller-bound
  `audit.tool_usage` (`UNITARES_ACTOR_WORK_TOOL_STATS=1`, era `SH1`).
  `continuity_E_input` is omitted by default under
  `UNITARES_DROP_CONTINUITY_E_BLEND=1` (`SH1`). The outcome term applies once
  three or more outcomes are recorded in the last 24h. It counts them as
  recorded, of any provenance, without attribution to whether your change caused
  them, and an adverse one means rework, not fault. When an adverse outcome
  counts, the check-in carries `outcomes_in_e` saying which outcomes counted,
  the term's share of the observation, and what one more adverse outcome would
  cost.
- **I (Integrity)** blends eligible per-agent calibration accuracy (pooled with
  a cohort prior when supplied, or tactical calibration once five or more recent
  verified confidence–outcome pairs exist) and per-agent outcome-score
  consistency (`consistency_score`, once three or more non-echo outcomes exist
  in the last 24h, excluding `outcome_event` echo duplicates). Those two
  evidence gates are independent: when both are active their base weights
  renormalize to `5/7` (`calibration_accuracy`) and `2/7` (`consistency_score`);
  when only one gate is met, that term carries `100%` of the base; only when
  neither gate is met does the component arithmetic use the neutral `0.75`
  `uncalibrated_base_fallback` (while the axis itself holds unread under `HU1`;
  default under `UNITARES_PER_AGENT_I_CALIBRATION=1` and
  `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`, which exclude the legacy
  `legacy_tanh_v` coherence-trend self-loop and lifetime fleet-strategic bins;
  setting those flags to `0` restores the legacy terms), plus optional
  `continuity_I_input` (`unique_tools_ratio` is omitted by default under
  `UNITARES_DROP_UNIQUE_TOOLS_I_BLEND=1`, era `SH1`). Outcome consistency
  measures variance: constant success and constant failure scores both have zero
  variance. Outcome success enters E separately.
- **S (Entropy / drift)** rises with an agent-local drift norm and complexity
  divergence (with the controller's regime-instability self-loop neutralized by
  default under `UNITARES_NEUTRALIZE_SENSOR_SELF_LOOPS=1`), plus optional
  `continuity_S_input` and actor work-tool call velocity
  (`UNITARES_ACTOR_WORK_TOOL_STATS=1`, era `SH1`). By default
  (`UNITARES_SENSOR_DRIFT_AGENT_LOCAL=1`, era `SD1`) the drift norm is built
  only from your supplied complexity divergence and your own tactical
  calibration error once ten or more recent verified pairs exist; the shared
  stability and fleet-calibration components are left out, and with neither
  present the drift term is unread rather than a fleet constant.
  `derivation.drift_input` records what was used.
- **V (Valence)** smooths `E_obs - I_obs` once, with its own EMA rate, stepping
  only when both E and I were read under `HU1`. Positive records a positive
  smoothed `E_obs - I_obs` gap; negative records the reverse. Its different
  smoothing rate means V is not generally the difference of the current smoothed
  E and I values. It adds a temporal summary, not another external observation.
  "Running hot" and "running careful" are interpretations of the gap.

## Unread Axes (`HU1`) and Estimation Stages

An axis is a reading only when at least one of its base terms was supplied. When
none was, the axis is held (default ON under `UNITARES_HOLD_UNREAD_AXES=1`, era
`HU1`): the published number stays at its last reading, or at a bootstrap
placeholder if it was never read, and the assessment stops scoring it once ten
check-ins pass without a reading (`behavioral.assessment.unscored_axes` lists
the axes a verdict says nothing about).

The check-in response reports this in `reading_coverage`:

- `measured` lists the axes with a supplied base term.
- `defaulted` lists the rest with the terms that were missing.
- `v_rests_on_defaults` is true when E or I is a default, since V is derived
  from them.

Filtered response modes show `reading_coverage` whenever the set of defaulted
axes changes from the previous check-in, including when a reading becomes fully
measured; full mode always carries it.

With unread axes held (the default), live estimation stages count check-ins that
read something:

1. A new agent's first three check-ins read nothing, so check-ins 1–5 use the
   mostly server-derived Φ cold-start prior.
2. Check-ins 6–27 (or once `behavioral.confidence >= 0.3`) use the behavioral
   assessment against fixed universal thresholds.
3. Check-in 28 enables self-relative z-score deviation against the agent's
   Welford baseline.

Stage boundaries derive from `BEHAVIORAL_AUTHORITY_THRESHOLD = 0.3` over
`BOOTSTRAP_UPDATES = 10`, and `is_baselined` at `baseline_confidence >= 0.8`
with the confidence ramp `(evidence_count - 5) / 25`, where `evidence_count` is
`read_update_count` under the hold and `update_count` when
`UNITARES_HOLD_UNREAD_AXES=0` (giving stages at check-ins 1–2, 3–24, and 25+
with the hold off). From check-in 6 on, an agent that has supplied too few
readings gets no verdict at all: the response carries `safe` with risk 0, driver
`insufficient_reading`, and a provisional grade, which means nothing was
measured, not that anything was cleared. Reporting complexity with check-ins and
recording outcomes (including verified confidence–outcome pairs) supplies the
readings. Absolute safety floors and basin-health gates remain in force
throughout for axes with a recent reading; an unscored axis raises no risk and
trips no floor.

The roadmap target for richer cold-start grounding (a class-anchor reference
blend) is in `docs/ontology/eisv-proprioception-contract.md`, *Core mathematical
posture*; it is not the live path.

## Calibration Ledger and Corroboration Mechanics

- **Evidence weight by observer**: an outcome you submit yourself is graded
  `tool_observed` (evidence weight 0.65) at best, because the public path caps a
  caller attesting its own result — and 0.65 is exactly the calibration
  admission floor, so a self-described tool call is admitted at the boundary,
  not above it. Rows the server or an external signal observed can grade higher
  (0.85 / 1.00). The grade is not a score of your work; it is a statement about
  who checked. When a claim grades `claim_only`, the response's
  `corroboration_hint` names the reference keys the grader reads (they reach
  `self_report_with_refs`, below the calibration floor).
- **Idempotency and durable delivery**: while the binding ledger is retained,
  prediction-bound outcomes are database-idempotent per
  `(agent_id, prediction_id)`: an identical retry returns the first canonical
  outcome, while conflicting reuse is rejected. New eligible bound outcomes
  create durable calibration work in that same transaction. A recovery worker
  delivers each sample once per configured sink using receipts committed with
  projection state; transient failures leave work pending. File consumers must
  share the actual durable files and lock. Changing destination, backend, or
  epoch does not silently reroute pending work. Older outcomes without receipts
  are not automatically backfilled, and unbound submissions retain best-effort,
  non-idempotent semantics. After both canonical outcome and binding expire, a
  new canonical submission may be established. The prediction registry is a
  cache carried in the monitor snapshot; durable binding authority lives in the
  database.
- **Omitted confidence and simulation**: do not reweight the legacy `C(V_ODE)`
  omitted-confidence estimator without prospective outcome calibration because
  confidence history can feed later entropy penalties. That derived estimate
  stays internal: omitted confidence does not mint an agent tactical prediction
  or become an agent-reported calibration observation. Earlier explicit
  predictions remain available for their eventual outcomes. Simulation restores
  prediction bookkeeping and does not write trajectory calibration observations.
