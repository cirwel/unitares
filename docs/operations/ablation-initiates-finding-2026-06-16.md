# Ablation Finding — "initiates" / prevented bad outcomes (0 → 1)

**Recorded:** June 16, 2026
**Status:** Finding logged — **inconclusive (n = 1)**, not a validation claim
**Surface:** ablation analysis path (`scripts/analysis/eisv_ablation_matrix.py`,
`eisv_skeptic_report.py`, `ablation_negative_controls.py`)

---

## The finding as reported

> An ablation test on initiates showed prevented bad outcomes 0 → 1.

Read literally: ablating the intervention-**initiate** path (CIRS
`governance_action(action="initiate", ...)`, e.g. `void_intervention`) changed the
count of *prevented bad outcomes* from **0** (ablated / no signal) to **1**
(signal present). The delta is a single prevented outcome.

## Why this is logged as inconclusive, not as evidence

This sits inside the regime the analysis path already refuses to read as signal:

- `eisv_skeptic_report.summarize_conclusion` returns
  `INCONCLUSIVE: fewer than 10 bad outcomes; predictive lift is too fragile.`
  for any slice with `bad < 10` (`scripts/analysis/eisv_skeptic_report.py:696`).
  A 0→1 prevented-outcome result is `bad`-count ≈ 1 — an order of magnitude
  below that floor.
- The framework's stated discipline (`docs/operations/ablation-negative-controls.md`,
  "Interpretation discipline") is that an ablation slice validates *plumbing and
  containment*, not the governance mechanism. "The fixture validates EISV" is
  called out explicitly as bad language.

So the honest reading is: **the initiate-ablation path is wired and observable —
it can register a prevented-outcome delta at all — but a single prevented outcome
is not evidence that intervention-initiation prevents bad outcomes.** It is one
sample in the `INCONCLUSIVE` band.

## What would turn this into a real result

To promote past `INCONCLUSIVE`, the same path needs the volume its own thresholds
demand, on real (non-synthetic) outcomes:

- ≥ 100 trusted outcomes and ≥ 10 bad outcomes in the window
  (`summarize_conclusion` gates), and
- a prevented-outcome count that holds up across windows/scopes, compared against
  the boring `previous_outcome_bad` baseline rather than against the ablated arm
  alone.

Until then this stays a logged observation, not a claim.

## Provenance

- Reported by the operator on 2026-06-16; recorded here per the "record the
  finding" decision rather than changing analysis code.
- No production outcome rows, KG entries, or dialectic were created from this
  finding (consistent with the synthetic/containment boundary that governs this
  surface).
