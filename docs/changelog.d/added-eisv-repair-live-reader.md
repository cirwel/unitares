- **eisv:** `scripts/analysis/eisv_repair_live_read.py` implements the
  pre-declared live-read rule for the 2026-10-15 basin and I-rest repairs
  (`docs/evaluations/eisv-i-rest-live-read/rule-v0.md`). It reads behavioral
  `safe` → `approve` per (verdict driver, identity class) stratum. The RK1 era
  and the pre-repair baseline are read in one read-only snapshot and reported
  separately. Each stratum gets `CONSISTENT`, `BELOW`, `ABOVE` (with a
  qualifier) or `INCONCLUSIVE` under the rule's floors. The script refuses
  before 2026-10-23, without a resident roster, and across an instrument
  change.
