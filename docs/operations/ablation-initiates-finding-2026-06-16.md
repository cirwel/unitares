# Ablation Finding — strict_bad observed 0 → 1 (NOT "prevented")

**Recorded:** June 16, 2026
**Status:** Finding logged — **measurement/instrumentation step**; inconclusive (strict n = 1 bad); **not** prevention, **not** EISV validation
**Surface:** ablation analysis path (`scripts/analysis/outcome_inventory.py`,
`scripts/analysis/eisv_ablation_matrix.py`, `eisv_skeptic_report.py`)

---

## Correction to the original report

This was first reported (and first recorded here) as *"prevented bad outcomes 0 → 1."*
Live re-verification (Hermes, 2026-06-16) shows that wording is wrong on two counts:

1. The metric that moved is **`strict_bad`** (count of strict-scope bad outcomes in
   the inventory), not a count of prevented outcomes.
2. The captured row's `decision_action = proceed` — UNITARES **observed and
   classified** a strict bad outcome, it did **not** stop one.

> **Correct wording:** "strict bad evidence moved from zero to one."
> **Not:** "UNITARES prevented a bad outcome."

The filename retains the original "initiates/finding" slug for branch/PR continuity;
the content below is the corrected record.

## What actually moved

The watchdog delta:

> `strict_bad` increased 0 → 1; new EISV/prior-state slice beats previous-outcome
> baseline: `strict:30:0`

Before this, **strict scope had no negative class at all** — zero strict bad
outcomes — so any strict-scope predictive claim was structurally impossible (no
bad examples to discriminate). The strict lane now has its first bad row.

### Live inventory (`--window-days 90 --leads 0,5,30`)

| Field | Value |
|---|---|
| total outcomes | 64,153 |
| total bad | 1,589 (2.5%) |
| strict outcomes | 2,569 |
| **strict bad** | **1** |
| hard-exogenous outcomes | 2,517 |
| e-process eligible | 287 |
| prediction-id present | 55 |

### The strict bad row — strong provenance, but a seeded probe

| Field | Value |
|---|---|
| outcome type | `test_failed` |
| is_bad | true |
| verification source | `external_signal` (externally verified) |
| test name | `overconfidence_probe` |
| error | **seeded** assertion failure — `assert 2 + 2 == 5` |
| prediction binding | `registry` |
| reported confidence | ~0.915 |
| **decision action** | **proceed** |

This is a **controlled probe event** (seeded assertion), not organic production
evidence. It should be reported as controlled-probe evidence, not as a real-world
strict failure.

## Ablation matrix (`--scopes strict,task --windows 30,90 --leads 0,5,30`)

The new slice the watchdog flagged:

**`strict / 30d / 0min lead`** — trusted 118, bad **1**, prior_state 101, prior_risk
101, baseline AUC 0.557, baseline Brier 0.0271, best = `previous_bad_plus_prior_risk`,
AUC delta **+0.419**, Brier improvement **+0.0023**, beats both **yes** —
**conclusion: INCONCLUSIVE (fewer than 10 bad outcomes).** A +0.419 AUC delta on a
single bad event is fragile: one point swings the slice.

Task scope stays more informative but only weakly positive:

| Slice | trusted | bad | best model | AUC Δ | Brier Δ | beats both | conclusion |
|---|---:|---:|---|---:|---:|---|---|
| task / 90d / 0min | 6,870 | 80 | `previous_bad_plus_prior_risk` | +0.121 | +0.0004 | yes | weak signal |
| task / 90d / 5min | 6,870 | 80 | `previous_bad_plus_prior_risk` | +0.024 | +0.0003 | yes | weak signal |
| task / 90d / 30min | 6,870 | 80 | `prior_s_binned` | −0.068 | −0.0028 | no | skeptical |

All 30-day task slices remain skeptical.

## Interpretation (what this is, and what it is not)

- **Measurement / instrumentation — improved.** The pipeline saw the strict bad
  row, classified its provenance, bound it to a prediction, incorporated it into
  the matrix, and the watchdog noticed the delta. Targeted tests still pass
  (16 passed).
- **Diagnostic signal — weak / interesting.** One strict slice now beats baseline,
  but strict n=1 bad is too fragile to read as lift.
- **Governance policy — not proven.** The row's `decision_action = proceed`.
- **Enforcement / prevention — not shown.** Nothing here stopped a bad outcome.

The most interesting angle: this is an **overconfidence case** — high reported
confidence (~0.915) while a hard external test failed and the decision was
`proceed`. That is a clean label for "confidence was high, outcome was bad," which
is exactly the failure mode UNITARES cares about — captured as calibration
evidence, not caught-and-stopped.

## Evidence discipline — what would move this forward

1. Hold the corrected claim: **`strict_bad` observed 0 → 1**, not "prevented."
2. Classify this row as **controlled probe evidence** (it is seeded), distinct from
   organic external strict-bad evidence.
3. Reach **≥ 10 strict bad outcomes** before interpreting predictive lift — the
   report itself gates `<10 bad` as inconclusive
   (`eisv_skeptic_report.summarize_conclusion`).
4. Keep requiring strong provenance on strict-bad rows: `verification_source =
   external_signal`, `hard_exogenous = true`, `eprocess_eligible = true`,
   `prediction_binding = registry`.
5. Watch whether `strict:30:0` still beats baseline after more strict bad rows
   arrive.
6. Treat this as an overconfidence-policy regression case: high confidence +
   external test failed + decision proceeded → open question of whether policy
   *should* have paused/guided, or whether it is only calibration evidence.

## Structural note — no cadence is accumulating this corpus

The ablation analysis scripts are CLI-only and run manually; **no GitHub Actions
`schedule:`/cron and no launchd plist invokes them.** This result came from a manual
re-run. With no recurring job, the strict corpus will not approach the ≥10-bad floor
on its own — the n=1 fragility above is structural, not just incidental.

## Provenance

- Re-verified live by Hermes on 2026-06-16 via `outcome_inventory.py`,
  `eisv_ablation_matrix.py`, and a 16-test targeted pytest battery (all passing).
- No production outcome rows, KG entries, or dialectic were created from this
  finding; the seeded probe row is controlled evidence, not a new write.
