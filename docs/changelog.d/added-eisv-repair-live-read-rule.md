- **eisv:** two follow-ups from an adversarial design review of the open
  decisions on the 2026-12-01 read and the shadows.
  - `docs/evaluations/eisv-i-rest-live-read/rule-v0.md` declares, before the
    read, how the live check of the 2026-10-15 basin and I-rest repairs is
    judged: behavioral only, one era at a time, and a pooled share per stratum,
    in the replay's own statistic, with stated floors.
  - New tests in `tests/test_eisv_ablation_matrix.py` pin that PASS condition 1
    implies condition 2 under the shipped estimator, and show that the printed
    three-decimal p cannot decide condition 1. `docs/EVALUATION_INDEX.md`
    carries the matching reading note.
  - The stop rule is unchanged.
