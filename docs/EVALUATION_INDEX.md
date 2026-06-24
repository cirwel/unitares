# Evaluation Catalog — EISV validation, ablations, dogfood, analysis

Single index of the evaluation/ablation/dogfood/validation surface, so the work is
discoverable instead of rediscovered. Before adding a new eval or "does EISV
actually work" analysis, **check here first** — much already exists, scattered
across `scripts/analysis/`, `scripts/eval/`, `scripts/diagnostics/`, `src/`,
`tests/`, `demos/`, `docs/operations/`, `~/.unitares/analysis/`, and the
`hermes-agent` repo. There was no catalog before this one.

> **Honesty of this index.** Assembled 2026-06-23. Entries marked **✓** were read/run
> that session; **~** are characterized from filename + location only (not yet read) —
> do not trust the one-liner as a finding. This is an inventory, not a re-derivation.
> Keep it accurate: add a row when you add an eval; mark it ✓ once verified.

## ⚠ Read this before trusting any EISV-validation number

The honest current state of "does EISV discriminate / predict / prevent" lives in
memory `project_eisv-validation-gap.md` and `demos/frt_autonomy_sandbagging/REAL_LLM_FINDINGS.md`.
Headline as of 2026-06-23:

- **Organic-fleet validation is structurally impossible** — the tool-failure-labeled
  population (transport-level callers) and the EISV-bearing population (onboarded,
  checking-in agents) are disjoint. Don't try to validate on `audit.tool_usage` joined
  to verdicts; it returns ~0 because the keys don't overlap.
- The **`~/.unitares/analysis/eisv-validation-*` scheduled oneshot is a broken join**
  (0.000 in every cohort while failures exist) — retire/repoint, don't trust.
- EISV Integrity is a **confidence-vs-outcome consistency** detector: catches naive
  overconfidence against a calibrated baseline (AUC ~1.0 on a model organism),
  **collapses on noisy baselines (~0.75) and inverts on calibrated concealment (~0.19)**;
  **cross-framing** dominates for concealment (~0.97).

## Validation — "does EISV track reality / discriminate?"

| Artifact | What it does | Status |
|---|---|---|
| `demos/frt_autonomy_sandbagging/` (`run_unitares_demo.py`, `unitares_integration.py`) | ✓ Model-organism sandbagging eval through real `BehavioralEISV`; Integrity AUC naive=1.0, calibrated≈0.35 | active, 11 tests pass |
| `demos/frt_autonomy_sandbagging/real_llm_probe.py` + `REAL_LLM_FINDINGS.md` | ✓ Real-LLM (Ollama) extension; competence boundary + cross-framing arm | added 2026-06-23 (PR #1026) |
| `scripts/analysis/outcome_validation.py` | ~ outcome-vs-EISV validation against `audit.outcome_events` | unread — verify before reuse |
| `scripts/analysis/eisv_skeptic_report.py` | ~ skeptic-framed EISV report (overlaps the validation question) | unread |
| `scripts/analysis/prospective_prediction_cohort.py` | ~ prospective prediction cohort (forward-looking discrimination) | unread |
| `scripts/analysis/validate_theoretical_foundations.py` | ~ checks the theoretical (ODE/stability) claims | unread |
| `scripts/analysis/validate_basin_gate.py` | ~ validates the basin health-gate behavior | unread |
| `~/.unitares/analysis/eisv-validation-2026-06-13_0900.md` | ✓ scheduled oneshot — **BROKEN JOIN, null, do not trust** (see warning above) | retire/repoint |
| (scratchpad) `eisv_validation/leadtime_probe.py` | ✓ lead-time / warning-vs-reaction on real data; no advance-warning for task-failure (AUC 0.545) | not yet in repo |

## Ablation

| Artifact | What it does | Status |
|---|---|---|
| `scripts/analysis/eisv_ablation_matrix.py` | ~ EISV ablation matrix (which components carry signal) | unread |
| `scripts/analysis/ablation_negative_controls.py` + `tests/test_ablation_negative_controls.py` | ~ negative-control ablations | unread |
| `scripts/diagnostics/dogfood_ablation_guard.py` + `tests/test_dogfood_ablation_guard.py` | ~ guard that ablation/dogfood invariants hold | unread |
| `tests/test_eisv_ablation_matrix.py` | ~ tests for the ablation matrix | unread |
| `docs/operations/ablation-initiates-finding-2026-06-16.md` | ✓ finding: `strict_bad` 0→1 was **observed/classified, NOT prevented** — corrected; not EISV validation | logged |
| `docs/operations/ablation-negative-controls.md` | ~ negative-controls writeup | unread |

## Dogfood

| Artifact | What it does | Status |
|---|---|---|
| `scripts/analysis/dogfood_dialectic.py` | ~ dogfood of the dialectic path | unread |
| `agents/common/dogfood_friction.py` + `tests/test_r6_dogfood.py` | ~ dogfood-friction capture (agents building UNITARES run under it) | unread |
| `hermes-agent/skills/dogfood/` (+ `optional-skills/dogfood/`) | ~ Hermes dogfood **skill** (report template, adversarial-ux-test) — process, not stored runs | separate repo |

## Resident validation

| Artifact | What it does | Status |
|---|---|---|
| `src/resident_validation*.py`, `scripts/diagnostics/resident_validation_*` | ~ supervised-invocation / cohort / tick / canary for resident agents | unread |
| `docs/operations/resident-validation-cohort.md`, `resident-validation-supervised-invocation.md` | ~ resident-validation design docs | unread |

## Analysis / metrics (supporting, not pass/fail evals)

`scripts/analysis/`: ~ `analyze_drift`, `basin_estimation`, `contraction_analysis`,
`eisv_pca_analysis`, `compositionality_metrics`, `report_calibration`,
`outcome_inventory`, `export_outcome_dataset`, `plot_eisv_trajectories`,
`pin_ttl_bleed_*`. · `scripts/eval/`: ~ `metrics.py`, `retrieval_eval.py`
(+ `tests/retrieval_eval/`). All **unread** here — characterized by name.

## Hermes-agent (separate repo, separate run history)

`environments/benchmarks/` + `tests/environments/benchmarks/` · `skills/mlops/evaluation/`
(incl. `lm-evaluation-harness`) · `skills/dogfood/`. **Not consolidated with UNITARES's** —
cross-repo ablation/dogfood results are not joined to UNITARES findings. Kenny flagged
Hermes has ablation + dogfood runs (2026-06-23); their results are not indexed here yet.

## Recurring scheduled outputs (`~/.unitares/analysis/`)

- `eisv_validation_oneshot.sh` → `eisv-validation-*.md` — **broken (see warning)**.
- `report-2026*.md` — recurring **per-phase latency** analysis (perf, not EISV validation).

## Maintenance

This is the catalog of record for evaluation work. When you add or run an eval,
add/update its row and mark **✓** once you've verified what it actually shows. The
biggest gap is **depth**: most rows are `~` (unread) — a future pass should read the
`scripts/analysis/` validation/ablation scripts and fold their real findings here, so
the next "does EISV work?" question starts from what exists.
