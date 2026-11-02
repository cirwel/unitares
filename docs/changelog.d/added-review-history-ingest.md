- **Proposed review-history tooling:** recover the optional, dry-run-default
  `scripts/dev/ingest_review_records.py` producer and its fixture tests. It
  imports review-gate comments and self-attested dispositions as bounded
  historical observations, with current App attribution, source fingerprints,
  partial-import recovery and status readback. Adoption is pending; no live
  backfill or schedule is enabled. See `docs/dev/REVIEW_HISTORY_IMPORT.md`.
