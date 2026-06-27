# Evaluation Catalog — EISV validation, ablations, dogfood, analysis

Single index of the evaluation/ablation/dogfood/validation surface, so the work is
discoverable instead of rediscovered. **Before adding a new eval or "does EISV
actually work" analysis, check here first** — much already exists, and at least one
session (2026-06-23) rebuilt machinery that was already in `scripts/analysis/`.

Rows verified by reading the code on 2026-06-23 are **✓**; entries still inferred from
name only are **~**. Freshness flags call out scripts that **won't run as-is** (removed
backends, missing source symlinks). Hermes-agent's ablation/dogfood lives in its own
repo (automation-side) and is intentionally *not* consolidated here.

## Semantic guardrail: EISV is proprioception, not verdict authority

Read [`docs/ontology/eisv-proprioception-contract.md`](ontology/eisv-proprioception-contract.md)
before interpreting these reports. EISV/prior-state analysis asks whether
proprioceptive telemetry adds signal over baselines. It does **not** let EISV
supply its own outcome labels, and it does not treat ordinary CI/test failures as
moral badness. Human-facing labels should distinguish task-negative evidence,
contract/process violations, authority/harm events, synthetic red-team fixtures,
and unknown/unmeasured outcomes.

### Outcome-label vocabulary

Use `bad` only as a data label, never as an undefined success/failure slogan:

| Term | Meaning |
|---|---|
| `is_bad=true` / `bad` | An outcome row labeled negative by its recorded type or rubric. It is an analysis label, not a moral category. |
| `task-negative` | A failed test/tool/task result such as `test_failed`, `tool_rejected`, or `task_failed`. |
| `strict_bad` | A strict-scope negative row with stronger provenance requirements; useful for validation only after enough rows exist. |
| `prevented` | Only valid when a policy/actuator actually blocked, paused, rejected, or reverted an adverse effect. A counted `bad` row by itself is observed evidence, not prevention. |

## ⚠ Start here: the two scripts that already answer "does EISV discriminate?"

Don't rebuild discrimination analysis — these exist and are current:

- **`scripts/analysis/eisv_skeptic_report.py`** ✓ — the substantive one. Computes AUC/Brier
  **lift of EISV/prior-state over a previous-outcome baseline**, emits a runtime verdict
  (`SKEPTICAL` / `KEEP-TESTING` / `WEAK` / `INCONCLUSIVE`). The EISV-distributional KILL in
  memory came from running this (Probe A: negative AUC lift). No hardcoded conclusion.
- **`scripts/analysis/eisv_ablation_matrix.py`** ✓ — same question across scope/window/lead
  slices with bootstrap CIs, permutation p-values, BEAM-lane exclusion.

The honest current state of "does EISV discriminate / add predictive signal / support safer policy" lives in memory
`project_eisv-validation-gap.md` (the `frt_autonomy_sandbagging` demo and its
`REAL_LLM_FINDINGS.md` write-up were removed from the repo as out-of-scope — see
[`REPO_SCOPE.md`](REPO_SCOPE.md); the headline below preserves the finding).
Headline (2026-06-23): **organic-fleet validation is structurally impossible** (tool-failure
population and EISV-bearing population are disjoint); the **`~/.unitares/analysis/eisv-validation-*`
scheduled oneshot is a broken join — do not trust**; EISV Integrity catches naive overconfidence
against a calibrated baseline but **collapses on noisy baselines (~0.75) and inverts on
calibrated concealment (~0.19)**, where **cross-framing dominates (~0.97)**.

## Validation — "does EISV track reality / discriminate?"

| Artifact | What it does | Output / finding | Freshness |
|---|---|---|---|
| `scripts/analysis/eisv_skeptic_report.py` ✓ | AUC/Brier lift of EISV vs previous-outcome baseline; runtime verdict | Markdown report; KILL of EISV-distributional came from it | current (live PG) |
| `scripts/analysis/eisv_ablation_matrix.py` ✓ | Same vs-baseline across scope/window/lead; bootstrap CI, permutation p | Markdown matrix; no hardcoded verdict | current |
| `scripts/analysis/outcome_validation.py` ✓ | Buckets agent-days by legacy→grounded basin-flip; outcome rates per bucket | Console table + `--csv`; tool | current |
| `scripts/analysis/prospective_prediction_cohort.py` ✓ | Counts registry-bound holdout predictions (plumbing only, no claim) | Markdown cohort summary | current |
| `scripts/analysis/validate_basin_gate.py` ✓ | Validates #689 basin-health gate never raises risk; healthy wobbles stay safe | Console PASS/FAIL + exit; hardcoded 06-13 Sentinel false-pause fixture | current |
| `scripts/analysis/validate_theoretical_foundations.py` ✓ | Greps `governance_core` source for ODE/coherence terms vs theory doc | Pass/fail console | **⚠ won't run by default** — needs `governance_core/` source symlink; brittle exact-string greps |
| ~~`demos/frt_autonomy_sandbagging/`~~ (removed) | Model-organism sandbagging through real `BehavioralEISV` | Integrity AUC naive=1.0, calibrated≈0.35 | **removed** from repo as out-of-scope (#1043, see `REPO_SCOPE.md`); finding preserved in `SCOPE_AND_THREAT_MODEL.md` |
| ~~`demos/frt_autonomy_sandbagging/real_llm_probe.py`~~ (removed) | Real-LLM (Ollama) extension; competence boundary + cross-framing | naive≈0.75, calibrated inverts≈0.19, cross-framing≈0.97 | **removed** with the demo (#1043); finding preserved in `SCOPE_AND_THREAT_MODEL.md` |
| `~/.unitares/analysis/eisv-validation-2026-06-13_0900.md` ✓ | Scheduled oneshot cohort comparison | **BROKEN JOIN — null, do not trust** | retire/repoint |
| (scratchpad) `eisv_validation/leadtime_probe.py` ✓ | Lead-time / warning-vs-reaction on real data | No advance-warning for task-failure (AUC 0.545) | not yet in repo; overlaps `eisv_skeptic_report` |

## Ablation

| Artifact | What it does | Output / finding | Freshness |
|---|---|---|---|
| `scripts/analysis/ablation_negative_controls.py` ✓ | Synthetic known-safe/bad fixtures as red-team controls | JSONL fixtures; hardcoded "SYNTHETIC NEGATIVE CONTROL — not validation" | current |
| `scripts/diagnostics/dogfood_ablation_guard.py` ✓ | Silent CI guard: identity neutrality, BEAM/substrate lanes, matrix exclusion | Empty stdout = healthy; alerts only on regression | current |
| `docs/operations/ablation-negative-controls.md` ✓ | Documents the negative-controls fixture (synthetic-only, never persisted) | "validates plumbing + containment, NOT EISV"; smoke `strict_bad:4` | current (Experimental) |
| `docs/operations/ablation-initiates-finding-2026-06-16.md` ✓ | Finding: `strict_bad` 0→1 was **observed/classified, NOT prevented** | logged correction; not EISV validation | logged |

## Dogfood

| Artifact | What it does | Output | Freshness |
|---|---|---|---|
| `scripts/analysis/dogfood_dialectic.py` ✓ | Live dogfood: onboard→request_review→submit_thesis, asserts UUID consistency | PASS/FAIL; needs live MCP :8767 | current |
| `agents/common/dogfood_friction.py` ✓ | Normalizes friction observations into `/api/findings` events | Library; event dict + deterministic fingerprint | current |
| `tests/test_r6_dogfood.py` ~ | R6 dogfood test | — | unread |

## Resident validation

**What it's for:** a scaffold to ask whether long-running residents (Vigil/Sentinel/Lumen)
actually improve UNITARES over time — by emitting bounded, non-actuating "I observed X, predict
Y" tick envelopes a future supervisor can score. **Today it is INERT** (local JSONL only, no
UNITARES writes, nothing scheduled) — a measurement harness, not a live subsystem.

| Artifact | What it does | Freshness |
|---|---|---|
| `src/resident_validation.py` / `_runner.py` / `_invocation.py` ✓ | Build deterministic low-authority tick envelopes; canary runner; lock + tick-cap + local audit | current (pure libs) |
| `scripts/diagnostics/resident_validation_{supervised_invocation,tick,canary}.py` ✓ | CLIs over the above; only side effect is `data/resident_validation/` JSONL | current |
| `docs/operations/resident-validation-{cohort,supervised-invocation}.md` ✓ | v0 cohort + supervised-invocation design; matches code | current (Experimental) |

## Analysis / metrics (supporting — not pass/fail evals)

| Artifact | What it does | Freshness |
|---|---|---|
| `scripts/analysis/outcome_inventory.py` ✓ | Read-only inventory of outcome provenance/objectivity/prior-state coverage | current (live PG) |
| `scripts/analysis/export_outcome_dataset.py` ✓ | Exports flattened `audit.outcome_events` for offline study | current |
| `scripts/analysis/analyze_drift.py` ✓ | `trajectory_validated` convergence + decision/EISV correlation | current (JSONL path legacy) |
| `scripts/analysis/basin_estimation.py` ✓ | Monte-Carlo EISV basin-of-attraction mapping | current (pure `governance_core`) |
| `scripts/analysis/contraction_analysis.py` ✓ | EISV Jacobian contraction: eigenvalues, Gershgorin, theta sweep | current (pure) |
| `scripts/analysis/plot_eisv_trajectories.py` ✓ | Plots EISV convergence/degradation/recovery (synthetic) | current (pure) |
| `scripts/analysis/pin_ttl_bleed_report.py` ✓ | Tests pin-TTL masking hypothesis from audit events | current (live PG) |
| `scripts/eval/metrics.py` ✓ | Pure ranking metrics (DCG/nDCG/recall/MRR) | current (CI-pinned) |
| `scripts/eval/retrieval_eval.py` ✓ | KG retrieval quality eval over labeled corpus | current (needs live PG + embeddings) |
| `scripts/analysis/report_calibration.py` ✓ | Strategic/tactical calibration bins, ECE, failure modes | **⚠ possibly-stale** — in-process state, no live-DB load path |
| `scripts/analysis/eisv_pca_analysis.py` ✓ | PCA/correlation over EISV histories | **⚠ won't run** — reads REMOVED SQLite backend; hard-gated |
| `scripts/analysis/compositionality_metrics.py` ✓ | Topographic-similarity of Lumen *primitive utterances* (not EISV) | **⚠ stale-ish** — external anima SQLite; synthetic fallback |

## Recurring scheduled outputs (`~/.unitares/analysis/`)

- `eisv_validation_oneshot.sh` → `eisv-validation-*.md` — **broken join (see warning)**.
- `report-2026*.md` — recurring **per-phase latency** analysis (perf, not EISV validation).

## Maintenance

Catalog of record. When you add or run an eval, add/update its row and mark **✓** once
verified. Remaining gaps: a few `~` rows (e.g. `tests/test_r6_dogfood.py`); the
`⚠`-flagged scripts (`validate_theoretical_foundations`, `eisv_pca_analysis`,
`compositionality_metrics`, `report_calibration`) are candidates to **fix or sunset**;
and the scratchpad `leadtime_probe.py` should either land in the repo or be retired in
favor of `eisv_skeptic_report.py`, which it overlaps.
