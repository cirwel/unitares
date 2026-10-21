# Dialectic reviewer evidence

Labelled evidence about what dialectic reviewers actually do. Two separate
cohorts live here. They were labelled at different times, with different units,
different taxonomies and different selection rules, and **they must never be
pooled**.

| Cohort | Unit | Labels | Files |
|---|---|---|---|
| 2026-08-09 reviewer-verdict cohort | Recorded reviewer rejections (`synthesis` verdicts) in sessions created 2026-06-28 to 2026-08-09, probe-family excluded (n=18) | Route: `deny` / `cooldown` / `needs_evidence` / `human` | [`dialectic-reviewer-verdict-eval-2026-08-09.md`](dialectic-reviewer-verdict-eval-2026-08-09.md), [`dialectic-reviewer-verdict-labels-2026-08-09.json`](dialectic-reviewer-verdict-labels-2026-08-09.json) |
| 2026-08-19 antithesis cohort | Every substantive non-canary `antithesis` message (n=97) | Behaviour: `refutes_substantive` / `concurs_with_conditions` / `ratifies` / `formulaic` / `non_verdict` | [`dialectic-reviewer-labels.md`](dialectic-reviewer-labels.md), [`dialectic-reviewer-labels-20260819.jsonl`](dialectic-reviewer-labels-20260819.jsonl) |

## ⛔ Split at 2026-07-02; not poolable

The reviewer backend changed on **2026-07-02**, when the orchestrated Codex
reviewer was activated and the local model became the degraded fallback. Any
rate from either cohort must be read on one side of that date.

- The 2026-08-19 file records an `era` on every row (`pre_codex_reviewer` /
  `post_codex_reviewer`, split at 2026-07-02), and its document reports the two
  eras separately. The pooled distribution is printed for reference only and
  must not be quoted.
- The 2026-08-09 cohort window (2026-06-28 to 2026-08-09) straddles the same
  boundary and carries no per-row era, so its route shares are not comparable
  with either era of the 2026-08-19 file.
- The two files label different things (verdict routes versus antithesis
  behaviour). Do not concatenate them, join them, or add their counts.

Both label sets are single-annotator and unblinded, and neither establishes
whether a reviewer was right. `core.dialectic_messages.agrees` is not backfilled
from either file: every row is an inferred label, never a first-party verdict.

## Consumers

- `scripts/dev/dialectic_verdict_labels.py` reads the 2026-08-09 JSON and
  reproduces that cohort (`--verify-baseline`).
- `tests/test_runtime_observations.py` checks the 2026-08-19 JSONL against the
  counts its document quotes.
- [`../../EVALUATION_INDEX.md`](../../EVALUATION_INDEX.md) lists the 2026-08-19
  set under *Labelled sets*.
