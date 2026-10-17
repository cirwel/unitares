- **eisv:** the outcome-read receipt written by `scripts/analysis/eisv_ablation_matrix.py`
  now records the code that ran: the checkout's commit, any uncommitted
  changes to the modules the read runs, and each module's SHA-256. Before
  this, a receipt recorded parameters but no code version, so a change to
  the estimator between registration and the 2026-12-01 read could not be
  detected afterwards. The block is recorded, never checked, and a value that
  cannot be determined is null rather than refusing the read.
