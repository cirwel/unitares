# Independent-operator cohort — enrollment records

Append-only companion to
[`independent-operator-cohort-preregistration-v0.md`](independent-operator-cohort-preregistration-v0.md).
Each enrollment is added by PR and never edited afterward; corrections are
new dated entries. The protocol file itself does not change when an operator
enrolls — that is why this file exists.

Required fields per phase-1 entry (see the protocol's Enrollment section):
operator name or pseudonym · disclosed relationship and consideration ·
fleet shape and cron declaration · declared outcome producers · release tag
and commit sha under test · harness script sha256s · demo-agent
`identity_id` (deleted before window start) · plumbing-check counts ·
publication consent covering negative results and abandonment.

Phase-2 entries record the actual window-start timestamp (UTC) after the
first qualifying check-in.

## Entry template

Copy this block for a phase-1 enrollment PR; every field is required, and
"none" is an acceptable value only where it is true:

```markdown
### <operator pseudonym> — phase 1, <UTC date>
- Relationship to maintainer / consideration received: <disclosed plainly>
- Fleet shape: <N agents; client types; which are cron-driven>
- Declared outcome producers: <e.g. CI runner, test harness, review tool>
- Under test: <release tag> @ <commit sha>
- Harness sha256: eisv_ablation_matrix.py=<sha256>, eisv_skeptic_report.py=<sha256>
- Protocol amendment: v0.1 (2026-09-02); lane-P fixture rule: corrected (fixed by REGISTERED_READ_MANIFEST)
- Lane-P read IDs: operator-<pseudonym>-day58-seed-0, operator-<pseudonym>-day58-seed-1, operator-<pseudonym>-day58-seed-2 (`--uncertainty-seed` equals the seed each names)
- Demo identity_id: <id> (rows deleted before window start: <UTC timestamp>)
- Plumbing check: <producer -> eligible-row count, per producer>
- Publication consent: <explicit sentence covering negative results and
  abandonment, signed-off in the PR by the operator's account>
```

Phase-2 amendment template:

```markdown
### <operator pseudonym> — phase 2, <UTC date>
- First qualifying check-in (window day 1): <UTC timestamp>
- Lane-P read instant (day 58): <UTC timestamp, computed now, frozen>
```

---

*(no enrollments yet)*
